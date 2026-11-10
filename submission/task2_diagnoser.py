#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
任务二首轮：**LLM 诊断 + 修复**（主路径）

为什么单独立一个模块
--------------------
赛题是「基于大语言模型的 Scan Insertion 智能体系统设计」，任务二的 75 分
（问题识别与原因定位 30 + 问题修复正确性 30 + 问题闭环真实性 15）全部落在
「读懂 original.dofile 被注入了什么故障、为什么错、怎么改对」这一件事上 ——
这是 LLM 最该发挥的战场，不该被确定性修复器整个顶掉。

因此首轮流程改为：
  1. **LLM 主路径**（本模块）：把「任务说明书 + original.dofile + 网表事实 +
     手册 RAG + 命令白名单」交给 LLM，让它一次性给出
       · 结构化问题清单（现象 / 涉及对象 / 行号 / 根因 / 修复方式 / 违反的任务要求）
       · 修复后的**完整** dofile
  2. **确定性守卫**：LLM 的 dofile 必须通过
       「命令合法（无幻觉）+ 输入路径绝对 + 基础命令齐备 + 满足 task_spec 静态断言」
     才被采用；不过关就回退 spec_fixer 的确定性修复（保命，不丢分）。
  3. **证据链闭合**：LLM 给的行号 / 原文片段用于在 original.dofile 里定位，
     定位不到就用关键词检索兜底；**绝不编造行号**（定位失败时留空）。

设计纪律（与 spec_fixer 一致）
------------------------------
· 只依赖 task_spec 原文 + 网表事实 + 手册，**不读** preset_issues.json / golden.dofile。
· LLM 输出的每一处「修复」都要能被确定性断言复核，否则不采用它的 dofile。
"""

import json
import os
import re

try:                                                            # 包内引用
    from . import dofile_generator as _dg
    from . import manual_rag
    from . import spec_verifier
    from . import config
except ImportError:                                             # 直接脚本引用
    import dofile_generator as _dg
    import manual_rag
    import spec_verifier
    import config


# 修复后的 dofile 必须含有的基础命令。
# 与 scan_agent.REQUIRED_CMDS 同口径：缺任何一个，工具跑起来必然连锁报错
# （如缺 load_netlist -> present_design 无设计 -> 后续全报 SCAN-1021）。
_REQUIRED_CMDS = ("load_lib", "load_netlist", "present_design", "insert_dft_logic", "exit")

# 从 dofile 里提取命令名（用于构造 RAG 查询与基础命令检查）
_CMD_HEAD_RE = re.compile(r"(?m)^[ \t]*([a-z_][a-z0-9_]*)")


def _cmd_names(text):
    """提取 dofile 中出现的顶层命令名集合。"""
    return {m.group(1) for m in _CMD_HEAD_RE.finditer(text or "")}


def _rag_query(task, original_dofile, understanding):
    """
    构造手册检索查询 —— 让 LLM 有据可依地「查阅解决办法」。

    三路关键词合并：
      1. original.dofile 里实际用到的命令（错就错在这些命令的参数/顺序上）；
      2. task_spec 明确要求的配置语义（插链 / Wrapper / 分区 / DRC 豁免 …）；
      3. 结构化理解里已抽出的命令族（补上 original 漏写的那些命令的正确写法）。
    """
    parts = []
    names = _cmd_names(original_dofile)
    # 只保留 dftexp_scan 的配置/流程类命令，过滤掉 set/if/for 之类 Tcl 关键字
    for n in sorted(names):
        if n.startswith(("set_", "load_", "present_", "insert_", "examine_",
                         "add_", "dump_", "rpt_", "check_", "write_")):
            parts.append(n)

    u = understanding or {}
    if u.get("clock_signals"):
        parts.append("set_scan_signal clock")
    if u.get("reset_signals"):
        parts.append("set_scan_signal reset")
    if u.get("scan_enable_signals"):
        parts.append("set_scan_signal scan_enable")
    if u.get("scan_cell_mappings"):
        parts.append("set_scan_cell_mapping")
    if u.get("scan_cfg"):
        parts.append("set_scan_cfg")
    if (u.get("wrapper_cfg") or {}).get("enabled") or u.get("wrapper_signals"):
        parts.append("set_wrapper_cfg add_dedicated_wrapper_cell_type")
    if u.get("drc_rules"):
        parts.append("set_scan_drc_cfg set_scan_drc_rule_handling")
    if u.get("scan_element_exclude"):
        parts.append("set_scan_element")

    spec = task.get("task_spec", "") or ""
    for kw in ("扫描链", "wrapper", "分区", "DRC 违例", "SCANDEF", "CTL"):
        if kw.lower() in spec.lower():
            parts.append(kw)

    if not parts:
        return ""
    return " ".join(sorted(set(parts)))


def _facts_text(task, facts):
    """网表事实摘要（复用生成器的同一接口，两个任务口径一致）。"""
    try:
        return _dg._facts_summary(task, facts)
    except Exception:                                           # noqa: BLE001
        return "（网表事实不可用）"


def _validate_fixed_dofile(task, understanding, text):
    """
    确定性校验 LLM 修复后的 dofile。返回「不通过的原因」列表；空列表 = 通过。

    判据全部来自确定性代码（命令白名单 / 路径规则 / task_spec 静态断言），
    不依赖参考答案，public 与 hidden 一致。
    """
    reasons = []
    if not (text or "").strip():
        return ["输出为空"]

    # 1. 命令白名单（幻觉命令检查）
    violations = _dg.validate_dofile_commands(text)
    if violations:
        cmds = sorted({c for _ln, c in violations})
        reasons.append("含 %d 个未知命令 %s" % (len(violations), cmds[:5]))

    # 2. 输入路径必须绝对（dofile 在 deliverables/ 下运行）
    if _dg.has_relative_input_path(text):
        reasons.append("load_* 使用了 $script_dir 相对路径")

    # 3. 基础命令齐备
    names = _cmd_names(text)
    missing = [c for c in _REQUIRED_CMDS if c not in names]
    if missing:
        reasons.append("缺基础命令 %s" % missing)

    # 4. task_spec 静态断言（链数 / 端口命名 / 时钟声明 / DRC 豁免禁止 …）
    try:
        assertions = spec_verifier.build_assertions(task, understanding)
        bad = spec_verifier.check_assertions(assertions, text)
        if bad:
            reasons.append("未满足 %d 项任务要求（%s）" % (
                len(bad), "；".join(b["requirement"] for b in bad[:3])))
    except Exception:                                           # noqa: BLE001
        pass

    return reasons


# 修复后 dofile 的分隔标记。
# 为什么不用「整包 JSON」承载 dofile：dofile 含大量换行与引号，塞进 JSON 字符串
# 需要逐处转义，LLM 一旦漏转义（真实换行/未转义引号）整个 JSON 就解析失败 ——
# 实测 task_2/case3 正是这样丢掉整份修复结果。改用「JSON 只放 issues，
# dofile 用标记分隔的纯文本」，从根上消除转义风险。
_DOFILE_MARK = "<<<FIXED_DOFILE>>>"

# 预算阈值（秒）：低于它就不要求 LLM 输出完整 dofile，只让它做诊断。
#
# 实测（真实 deepseek-v4-pro，本地网络）任务二「诊断 + 完整 dofile」单次耗时
# 122.2s / 156.6s / 174.1s（task_2/case1、case1 复测、case3）——
# 而 150s 预算的 case 扣掉工具运行（30~60s）后，留给 LLM 的只有 ~100s，
# 这条路径必然超时、全军覆没（等于「从没真正用上 LLM」）。
#
# 因此按预算分流：
#   · 预算 >= 阈值（如 t2/case5 的 3600s）→ LLM 输出「issues + 完整 dofile」，完整智能体行为；
#   · 预算 <  阈值（150s 级）→ LLM 输出「issues + 每条 fix_action（具体命令）」，
#     修复由 spec_fixer 按同一份 task_spec 落地。LLM 依然承担了**问题识别与原因定位
#     （30 分）与修复决策**，只是把「誊写整份脚本」这段纯机械劳动交给确定性代码，
#     输出量从数千字符降到几百，单次延迟随之落到预算内。
_FULL_DOFILE_MIN_BUDGET_S = float(
    os.environ.get("SCAN_AGENT_TASK2_FULL_DOFILE_MIN_BUDGET", "300"))

_SYSTEM_PROMPT_HEAD = """你是广立微 dftexp_scan（Scan Insertion）工具专家。original.dofile 被注入了故障，
正确写法就在任务说明书里。请逐条对照，找出所有不一致处。

【输出】一个 JSON（```json 代码块），不要任何额外文字：
{
  "issues": [
    {
      "category": "dofile_execution|drc|dft_config|missing_artifact",
      "phenomenon": "现象（一句话）",
      "located_object": "涉及的命令，如 set_scan_signal -type clock",
      "locator": "L12",
      "excerpt": "该行原文片段（用于定位；不确定留空）",
      "root_cause": "根因：为什么错（引任务说明书要求）",
      "fix_action": "修复：写出正确的完整命令与取值",
      "violated_requirement": "被违反的说明书原文（逐字；无则留空）"
    }
  ]
}
"""

# 输出要求（二选一）
_DOFILE_RULE_FULL = f"""
【输出要求 —— 在问题清单之后，再输出修复结果】
JSON 之后单独一行写标记 {_DOFILE_MARK}，其后**原样输出修复后的完整 dofile 全文**
（纯文本，不要再被 ``` 代码块包裹，不要任何解释文字）。

【严格要求】
1. {_DOFILE_MARK} 之后必须是**完整可运行**的 dofile（含 load_lib / load_netlist /
   present_design / insert_dft_logic / exit），不能只给补丁片段。
2. 命令名只能来自「命令白名单」；输入文件路径必须用绝对路径（不要 $script_dir）。
3. 只修 task_spec 明确要求的内容；spec 没写的不要动、不要加。
4. 涉及时序单元实例名 / 端口名 / 网络路径时，以「网表事实」给出的真实名字为准，
   不得凭空编造。
5. issues 与修复后的 dofile 必须一致：修好的问题要在 issues 里列出，issues 里列的
   必须真的在修复后的 dofile 里改掉了。
6. locator 只写你**确实看到**的行号；不确定就留空 —— 编造行号会导致证据链不可信。"""

_DOFILE_RULE_ISSUES_ONLY = """
【注意】
1. 只输出该 JSON，不要输出 dofile，不要任何解释文字。
2. fix_action 必须落到**具体命令与取值**（如「把 -port resetn 改为 -port clk」
   「补上 -chain_count 4」）—— 修复将严格按它执行，含糊等于没诊断出来。
3. locator 只写确实看到的行号，不确定就留空（编造行号会使证据链不可信）。
4. 直接给结论，不要复述 original.dofile 或说明书的原文。"""


def _wants_full_dofile(task):
    """
    是否要求 LLM 输出完整修复 dofile。

    预算充裕 → 是（完整的「生成修复脚本」智能体行为）；
    预算紧张（150s 级）→ 否（只做诊断 + 修复决策，脚本由确定性代码落地），
    理由见 _FULL_DOFILE_MIN_BUDGET_S 注释。
    """
    budget = ((task or {}).get("limits") or {}).get("max_wall_time_s")
    try:
        return float(budget or 0) >= _FULL_DOFILE_MIN_BUDGET_S
    except (TypeError, ValueError):
        return False


def analyze_and_fix(task, original_dofile, llm, facts=None, understanding=None,
                    want_full_dofile=None):
    """
    任务二首轮：让 LLM 诊断故障（预算充裕时并产出修复后的完整 dofile）。

    Args:
        task: parse_task 结果
        original_dofile: 被注入故障的 dofile 原文
        llm: LLM 客户端
        facts: netlist_facts 结果（可为 None）
        understanding: 结构化理解（可为 None）
        want_full_dofile: 是否要求 LLM 输出完整修复 dofile；
            None = 按预算自动判定（见 _wants_full_dofile）

    Returns:
        dict | None: {
            "issues": [统一 issue dict...],
            "fixed_dofile": str（诊断-only 模式下为 ""，由调用方按 fix_action 落地）,
            "full_dofile": bool（本次是否走了完整 dofile 路径）
        }
        None 表示 LLM 不可用 / 调用失败 / 输出不合格。
    """
    if llm is None or not getattr(llm, "available", False):
        return None
    if not (original_dofile or "").strip():
        return None

    full = _wants_full_dofile(task) if want_full_dofile is None else bool(want_full_dofile)

    # RAG 只取 1 段：任务二的修复目标窄（就 original 里那几处故障），1 段即命中
    # 相关命令的语法小节；多给的段落只增加 input token，进而推高推理型模型的
    # 思考开销（实测单次延迟对输入长度敏感）。
    rag_context = manual_rag.search_manual(_rag_query(task, original_dofile, understanding),
                                           top_k=1)

    # 白名单按需裁剪：只列「original 用到的命令 + task_spec 提到的命令 + 基础命令」。
    # 全量 39 条 3560 字符，而任务二真正相关的通常只有 10 条左右 —— 直接砍掉
    # 约 2/3 的 prompt 长度，对降低单次延迟有实质作用。
    whitelist_prompt = _dg.build_command_whitelist_prompt(
        only=_relevant_commands(task, original_dofile))

    system_prompt = _SYSTEM_PROMPT_HEAD + (
        _DOFILE_RULE_FULL if full else _DOFILE_RULE_ISSUES_ONLY)

    user_prompt = f"""# 任务说明书（修复的唯一依据）
{task.get('task_spec', '')}

# 系统限制
{task.get('limitations', '')}

# 被注入故障的 original.dofile（修复对象）
```
{original_dofile}
```

# 命令白名单（命令名必须一字不差）
{whitelist_prompt}

# 网表事实（确定性解析器从本 case 的网表 + Liberty 抽取，可直接引用真实名字）
{_facts_text(task, facts)}

# 手册参考段落（RAG 检索，用于核对命令语法与参数形式）
{rag_context if rag_context else '（手册未提供）'}

按上面的输出要求作答。"""

    text = llm.chat(system_prompt, user_prompt,
                    max_tokens=config.LLM_TASK2_MAX_TOKENS if full
                    else config.LLM_TASK2_ISSUES_MAX_TOKENS)
    _dump_raw(text, task)
    if not text:
        print("  ⚠ LLM 任务二诊断无返回，回退确定性修复")
        return None

    data, fixed = _parse_response(text)
    if data is None:
        print("  ⚠ LLM 任务二诊断未产出合法 issues JSON，回退确定性修复")
        return None

    issues = _normalize_issues(data.get("issues") or [])
    if not issues:
        print("  ⚠ LLM 任务二诊断未给出任何问题，回退确定性修复")
        return None

    if not full:
        # 诊断-only 模式：修复由调用方按同一份 task_spec 用 spec_fixer 落地
        return {"issues": issues, "fixed_dofile": "", "full_dofile": False}

    reasons = _validate_fixed_dofile(task, understanding, fixed)
    if reasons:
        print("  ⚠ LLM 修复结果未通过确定性校验（%s），回退确定性修复"
              % "；".join(reasons))
        return None

    return {"issues": issues, "fixed_dofile": fixed, "full_dofile": True}


def _dump_raw(text, task):
    """调试用：把 LLM 原始返回落盘（仅在 SCAN_AGENT_DEBUG_LLM_RAW 指定路径时启用）。"""
    path = os.environ.get("SCAN_AGENT_DEBUG_LLM_RAW")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n\n===== {task.get('case_id', '?')} "
                    f"len={len(text or '')} =====\n{text or ''}\n")
    except OSError:
        pass


# 基础命令：无论 task_spec 是否提到，都必须出现在白名单里
_BASE_CMDS = (
    "load_lib", "load_netlist", "present_design", "insert_dft_logic",
    "examine_scan_drc", "examine_scan_chain", "set_scan_cfg", "set_scan_signal",
    "dump_netlist", "dump_ctl", "dump_def", "dump_scan_def",
)


def _relevant_commands(task, original_dofile):
    """任务二真正用得上的命令集合（original 已出现 + task_spec 提到 + 基础命令）。"""
    cmds = set(_BASE_CMDS)
    for m in re.finditer(r"(?m)^[ \t]*([a-z_][a-z0-9_]*)", original_dofile or ""):
        cmds.add(m.group(1))
    spec = task.get("task_spec", "") or ""
    for m in re.finditer(r"\b([a-z_]+_[a-z0-9_]+)\b", spec):
        cmds.add(m.group(1))
    return cmds


def _parse_response(text):
    """
    把 LLM 回复切成 (issues_json_dict, dofile_text)。

    格式：前半是 issues 的 JSON（可能被 ```json 包裹），后半以 _DOFILE_MARK 开头
    是纯文本 dofile。任一缺失返回 (None, "")，由调用方回退确定性修复。
    """
    if not text:
        return None, ""
    if _DOFILE_MARK in text:
        head, _, tail = text.partition(_DOFILE_MARK)
    else:
        # 容错：LLM 漏写标记时，退化为「首个 JSON 之后的全部内容当作 dofile」。
        # 找不到 JSON 起止就整段当 issues、dofile 留空（会被校验拦下）。
        head, tail = text, ""
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e > s:
            head, tail = text[:e + 1], text[e + 1:]

    data = _extract_json(head)
    fixed = _clean_dofile_text(tail)
    return data, fixed


def _extract_json(text):
    """从文本中提取首个 JSON 对象（容错：剥代码块 / 截取首尾花括号）。"""
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    s, e = t.find("{"), t.rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(t[s:e + 1])
        except ValueError:
            pass
    return None


def _clean_dofile_text(text):
    """清掉 dofile 段可能残留的代码块围栏与首尾空白。"""
    t = (text or "").strip()
    if not t:
        return ""
    m = re.match(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```\s*$", t, re.DOTALL)
    if m:
        t = m.group(1)
    else:
        t = re.sub(r"^```[a-zA-Z]*\s*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    t = t.strip()
    return t + "\n" if t else ""


_ALLOWED_CATEGORIES = {
    "dofile_execution", "drc", "dft_config", "missing_artifact",
}


def _normalize_issues(raw_issues):
    """
    把 LLM 的 issues 归一化成 spec_fixer 同款结构（供 scan_agent._issue_to_record 使用）。

    字段映射：
        located_object  -> issue 标题
        phenomenon      -> 现象
        root_cause      -> 根因
        fix_action      -> 修复方式
        locator         -> 'L12'（无则留空，由 _locate_in_text 按关键词兜底定位）
    """
    out = []
    if not isinstance(raw_issues, list):
        return out
    for it in raw_issues:
        if not isinstance(it, dict):
            continue
        obj = (it.get("located_object") or "").strip()
        phen = (it.get("phenomenon") or "").strip()
        if not obj and not phen:
            continue
        cat = (it.get("category") or "").strip()
        loc = (it.get("locator") or "").strip()
        # locator 只接受 'L<数字>' 形式，其余丢弃（避免把编造内容写进证据链）
        if not re.match(r"^L\d+$", loc):
            loc = ""
        out.append({
            "category": cat if cat in _ALLOWED_CATEGORIES else "",
            "issue": obj or phen,
            "located_object": obj or phen,
            "phenomenon": phen or obj,
            "root_cause": (it.get("root_cause") or "").strip(),
            "fix_action": (it.get("fix_action") or "").strip(),
            "violated_requirement": (it.get("violated_requirement") or "").strip(),
            "locator": loc,
            "excerpt": (it.get("excerpt") or "").strip(),
            "_source": "llm",
        })
    return out


def merge_issues(primary, secondary, original_text=""):
    """
    合并两路 issue（LLM 优先），按「涉及对象 + 现象」去重。

    两路都基于 **original.dofile**（修复起点）检测，因此描述的是同一批注入故障：
      · primary   = LLM 诊断结果（主路径，措辞更贴近「问题识别」的评分口径）；
      · secondary = spec_fixer 的确定性检测（兜底覆盖，防止 LLM 漏报某类故障）。

    Args:
        primary: LLM 的 issues（list of dict）
        secondary: spec_fixer 的 issues（list of dict）
        original_text: original.dofile 原文（用于补定位）

    Returns:
        list of dict（merged，primary 在前）
    """
    merged, seen = [], set()
    # 值 -> 已占用该值的问题键（用于「一方没有命令名、只有对象值」时仍然能合并）
    value_owner = {}
    for it in list(primary or []) + list(secondary or []):
        if not isinstance(it, dict):
            continue
        cmd, sig, values = _issue_key(it)
        key = (cmd, sig)
        if not cmd and not values:
            continue
        if key in seen:
            continue
        # 命令名缺失时，退化为「按对象值」判重：只要涉及同一个端口/实例/参数值，
        # 就视为同一问题（LLM 侧常用自然语言描述，没有命令名）。
        if not cmd:
            owner = None
            for val in values:
                if val in value_owner:
                    owner = value_owner[val]
                    break
            if owner is not None:
                seen.add(owner)
                continue
        seen.add(key)
        for val in values:
            value_owner.setdefault(val, key)
        merged.append(it)
    return merged


# 命令名：只认 dftexp_scan 的真实命令族前缀。
_CMD_TOKEN_RE = re.compile(
    r"\b(?:set|add|insert|examine|dump|load|present|rpt|connect|disconnect|delete|rewire|get_obj)"
    r"_([a-z0-9_]+)\b")
# 选项取值：`-port resetn` / `-type clock` / `--foo bar`（含引号与花括号形式）
_OPT_VALUE_RE = re.compile(r"-{1,2}[a-z][\w-]*\s+(\"[^\"]*\"|\{[^}]*\}|[^\s]+)")


def _issue_key(it):
    """
    计算一条 issue 的去重键 —— **必须能区分「同一个命令上的不同对象」**。

    旧实现用 `re.search(r"[a-z_]{4,}", blob)` 取首个英文词：
    `set_scan_signal` 里首个 ≥4 字母的词是 `scan`（`set` 只有 3 个字母），
    于是**所有** set_scan_signal / set_scan_cfg / examine_scan_dofile 的问题都塌成
    一条 —— 实测 task_2/case2 把 6 个预设问题并成 2 个（识别项直接 −20 分）。

    新键 = (命令名, 选项取值集合)。命令名缺失时退化为 ("", 值集合)，
    由调用方按「共享对象值」再合并一次。

    Returns:
        (cmd, "v1|v2", [v1, v2])；命令名缺失时 cmd 为 ""，由调用方按 values 二次合并。
    """
    obj = str((it or {}).get("located_object") or (it or {}).get("issue") or "")
    low = obj.lower()
    m = _CMD_TOKEN_RE.search(low)
    cmd = m.group(0) if m else ""

    values = []
    for raw in _OPT_VALUE_RE.findall(obj):
        v = str(raw).strip().strip("\"'{} ")
        if not v or v.lower() == cmd:
            continue
        # 总线下标归一：cfg_o[13:0] -> cfg_o（同一对象的不同写法不应重复登记）
        v = re.sub(r"\[.*$", "", v)
        if v and v not in values:
            values.append(v)
    values.sort()
    sig = "|".join(values[:6])
    return (cmd, sig, values)

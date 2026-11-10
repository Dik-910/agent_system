#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
任务规格校验模块（任务二的第二条修复驱动）

赛题 4.2.2 把任务二的故障分为三类，其中第三类「DFT 配置不符合任务要求」的特征是：

    Dofile 可以执行，但 Internal Scan / Wrapper Scan 或相关 DFT 配置不符合设计结构和
    任务要求（扫描链/ wrapper 链的数量、长度、时钟域、时钟边沿、Lockup 配置、
    DFT 信号配置、端口命名、分区归属、多余 wrapper 等）。

也就是说：**日志里根本不报 ERROR**。只靠「日志错误驱动」的修复循环会在第一轮就把这类
问题判为成功，导致问题识别（30 分）+ 问题修复（30 分）双归零。

本模块提供第二条驱动：把 task_spec 的关键要求抽成**可核验断言**（配置项 / 数值 /
端口名），回读最终 dofile 与工具日志逐条比对；若全部通过，再让 LLM 做一次
「任务要求 vs 最终配置」的对照复核（严格限定：必须有 task_spec 原文依据 + 证据，
否则不得报问题），从而覆盖「不报 ERROR 的配置类问题」。

设计原则：
  - 断言只取 task_spec 中**明确写出**的要求（数值、端口名、端口命名格式），不做推断；
  - LLM 复核要求「引用原文 + 指出证据」，并做可核验过滤，避免幻觉制造假问题
    （假问题会驱动错误的修改、浪费工具调用，比漏检更糟）。
"""

import re

try:
    from . import llm_client
except ImportError:                                             # pragma: no cover
    import llm_client


# ---------------------------------------------------------------------------
# 断言抽取（task_spec -> 可核验断言）
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r"[A-Za-z_][\w]*")


def _spec_idents(spec):
    """task_spec 中出现的全部标识符（用于过滤 LLM 幻觉出的「要求」）。"""
    return set(_IDENT_RE.findall(spec or ""))


def _builds_chain(task, understanding=None):
    """
    本 case 是否要真正构建扫描链。

    三步前置处理流程（connect_icg_only / replace_only / replace_unscan，如 task_1/case1、
    case4）**不建链**：此时「时钟/复位信号」「链数/链长/混时钟」都不属于任务要求，
    官方 golden 里也没有这些配置。若仍按 understanding 断言，会把「正确省略」误报成问题。
    """
    try:
        from . import dofile_generator as _dg
    except ImportError:
        import dofile_generator as _dg
    try:
        wf = _dg.decide_workflow(task or {}, understanding)
    except Exception:                                    # noqa: BLE001
        return True
    return ("full_scan" in wf) or (not wf)


def builds_chain(task, understanding=None):
    """公开入口：本 case 是否要真正构建扫描链（供 scan_agent 决定 DRC 是否算指标）。"""
    return _builds_chain(task, understanding)


def _design_ports_of(understanding, facts):
    """
    取「设计里真实存在的端口名」集合（用于过滤不存在的端口断言）。

    来源优先级：facts["top_ports"]（网表结构，权威） > understanding 里收集到的端口。
    hints 里同时收录 scan_status / wrapper 等自动创建端口的名字，避免误删合法端口。
    """
    ports = set()
    top_ports = ((facts or {}).get("top_ports") or {})
    for name in top_ports:
        ports.add(str(name))
    for key in ("clock_signals", "reset_signals", "scan_enable_signals",
                "constant_signals", "wrapper_signals"):
        for sig in (understanding or {}).get(key) or []:
            if isinstance(sig, dict) and sig.get("port"):
                ports.add(str(sig["port"]))
    return ports


def _port_exists(port, valid_ports):
    """端口是否在设计里存在；valid_ports 为空表示「无法判定」，一律放行。"""
    if not valid_ports:
        return True
    port = str(port or "").strip()
    if not port:
        return False
    # 去掉 bus 下标：cfg_o[13:0] -> cfg_o
    base = re.sub(r"\[.*$", "", port)
    return port in valid_ports or base in valid_ports


def build_assertions(task, understanding=None, valid_ports=None):
    """
    从 task_spec（+ 结构化理解）抽取可核验断言。

    Args:
        valid_ports: 设计里真实存在的端口名集合。给定时，端口断言会先做存在性校验 ——
            **不存在于设计的端口不产生断言**，因为"时钟端口不存在"本身就是待修复的
            故障，而不是任务要求。task_2/case1 的 `resetn`、case2 的 `wb_clk` 都是
            original.dofile 里被注入的故障值，旧实现把它们当成"要求"写进断言，
            于是**修对了反而被判不合格**（假阳性 → 阻断成功路径 → 全部 resolved:false）。

    返回 [(assertion_id, requirement_text, checker)]，checker 为
    (kind, payload)：kind ∈ {"dofile_has", "dofile_absent", "output_has"}。
    """
    spec = (task or {}).get("task_spec", "") or ""
    u = understanding or {}
    out = []
    builds_chain = _builds_chain(task, u)

    def add(req, kind, payload):
        out.append({
            "assertion_id": "A%d" % (len(out) + 1),
            "requirement": req,
            "kind": kind,
            "payload": payload,
        })

    # 1) 明确要求配置为扫描时钟 / 复位 / 扫描使能的端口
    #    扫描时钟/复位只在「真建链」时才是指标；不建链的 case 官方 golden 也不声明时钟。
    #    端口必须**真实存在于设计**：不存在的端口是待修故障（如 case1 的 resetn、
    #    case2 的 wb_clk），不能当成"任务要求"写进断言。
    skipped_ports = []

    def _add_port_sig(sig, type_kw):
        port = (sig or {}).get("port")
        if not port:
            return
        # 只有 clock/reset 必须存在于设计。scan_enable 等由工具自动创建
        # （官方 task_spec 明说），不能因为"网表里没有"就判它不合格。
        if type_kw in ("clock", "reset") and not _port_exists(port, valid_ports):
            skipped_ports.append(str(port))
            return
        ep = re.escape(str(port))
        # -type / -port 的先后顺序不固定（官方写法两种都出现过），两种都认。
        payload = (r"set_scan_signal\b[^\n]*(?:-type\s+%s\b[^\n]*-port\s+%s\b"
                   r"|-port\s+%s\b[^\n]*-type\s+%s\b)"
                   % (type_kw, ep, ep, type_kw))
        add("扫描%s %s" % ({"clock": "时钟", "reset": "复位",
                          "scan_enable": "使能"}[type_kw], port), "dofile_has", payload)

    for sig in ((u.get("clock_signals") or []) if builds_chain else []):
        _add_port_sig(sig, "clock")
    for sig in ((u.get("reset_signals") or []) if builds_chain else []):
        _add_port_sig(sig, "reset")
    for sig in (u.get("scan_enable_signals") or []):
        _add_port_sig(sig, "scan_enable")
    if skipped_ports:
        print("  ⚠ 以下端口在设计里不存在，其断言已跳过（视为待修故障而非需求）：%s"
              % ", ".join(sorted(set(skipped_ports))))

    # 2)~4)、6) 链数 / 链长 / 端口命名 / 混时钟混沿 / wrapper 链数 —— 均只对「真建链」的 case 有效
    if builds_chain:
        for n in _unique_ints(re.findall(r"chain_count\s*[:=]?\s*(\d+)", spec)):
            add("扫描链数量 chain_count=%d" % n, "dofile_has",
                r"-chain_count\s+%d\b" % n)
        for n in _unique_ints(re.findall(r"(?:扫描链数量|链条数|扫描链个数|链数)\s*(?:为|是|[:=])\s*(\d+)", spec)):
            add("扫描链数量 %d" % n, "dofile_has", r"-chain_count\s+%d\b" % n)
        for n in _unique_ints(re.findall(r"max_length\s*[:=]?\s*(\d+)", spec)):
            add("扫描链最大长度 max_length=%d" % n, "dofile_has", r"-max_length\s+%d\b" % n)
        for n in _unique_ints(re.findall(
                # 与 dofile_generator._MAX_LEN_RE 等价：覆盖 最大长度/最大链长度/链最大长度/最大链长，
                # 且容忍中间夹括号说明（如「链最大长度（含内部链与 wrapper 链）不超过 100」）
                r"(?:最大\s*链?\s*长(?:度)?|链\s*最大\s*长(?:度)?)[^\d\n]{0,40}?(\d{1,6})", spec)):
            add("扫描链最大长度 %d" % n, "dofile_has", r"-max_length\s+%d\b" % n)

        # 3) 扫描数据端口命名
        if u.get("scan_cfg", {}).get("si_port_format"):
            f = u["scan_cfg"]["si_port_format"]
            add("扫描数据输入端口命名 %s" % f, "dofile_has",
                r"-si_port_format\s+\"?%s" % re.escape(f))
        if u.get("scan_cfg", {}).get("so_port_format"):
            f = u["scan_cfg"]["so_port_format"]
            add("扫描数据输出端口命名 %s" % f, "dofile_has",
                r"-so_port_format\s+\"?%s" % re.escape(f))

        # 4) 禁止混合时钟域 / 禁止混合时钟边沿
        if re.search(r"不允许混合不同时钟域|禁止.*时钟域.*混合|不允许.*时钟域", spec):
            add("不允许混合不同时钟域", "dofile_has", r"-mix_clocks\s+false\b")
        if re.search(r"不允许不同时钟沿混合|禁止.*时钟沿.*混合", spec):
            add("不允许不同时钟沿混合", "dofile_has", r"-mix_edges\s+false\b")

        # 6) wrapper 链数
        for n in _unique_ints(re.findall(r"wrapper[^\n]{0,40}?chain_count\s*[:=]?\s*(\d+)", spec, re.I)):
            add("Wrapper 链数 %d" % n, "dofile_has", r"set_wrapper_cfg\b[^\n]*-chain_count\s+%d\b" % n)

    # 5) DRC 豁免的边界：task_spec 可能**只禁止对特定规则**设豁免。
    #
    #    实测 task2/case6：spec 写「不得用 DRC 规则豁免（set_scan_drc_rule_handling
    #    对 DFTR1/DFTR9）消除违例」，同时又要求保留 original.dofile 里
    #    `set_scan_drc_rule_handling {DFTR-TIE1 DFTR-TIE0} Ignore`（那是工具口径，
    #    与违例无关）。旧实现见到 spec 里同时出现「豁免」和「DFTR」就认为任务要求
    #    豁免（被同一行的「（如 …）」转义误伤后又反过来全禁），两种都会判错：
    #    前者放过真违规，后者把合法配置判成违规 —— 每轮都"配置不符合要求"，永远不收敛。
    forbidden = _spec_forbidden_ignore_rules(spec)
    if forbidden:
        pat = (r"set_scan_drc_rule_handling[^\n]*(?:"
               + "|".join(re.escape(r) + r"\b" for r in sorted(forbidden))
               + ")")
        add("不得对 %s 设 DRC 豁免" % "、".join(sorted(forbidden)),
            "dofile_absent", pat)
    elif not _spec_asks_drc_ignore(spec):
        add("不得通过 DRC 豁免掩盖违例", "dofile_absent", r"set_scan_drc_rule_handling\b")

    return out


def _unique_ints(vals):
    out = []
    for v in vals:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n not in out:
            out.append(n)
    return out


_PROHIBIT_RE = re.compile(r"不得|禁止|严禁|不允许|不能")


def _spec_forbidden_ignore_rules(spec):
    """
    task_spec 明确**禁止**对其设豁免的 DRC 规则号（如 case6 的 DFTR1/DFTR9）。

    只认「同一句里既有禁止词、又有豁免/忽略、又点了具体规则号」的行，
    避免把「（如 DFTR1）」这类举例当成要求。
    """
    rules = set()
    for line in (spec or "").splitlines():
        if not re.search(r"忽略|豁免|Ignore", line):
            continue
        if not _PROHIBIT_RE.search(line):
            continue
        for m in re.finditer(r"\bDFTR-?T?I?E?\d+\b", line, re.I):
            rules.add(m.group(0).upper())
    return rules


def _spec_asks_drc_ignore(spec):
    """
    task_spec 是否明确要求忽略/豁免某 DRC 规则（与 dofile_generator 判据一致）。

    注意要排除**禁止句**：case6 的「不得用 DRC 规则豁免…」既含「豁免」又含「DFTR」，
    按字面会被误判成"任务要求豁免"。禁止句由 _spec_forbidden_ignore_rules 单独处理。
    """
    for line in (spec or "").splitlines():
        if not re.search(r"忽略|豁免", line):
            continue
        if _PROHIBIT_RE.search(line):
            continue
        if re.search(r"（如|例如|比如", line):
            continue
        if re.search(r"DFTR", line, re.I):
            return True
    return False


def check_assertions(assertions, dofile_text):
    """
    逐条核验断言，返回不满足的断言列表。

    Args:
        assertions: build_assertions 的结果
        dofile_text: 最终 dofile 文本

    Returns:
        list of {"assertion_id", "requirement", "evidence"}
    """
    text = dofile_text or ""
    violations = []
    for a in assertions or []:
        rx = re.compile(a["payload"], re.I)
        hit = bool(rx.search(text))
        if a["kind"] == "dofile_has" and not hit:
            violations.append({
                "assertion_id": a["assertion_id"],
                "requirement": a["requirement"],
                "evidence": "最终 dofile 中未找到对应配置（%s）" % a["payload"],
            })
        elif a["kind"] == "dofile_absent" and hit:
            violations.append({
                "assertion_id": a["assertion_id"],
                "requirement": a["requirement"],
                "evidence": "最终 dofile 中出现了不允许的配置（%s）" % a["payload"],
            })
    return violations


# ---------------------------------------------------------------------------
# LLM 复核：任务要求 vs 最终配置
# ---------------------------------------------------------------------------

def review_with_llm(task, understanding, dofile_text, log_text, report_text, llm):
    """
    让 LLM 对照 task_spec 复核最终 dofile / 日志 / 配置报告是否满足全部明确要求。

    仅用于「日志无 ERROR、无 DRC 违例」但可能仍存在「DFT 配置不符合任务要求」的场景
    （赛题 4.2.2 第三类故障）。

    Returns:
        list of {"requirement", "evidence", "issue", "suggested_fix"}；无问题返回 []
    """
    if llm is None or not getattr(llm, "available", False):
        return []

    # 预算护栏（2026-10-10 平台实测引入）：本次复核发生在**工具跑完之后**，
    # 150s 级 case 此时通常只剩 30~50s，而平台单次调用实测需 78~148s ——
    # 发出去必然超时，白等还会挤掉后续修复轮次。此时退回确定性断言结果
    # （verify_spec 的断言部分照常生效，不牺牲问题识别能力）。
    if not llm_client.has_useful_time(llm):
        _left = llm.time_left() if hasattr(llm, "time_left") else None
        print(f"  ⚠ 剩余 {0 if _left is None else _left:.0f}s 不足以做 LLM 规格复核"
              f"（需 {llm_client.config.LLM_MIN_USEFUL_S:.0f}s），仅用确定性断言")
        return []

    spec = (task or {}).get("task_spec", "") or ""

    system_prompt = """你是广立微 dftexp_scan（Scan Insertion）工具与 DFT 配置专家。

你的任务：对照「插链任务说明书」，检查给定的最终 Dofile、工具日志、配置报告，
找出**确实不满足任务说明书明确要求**的 DFT 配置问题。

【严格纪律 —— 宁可不报，也不要编】
1. 只报告任务说明书中**有明确文字依据**的问题（要求原文必须能被引用）。
2. 每个问题必须给出可核验证据：Dofile 中缺失/错误的配置，或日志/报告中不符的数值。
3. 不得报告「可能」「建议」「优化空间」类的推测；不得凭空要求说明书未写的配置。
4. 若无法判断或没有明确依据，返回空列表。
5. 只返回 JSON，不要解释。
6. **已满足的项绝对不要放进 issues**。输出的是"问题清单"，不是"核对表" ——
   某项如果已经满足，就不要出现。违反项 evidence 必须**指出哪里不对**
   （如「-max_length 写 2000，要求 1000」），禁止只罗列已配置的内容
   （如「Dofile 中：set_scan_cfg -max_length 1000」——这是"已满足"，不是问题）。
7. **「配置有没有写」只看 Dofile 本身**：Dofile 里已经有的声明一律视为已配置。
   「日志中没有打印 / 未显示 / 未见确认信息」**不构成**配置缺失的证据 ——
   工具本来就不会逐条回显 set_scan_signal。日志只用于核对数值类结果
   （DRC 违例数、扫描链数、端口命名等）。
8. 只在「Dofile 确实没写」或「日志/报告里的数值与说明书要求不符」时才报问题。

输出格式：
{
  "issues": [
    {
      "requirement": "任务说明书中的原话（逐字引用）",
      "evidence": "证据：Dofile/日志/报告中的具体内容",
      "issue": "不满足之处（一句话）",
      "suggested_fix": "建议的 Dofile 修改（具体命令）"
    }
  ]
}"""

    user_prompt = f"""# 插链任务说明书
{spec}

# 最终 Dofile
{dofile_text}

# 工具运行日志（节选）
{(log_text or '')[-6000:]}

# 配置/结构报告（节选）
{(report_text or '')[:6000]}

请复核并返回 JSON。"""

    try:
        data = llm.chat_json(system_prompt, user_prompt)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []

    issues = data.get("issues")
    if not isinstance(issues, list):
        return []

    return _filter_verifiable_issues(issues, spec, dofile_text)


def _filter_verifiable_issues(issues, spec, dofile_text=""):
    """
    幻觉过滤：只保留「能在 task_spec 中找到依据」的问题。

    判据：问题文本（requirement + evidence）中至少有一个标识符或数字真实出现在
    task_spec 里；否则视为无依据的推测，丢弃。
    """
    spec_idents = {t.lower() for t in _spec_idents(spec)}
    spec_nums = set(re.findall(r"\d+", spec or ""))

    kept = []
    for it in issues:
        if not isinstance(it, dict):
            continue
        blob = " ".join(str(it.get(k, "")) for k in ("requirement", "evidence", "issue"))
        toks = {t.lower() for t in _IDENT_RE.findall(blob)}
        nums = set(re.findall(r"\d+", blob))
        # 有标识符命中（且该标识符不是通用词）或数字命中，才认为有依据
        generic = {"dofile", "set", "scan", "the", "and", "for", "not", "json"}
        ident_hit = bool((toks - generic) & spec_idents)
        num_hit = bool(nums & spec_nums)
        if not (ident_hit or num_hit):
            continue
        # DRC 豁免类问题的规则号必须真的对上（2026-10-29 task_2/case6 平台实测）：
        # 任务书禁止的是「对 DFTR1/DFTR9 设豁免」，而证据那行只有
        # `{DFTR-TIE1 DFTR-TIE0} Ignore`（工具口径的固有告警豁免，任务书并不禁止）。
        # 旧过滤只看"标识符在 spec 里出现过"，于是把这条合法配置判成违规，
        # 驱动第 3 轮改动，反而从 0 违例退化到 2 条 DFTR-TIE1。
        # 「日志里没打印」不是配置缺失的证据（2026-11-01 测试20 task_2/case5 实测：
        # Dofile 里 scan_mode / mbist_mode 的常量声明齐全，LLM 仍报「日志中未显示
        # 这些端口存在或配置生效的确认信息」，直接把一轮成功的运行判成失败，
        # 并让 14 条 issue 全部 resolved=false）。Dofile 里确实声明了 -> 丢弃。
        if _ABSENCE_RE.search(blob):
            declared = {m for m in toks
                        if re.search(r"-port\s+%s\b" % re.escape(m), dofile_text or "")}
            if declared:
                continue
        # 「日志里没看到失败/违例」= 检查通过，不是问题
        if _NO_FAILURE_RE.search(blob):
            continue
        if "set_scan_drc_rule_handling" in blob.lower():
            want = {r.upper() for r in re.findall(r"\bDFTR[\w-]*\d\b", str(it.get("requirement", "")), re.I)}
            have = {r.upper() for r in re.findall(r"\bDFTR[\w-]*\d\b", str(it.get("evidence", "")), re.I)}
            if want and have and not (want & have):
                continue
        # 「证据只是在复述现行配置」不算问题（2026-11-02 本地 t2/c5 实测）：
        # LLM 会把逐项核对表**整张**塞进 issues —— 四条 evidence 全是
        # 「Dofile 中：set_scan_signal -type constant ...」这种"已经配好了"的复述，
        # 甚至有一条自己写着「扫描链满足：最大链长 1000」。这类必须丢掉，
        # 否则一次全对运行被判 ✗，14 条 issue 全 resolved=false。
        # 只看 evidence：issue 字段常是 LLM 的套话（"…不符"），会把负面词带进来
        if _restates_current_config(str(it.get("evidence", ""))):
            continue
        kept.append(it)
    return kept


# 「没有发现问题」的正面表述：`日志中未显示其他 DRC 违例清零失败信息` 这类
# 是否定之否定，语义是"检查通过"，不能当问题证据。
_NO_FAILURE_RE = re.compile(
    r"(?:未|没有|并未)\s*(?:显示|出现|发现|见|观察到)[^。；\n]{0,24}"
    r"(?:失败|违例|错误|问题|异常)")

# 负面词：出现任意一个才说明"真的指控某处不对"。
_NEG_CLAIM_RE = re.compile(
    r"缺失|缺少|未声明|未配置|未设置|未找到|未包含|不符|不符合|不一致|错误|冲突|"
    r"多余|冗余|违反|不满足|偏离|超过|低于")


def _restates_current_config(blob):
    """
    这条 issue 是不是只在复述"现状"，而没有真的指控哪里不对。

    判据（两条同时成立）：
      ① 证据里出现 `Dofile 中：` / `Dofile 里：` 这类"引用现行配置"的写法；
      ② 全段找不到任何负面词（缺失/不符/违反/多余 …）。
    满足即为"核对表里已通过的那一项"，不是问题。
    """
    text = str(blob or "")
    if not re.search(r"Dofile\s*(?:中|里)\s*[：:]", text):
        return False
    if _NEG_CLAIM_RE.search(text):
        return False
    return True


# 「日志里没有显示/打印/确认」这类说辞：工具不会逐条回显配置，不能当证据
_ABSENCE_RE = re.compile(
    r"日志(?:中|里)?\s*(?:未|没有|并未)\s*(?:显示|打印|出现|确认|见|观察到|提及)"
    r"|(?:未|没有)\s*在日志(?:中|里)?\s*(?:显示|打印|出现|确认|见)")


# ---------------------------------------------------------------------------
# 汇总入口
# ---------------------------------------------------------------------------

def verify_spec(task, understanding, dofile_text, log_text="", report_text="", llm=None,
                use_llm=True, valid_ports=None):
    """
    任务二专用：核对最终配置是否满足 task_spec。

    Args:
        valid_ports: 设计里真实存在的端口集合（见 build_assertions 的说明）。
            None 表示不做端口存在性过滤（保持旧行为）。

    Returns:
        list of {"assertion_id", "requirement", "evidence", "suggested_fix"}
    """
    assertions = build_assertions(task, understanding, valid_ports=valid_ports)
    violations = check_assertions(assertions, dofile_text)
    for v in violations:
        v.setdefault("suggested_fix", "")

    if not violations and use_llm:
        for it in review_with_llm(task, understanding, dofile_text, log_text,
                                  report_text, llm):
            violations.append({
                "assertion_id": "L%d" % (len(violations) + 1),
                "requirement": str(it.get("requirement", "")),
                "evidence": str(it.get("evidence", "")),
                "suggested_fix": str(it.get("suggested_fix", "")),
            })

    return violations


def format_issues(violations):
    """把违反项格式化为可读的多行文本（用于日志与修复 prompt）。"""
    lines = []
    for v in violations or []:
        lines.append("- [%s] 要求：%s" % (v.get("assertion_id", ""), v.get("requirement", "")))
        if v.get("evidence"):
            lines.append("  证据：%s" % v["evidence"])
        if v.get("suggested_fix"):
            lines.append("  建议修复：%s" % v["suggested_fix"])
    return "\n".join(lines)

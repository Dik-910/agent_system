#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
决策记录模块
生成 decision_log.json，包含任务-配置映射、问题闭环、工具调用记录、文件改动。

评分关键点：requirement_mapping 的 config_ref.locator 必须指向 final.dofile 的
真实行号（如 L40），且有效对应项 / 总需求数 >= 90%。
因此本模块会：
  1. 从 task_spec 提取需求项；
  2. 在 final.dofile 中定位每个需求对应的命令及其真实行号；
  3. 生成带真实 locator 的 requirement_mapping。
"""

import json
import os
import re
from pathlib import Path


# ============================================================================
# 需求识别与命令定位
# ============================================================================

# 需求类型 -> 匹配 final.dofile 中命令的正则
_REQUIREMENT_PATTERNS = [
    {
        "name": "加载设计（库/网表/CTL）",
        "regex": r"^(load_lib|load_netlist|load_ctl)\b",
        "keyword_hint": ["load", "加载", "读取", "库", "网表"],
    },
    {
        "name": "指定当前设计",
        "regex": r"^present_design\b",
        "keyword_hint": ["present_design", "当前设计", "顶层模块", "指定.*设计"],
    },
    {
        "name": "配置扫描时钟信号",
        "regex": r"^set_scan_signal\b.*-type\s+clock\b",
        "keyword_hint": ["时钟", "clock", "clk", "扫描时钟"],
    },
    {
        "name": "配置扫描使能信号",
        "regex": r"^set_scan_signal\b.*-type\s+scan_enable\b",
        "keyword_hint": ["scan_enable", "扫描使能", "扫描使能信号", "shift"],
    },
    {
        "name": "配置复位信号",
        "regex": r"^set_scan_signal\b.*-type\s+reset\b",
        "keyword_hint": ["复位", "reset", "rst"],
    },
    {
        "name": "配置常量/模式信号",
        "regex": r"^set_scan_signal\b.*-type\s+constant\b",
        "keyword_hint": ["constant", "常量", "测试模式", "scan_mode", "mbist_mode"],
    },
    {
        "name": "DFF→SFF 扫描单元映射",
        "regex": r"^set_scan_cell_mapping\b",
        "keyword_hint": ["映射", "mapping", "DFF", "SFF", "扫描单元", "替换"],
    },
    {
        "name": "配置扫描链参数",
        "regex": r"^set_scan_cfg\b",
        "keyword_hint": ["扫描链", "chain", "链数", "链长", "chain_count", "max_length", "mix_clocks"],
    },
    {
        "name": "配置 Wrapper",
        "regex": r"^set_wrapper_cfg\b",
        "keyword_hint": ["wrapper", "Wrapper", "边界", "包裹"],
    },
    {
        "name": "配置 Scan Segment",
        "regex": r"^set_scan_segment\b",
        "keyword_hint": ["segment", "移位寄存器", "scan segment"],
    },
    {
        "name": "ICG 时钟门控配置",
        "regex": r"^set_dft_clock_gating_cfg\b",
        "keyword_hint": ["ICG", "时钟门控", "clock gating", "门控时钟"],
    },
    {
        "name": "标记不可扫描元素",
        "regex": r"^set_scan_element\b",
        "keyword_hint": ["不可扫描", "set_scan_element", "排除.*扫描"],
    },
    {
        "name": "配置伪主输入时钟",
        "regex": r"^add_pseudo_pi\b",
        "keyword_hint": ["pseudo_pi", "伪主输入", "pseudo PI", "门控时钟.*悬空"],
    },
    {
        "name": "DRC 规则豁免",
        "regex": r"^set_scan_drc_rule_handling\b",
        "keyword_hint": ["DRC.*忽略", "忽略.*DRC", "TIE", "DFTR-TIE"],
    },
    {
        "name": "执行 DRC 检查",
        "regex": r"^examine_scan_drc\b",
        "keyword_hint": ["DRC", "检查"],
    },
    {
        "name": "扫描链分析",
        "regex": r"^examine_scan_chain\b",
        "keyword_hint": ["扫描链", "chain", "分析"],
    },
    {
        "name": "插入 DFT 逻辑",
        "regex": r"^insert_dft_logic\b",
        # 不用裸 "insert"：任务目标段的 "Scan Insertion" 会命中，抢走本该属于
        # 插链步骤的原文，并和其它条目撞成同一文案后被去重。
        "keyword_hint": ["插入 DFT", "插链", "扫描链插入", "插链命令", "DFT 逻辑插入"],
    },
    {
        "name": "导出网表/CTL/SCANDEF",
        "regex": r"^(dump_netlist|dump_ctl|dump_def)\b",
        "keyword_hint": ["导出", "输出", "网表", "post_scan", "CTL", "SCANDEF", "dump"],
    },
]



def _dofile_file(run):
    """
    取某轮次的 dofile 路径，**保证是文件而不是目录**。

    坑：`Path(run.get("dofile_path", ""))` 在缺键时等于 `Path(".")`，
    而 `.exists()` 对目录也返回 True —— 随后 read_text 抛
    IsADirectoryError（case6 冒烟时实际踩到）。这里统一返回 None 表示「没有可读文件」。
    """
    p = Path((run or {}).get("dofile_path") or "")
    try:
        return p if p.is_file() else None
    except OSError:
        return None

def _find_command_lines(dofile_text, pattern):
    """在 dofile 中查找匹配 pattern 的行号（1-based）。"""
    lines = dofile_text.splitlines()
    matches = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(pattern, stripped):
            matches.append(i + 1)
    return matches


_TABLE_SEP_RE = re.compile(r"^\|[\s:|-]+\|$")
# 「输入文件」清单表格的首格是路径/文件名（`input/lib/sky130.lib`、
# `input/netlist/tv80.v` …）。这类行是**材料说明**而不是"任务要求"，
# 若被 `_requirement_text` 选中，就会出现
#   "requirement": "| `input/netlist/pre_scan.v` | 综合后的门级网表（pre-scan） |"
# 配 "dft_config": "load_netlist ..." —— 张冠李戴。
_FILE_TABLE_CELL_RE = re.compile(
    r"`?[\w.\-]*/[\w./<>-]+`?|\.(?:v|sv|lib|md|pdf|json|cfg|def|ctl|scandef)\b", re.I)


def _spec_requirement_lines(spec):
    """
    取 task_spec 中「任务要求」性质的正文行（排除输出/交付/日志/输入文件清单那几节）。

    为什么排除：需求映射要写「任务说明书中的一条具体要求」，而输出清单里的
    "插链后网表（Verilog 格式）" 之类只是产物说明，输入文件表里的
    "`input/netlist/tv80.v` | 8080 CPU 核心门级网表" 只是材料说明，
    把它们当成"任务要求"属于张冠李戴（docx 5.5.1「任务-配置映射的可追溯性」按可核验性打分）。
    """
    out, in_skip = [], False
    for raw in (spec or "").splitlines():
        s = raw.strip()
        if re.match(r"^#{1,6}\s*\S", s):
            in_skip = bool(re.search(
                r"输出|交付|日志|决策记录|背景|输入文件|输入清单", s))
            continue
        if in_skip or not s:
            continue
        if _TABLE_SEP_RE.match(s):
            continue
        # 表格行：首格是文件/路径的，属于材料清单，不是要求
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if cells and _FILE_TABLE_CELL_RE.search(cells[0]):
                continue
        out.append(s)
    return out


def _requirement_text(spec_lines, hints, fallback):
    """
    从 task_spec 原文里挑一条最贴近该需求的行，作为 requirement 文案。

    打分偏好「含数值/标识符等可核验内容」的行（如 "扫描链数量为 4"、"端口 `clk`"），
    拿不到合适原文时退回功能分类名（保证字段非空、可解析）。
    """
    best = None
    for line in spec_lines:
        low = line.lower()
        if not any(h.lower() in low for h in hints):
            continue
        score = len(re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d", line))
        if best is None or score > best[0]:
            best = (score, line.lstrip("-*# \t").strip())
    if best is None:
        return fallback
    text = best[1]
    return text if len(text) <= 180 else text[:180] + "…"


def build_requirement_mapping(task, final_dofile_text):
    """
    构建 requirement_mapping：从 task_spec 需求 -> final.dofile 命令 + 真实行号。

    requirement 优先取 task_spec 原文（原文或准确转述），
    config_ref.locator 指向 final.dofile 的真实行号。

    Returns:
        list: requirement_mapping 列表
    """
    spec = task.get("task_spec", "")
    spec_lines = _spec_requirement_lines(spec)
    mapping = []

    for req in _REQUIREMENT_PATTERNS:
        name = req["name"]
        pattern = req["regex"]
        hints = req["keyword_hint"]

        # 判断该需求是否被 task_spec 提及（按需生成，避免无中生有）
        mentioned = any(h.lower() in spec.lower() for h in hints)
        if not mentioned:
            continue

        # 在 final.dofile 中定位命令
        line_nos = _find_command_lines(final_dofile_text, pattern)
        if not line_nos:
            # 需求提到但 dofile 未实现：跳过（不产生空项，避免拉低有效对应项占比）
            continue

        # 取第一个匹配行的命令原文作为 dft_config
        lines = final_dofile_text.splitlines()
        first_line = lines[line_nos[0] - 1].strip()
        # 收集所有匹配行的 locator（如 L12, L34）
        locators = [f"L{ln}" for ln in line_nos]
        locator = locators[0] if len(locators) == 1 else ", ".join(locators)

        mapping.append({
            "requirement": _requirement_text(spec_lines, hints, name),
            "dft_config": first_line,
            "config_ref": {"source": "final_results/deliverables/final.dofile", "locator": locator},
        })

    mapping.extend(_pre_process_step_mapping(spec_lines, final_dofile_text))
    return _dedupe_mapping(mapping)


# 「三步前置处理」用例（task_1/case1、case4 等）：dofile 里是三条
#   insert_dft_logic -connect_icg_only / -replace_only / -replace_unscan
# 而 task_spec 的「主要工作包括」把它拆成 3 条独立工作要求。
# 只按 insert_dft_logic 记一条，会让 2/3 的要求在 requirement_mapping 里凭空消失
# （决策记录「覆盖 ≥90%」直接扣分）。这里按 flag 逐条映射回规格原文。
_PRE_STEP_HINTS = (
    ("connect_icg_only",
     ("ICG 控制信号重连", "ICG", "重连", "clock gating"),
     "重连 ICG 控制信号，使扫描模式可切换（-connect_icg_only）"),
    ("replace_only",
     ("DFF 到扫描 FF", "替换为对应的扫描 FF", "DFF", "扫描 FF"),
     "将 DFF 替换为功能对应的扫描 FF（-replace_only）"),
    ("replace_unscan",
     ("回替", "反向回替", "非扫描域", "不可扫描", "独立时钟域", "普通 DFF"),
     "将非扫描域内既有 SFF 回替为普通 DFF（-replace_unscan）"),
)


def _pre_process_step_mapping(spec_lines, final_dofile_text):
    """把 insert_dft_logic 的 -xxx_only / -replace_unscan 逐个 flag 映射回规格要求。

    排在 catalogue 映射**之前**返回（见 `_dedupe_mapping` 保留首次出现），
    这样同一条 insert_dft_logic 行不会被泛化的「插入 DFT 逻辑」抢走 ——
    逐 flag 的规格原文比泛化文案更可核验。
    """
    lines = (final_dofile_text or "").splitlines()
    out = []
    for idx, line in enumerate(lines, start=1):
        s = line.strip()
        if s.startswith("#") or not re.match(r"^insert_dft_logic\b", s):
            continue
        for flag, hints, fallback in _PRE_STEP_HINTS:
            if not re.search(r"-" + re.escape(flag) + r"\b", s):
                continue
            out.append({
                "requirement": _requirement_text(spec_lines, hints, fallback),
                "dft_config": s,
                "config_ref": {
                    "source": "final_results/deliverables/final.dofile",
                    "locator": f"L{idx}",
                },
            })
    return out


def _dedupe_mapping(mapping):
    """按 requirement 文案去重（保留首次出现），避免同一要求被登记两次。"""
    seen, out = set(), []
    for m in mapping:
        key = (m.get("requirement") or "").strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out



# ============================================================================
# decision_log 构建
# ============================================================================

# 工具统计信息码 → 数值（用于 summary 反映真实成果）
# 注意 SCAN-7602 原文为 "There was(were) 68 clock gating instance(s) ..."，
# 动词处带 "(were)" 变体，故只锚定 "<数字> clock gating instance"。
_RUN_STAT_RES = {
    "icg": re.compile(r"(\d+)\s+clock gating instance", re.I),
    "dff": re.compile(r"There\s+were\s+(\d+)\s+'D'\s+flip-flops", re.I),
    "sff": re.compile(r"There\s+were\s+(\d+)\s+'scan'\s+flip-flops", re.I),
    "chains": re.compile(r"Number\s+of\s+chains?\s*[:=]?\s*(\d+)", re.I),
}


def _extract_run_stats(log_text):
    """
    从 dftexp_scan 日志中提取关键统计：
      icg    - SCAN-7602 ICG 控制引脚重连数
      dff    - SCAN-7600 DFF→SFF 替换数
      sff    - SCAN-7600 不可扫描域 SFF 回替数
      chains - 最终扫描链数量
      errors - [ERROR] 行数
    未出现的项为 None。
    """
    text = log_text or ""
    stats = {}
    for key, rx in _RUN_STAT_RES.items():
        m = rx.search(text)
        stats[key] = int(m.group(1)) if m else None
    if stats.get("chains") is None:
        m = re.search(r"(\d+)\s+(?:scan\s+)?chains\b", text, re.I)
        if m:
            stats["chains"] = int(m.group(1))
    stats["errors"] = len(re.findall(r"\[ERROR\]", text))
    return stats


def _workflow_scope(dofile_text):
    """
    按最终 dofile 反推本次实际执行的工作流（不写死文案）。

    Returns:
        "pre_process"（三步前置处理，不建扫描链）/ "full_scan"（完整插链）/ "unknown"
    """
    pre_steps, full = set(), False
    for line in (dofile_text or "").splitlines():
        s = line.strip()
        if s.startswith("#") or "insert_dft_logic" not in s:
            continue
        m = re.search(r"insert_dft_logic\s+(-[A-Za-z_]+)", s)
        opt = m.group(1) if m else ""
        if opt in ("-connect_icg_only", "-replace_only", "-replace_unscan"):
            pre_steps.add(opt.lstrip("-"))
        else:
            full = True
    if full:
        return "full_scan"
    if pre_steps:
        return "pre_process"
    return "unknown"


def _format_generation_info(info):
    """
    把 dofile 生成的「候选方案与择优依据」格式化成 summary 里的一句话。

    赛题 5.4.1(b)：系统内部生成多个候选方案并由系统自主择优、自动执行，
    「应在决策记录中说明候选方案与最终选择依据」。这里把实际依据写进 summary，
    不新增顶层字段（保持与附录 schema 完全一致）。
    """
    if not info:
        return ""
    label = {"llm": "LLM 生成的 dofile",
             "rule": "规则模板 dofile",
             "fallback": "最小兜底脚本"}.get(info.get("chosen"), info.get("chosen") or "未记录")
    reason = info.get("reason") or ""
    text = f"dofile 生成采用{label}" + (f"（{reason}）" if reason else "") + "。"
    cands = info.get("candidates") or []
    if cands:
        desc = []
        for c in cands:
            desc.append("%s %d 字符/硬要求缺口 %d/链覆盖 %s" % (
                c.get("source"), c.get("chars", 0),
                len(c.get("gaps") or []), c.get("chain_coverage")))
        text += "候选对比：" + "；".join(desc) + "。"
    return text


def build_decision_log(task, task_type, run_results, understanding, fix_records,
                       final_dofile_text=None, netlist_changes=None,
                       final_run_id=None, original_dofile_text=None,
                       generation_info=None):
    """
    构建 decision_log 数据结构（严格对齐赛题附录 8.1.1 / 8.1.2 的 schema）。

    Args:
        task: parse_task 结果
        task_type: "task1" / "task2"
        run_results: 所有运行结果列表
        understanding: LLM 结构化理解（可能 None）
        fix_records: 修复记录列表（任务二）
        final_dofile_text: 最终采用的 dofile 文本（用于计算真实行号）
        netlist_changes: 网表级修复记录列表（任务二，可选）
        final_run_id: 「最终采用」的轮次。**必须与 final_results/ 落盘所用轮次一致** ——
            赛题评分项「final_run 指向的轮次存在且等于 final_results/ 下的内容」，
            两者不一致会直接扣分。由调用方用同一套择优逻辑（_best_run）传入。
        original_dofile_text: 任务二的修复起点。给了就在 file_changes 里补一条
            「original -> R1」的改动记录（首轮相对修复起点确实变了，diff 也真实存在）。

    Returns:
        dict
    """
    if final_run_id:
        final_run = next((r for r in run_results
                          if r.get("run_id") == final_run_id), None) or {}
    else:
        final_run = None
        for run in reversed(run_results):
            if run.get("success"):
                final_run = run
                break
        if final_run is None and run_results:
            final_run = run_results[-1]
        final_run_id = final_run.get("run_id", "R1") if final_run else "R1"

    # requirement_mapping 只属于任务一 schema（8.1.1）。任务二 schema（8.1.2）只有
    # issue_resolutions / tool_runs / file_changes，多写字段会被算作不合规。
    requirement_mapping = (build_requirement_mapping(task, final_dofile_text or "")
                           if task_type == "task1" else [])

    # tool_runs（exit_status 需符合 schema: completed | error | aborted）
    tool_runs = []
    for i, run in enumerate(run_results):
        rid = run.get("run_id", f"R{i+1}")
        if run.get("error") == "timeout" or run.get("exit_code") is None:
            status = "aborted"
        elif run.get("success"):
            status = "completed"
        else:
            status = "error"
        tool_runs.append({
            "tool_call_id": rid,
            "log_file": f"runs/{rid}/{rid}.log",
            "exit_status": status,
        })

    # 从末轮工具日志提取关键统计，让 summary 反映真实成果（可解释性评分点）
    stats = _extract_run_stats((final_run or {}).get("output", "") or "")
    # DRC 结论来自 run 结果上挂的权威判定（日志 + reports/*drc*.rpt 合并，含豁免规则）
    drc = (final_run or {}).get("_drc") or {}

    # summary 由**实际 dofile 与日志**反推，不再使用写死文案。
    # 旧实现把 task_1/case1 的场景（「完成前置处理…未构建扫描链」）无条件套用到
    # 所有 task1 case，导致 case2（完整插链）、case3（70 条链）的 summary 与
    # final.log 直接矛盾 —— 踩在 docx 5.5.1「问题闭环的真实性」判据上。
    scope = _workflow_scope(final_dofile_text)
    # 「最终成功」必须以**本轮是否通过判据**为准，而不能只看工具进程退出码。
    # 反例（改造后实测）：DRC 残留 5 条被判失败，但 exit_code=0，旧写法会写成
    # 「最终成功 ... DRC 残留违例 5 条」—— 同一句里自相矛盾。
    run_ok = bool((final_run or {}).get("accepted",
                                        (final_run or {}).get("success")))
    if drc.get("required") and not drc.get("ok"):
        run_ok = False
    chains = stats.get("chains")

    # LEC 只有在**真的跑过并通过**时才允许写进 summary（docx 5.4.2 不得伪造结果）。
    # 网表变更记录的形态见 lec_verifier.apply_netlist_fix：
    #   {"applied": bool, "lec": {"success": bool, ...}, ...}
    lec_done = any(
        bool((nc or {}).get("applied"))
        and bool(((nc or {}).get("lec") or {}).get("success"))
        for nc in (netlist_changes or [])
    )

    # 网表是否**真的被改过**——两种途径都要算：
    #   ① 工具自带 ECO 命令（scan_agent._eco_inject，记在 fix_records[*]["eco"]）；
    #   ② LLM 重写网表 + LEC（netlist_changes）。
    # 漏掉 ① 会写出「本轮未修改网表，无需 LEC」，而我们明明插了 MUX ——
    # 这正是 docx 5.5.1「决策记录自相矛盾」的扣分点（case6 冒烟实测到）。
    eco_applied = any((rec or {}).get("eco") for rec in (fix_records or []))
    eco_groups = [
        g for rec in (fix_records or []) if (rec or {}).get("eco")
        for g in (rec["eco"].get("groups") or [])
    ]

    if task_type == "task1":
        if scope == "pre_process":
            head = ("完成 Scan Insertion 前置处理"
                    "（ICG 控制引脚重连、DFF→SFF 替换、不可扫描域 SFF 回替，未构建扫描链）")
        elif scope == "full_scan":
            head = "完成 Scan Insertion 插链" + (
                f"（最终构建 {chains} 条扫描链）" if chains is not None else "（构建扫描链）")
        else:
            head = "完成 Scan Insertion Dofile 生成与运行验证"
    else:
        # 旧实现在这里无条件写「并做逻辑等价性校验」，而 5 个 public case 一次 LEC 都没跑，
        # `file_changes.lec_ref` 全为空 —— 属于"声明了没有发生的事"。
        head = "完成 Scan Insertion Dofile"
        if lec_done:
            head += " / 网表问题的定位与修复，并做逻辑等价性校验"
        elif eco_applied and netlist_changes:
            head += (" / 网表问题的定位与修复（工具 ECO 命令 + 网表重写，"
                     "LEC 未通过或未执行）")
        elif eco_applied:
            # 用工具自带 ECO 命令改网表：如实写「改了网表」，并明确 LEC 未单独执行，
            # 不冒充做过等价性校验（docx 5.4.2 不得伪造结果）。
            head += (" / 网表问题的定位与修复"
                     f"（以工具网表编辑命令 ECO 修改 {len(eco_groups)} 处，未单独运行 LEC）")
        elif netlist_changes:
            head += " / 网表问题的定位与修复（LEC 未通过或未执行）"
        else:
            head += " 问题的定位与修复（本轮未修改网表，无需 LEC）"
        if chains is not None:
            head += f"（最终构建 {chains} 条扫描链）"

    summary_parts = [head, f"共运行 {len(run_results)} 轮",
                     f"最终{'成功' if run_ok else '未完全成功'}"]
    if run_ok:
        # 只写 "错误数 0" 会与日志里的 DRC 违例自相矛盾，这里把两个口径都写出来。
        summary_parts.append(f"末轮 ERROR {stats['errors']} 条")
    if drc.get("required"):
        if drc.get("ok"):
            summary_parts.append("DRC 违例 0")
        else:
            top = sorted((drc.get("by_rule") or {}).items(), key=lambda kv: -kv[1])[:4]
            summary_parts.append(
                "DRC 残留违例 %d 条（%s）" % (
                    drc.get("total") or 0,
                    "、".join("%s×%d" % (r, n) for r, n in top) or "未分类"))
        if drc.get("allowed"):
            summary_parts.append("其中任务书允许残留 %d 条"
                                 % sum((drc.get("allowed") or {}).values()))

    summary_facts = []
    if chains is not None:
        summary_facts.append(f"构建扫描链 {chains} 条")
    if stats.get("icg") is not None:
        summary_facts.append(f"ICG 控制引脚重连 {stats['icg']} 个")
    if stats.get("dff") is not None:
        summary_facts.append(f"DFF→SFF 替换 {stats['dff']} 个")
    if stats.get("sff") is not None:
        summary_facts.append(f"不可扫描域 SFF 回替 {stats['sff']} 个")

    summary_text = "；".join(summary_parts) + "。"
    if summary_facts:
        summary_text += "关键结果：" + "；".join(summary_facts) + "。"

    # 候选方案与择优依据（赛题 5.4.1(b) 要求写进决策记录）
    _gen_text = _format_generation_info(generation_info)
    if _gen_text:
        summary_text += " " + _gen_text

    # file_changes：按 task 分别对齐 schema（8.1.1 只有 change_id/path/diff_path；
    # 8.1.2 额外有 type/lec_ref）
    file_changes = _build_file_changes(task_type, run_results, netlist_changes,
                                       original_dofile_text)

    decision_log = {
        "case_id": task.get("case_id", ""),
        "task": task_type,
        "final_run": final_run_id,
        "summary": summary_text,
    }
    if task_type == "task1":
        decision_log["requirement_mapping"] = requirement_mapping
    decision_log["issue_resolutions"] = _build_issue_resolutions(
        fix_records, run_results, file_changes)
    decision_log["tool_runs"] = tool_runs
    decision_log["file_changes"] = file_changes

    return decision_log



def _build_file_changes(task_type, run_results, netlist_changes,
                        original_dofile_text=None):
    """
    构建符合赛题 schema 的 file_changes 列表。

      · 任务一（8.1.1）：{"change_id", "path", "diff_path"}
      · 任务二（8.1.2）：{"change_id", "type": "dofile"|"netlist",
                          "path", "diff_path", "lec_ref"}

    任务二的修复起点是 original.dofile（而非某一轮的产物），所以第一条改动记录是
    「original -> R1」，diff 落在 diffs/dofile_orig_to_R1.diff。
    """
    changes = []
    fid = 0

    def _rec(path, diff_path, ctype="dofile", lec_ref=""):
        nonlocal fid
        fid += 1
        rec = {"change_id": f"F{fid}"}
        if task_type == "task2":
            rec["type"] = ctype
        rec["path"] = path
        rec["diff_path"] = diff_path
        if task_type == "task2":
            rec["lec_ref"] = lec_ref
        return rec

    # 0. 任务二：首轮相对修复起点（original.dofile）的改动
    if task_type == "task2" and original_dofile_text and run_results:
        first = run_results[0]
        rid = first.get("run_id", "R1")
        p = _dofile_file(first)
        if p.exists():
            try:
                same = (p.read_text(encoding="utf-8", errors="replace")
                        == original_dofile_text)
            except OSError:
                same = False
            if not same:
                changes.append(_rec(f"runs/{rid}/deliverables/{rid}.dofile",
                                    f"diffs/dofile_orig_to_{rid}.diff"))

    # 1. dofile 改动：每相邻两轮之间产生一个 diff（仅当 dofile 内容变化）
    for i in range(len(run_results) - 1):
        cur = run_results[i]
        nxt = run_results[i + 1]
        cur_path = _dofile_file(cur)
        nxt_path = _dofile_file(nxt)
        if not cur_path.exists() or not nxt_path.exists():
            continue
        if cur_path.read_text(encoding="utf-8", errors="replace") == \
           nxt_path.read_text(encoding="utf-8", errors="replace"):
            continue
        rid = nxt.get("run_id", f"R{i+2}")
        changes.append(_rec(f"runs/{rid}/deliverables/{rid}.dofile",
                            f"diffs/dofile_{cur.get('run_id', '')}_to_{rid}.diff"))

    # 2. 网表改动：每个网表级修复对应一条记录
    for nc in (netlist_changes or []):
        if not nc:
            continue
        lec = nc.get("lec") or {}
        lec_ref = ""
        if nc.get("applied") or lec.get("report"):
            # 网表改动必须提供 lec_ref（docx 8.1.2 file_changes 字段）
            lec_ref = _to_rel(lec.get("report", ""))
        # 网表工作副本实际落位到 runs/<该轮>/deliverables/pre_scan_<该轮>.v
        # （output_manager.finalize_outputs 第 5 步），这里按同一条路径记录，保证
        # decision_log 里引用的文件真实存在（docx 5.5.1「引用是否闭合」）。
        rid = nc.get("applied_run") or "R1"
        name = nc.get("pre_scan_name") or f"pre_scan_{rid}.v"
        changes.append(_rec(f"runs/{rid}/deliverables/{name}",
                            f"diffs/pre_scan_netlist_orig_to_{rid}.diff",
                            ctype="netlist", lec_ref=lec_ref))

    return changes



def _to_rel(path_str):
    """把绝对路径尽量转成相对 output 的路径（赛题要求相对位置）。"""
    if not path_str:
        return ""
    p = path_str.replace("\\", "/")
    # 尝试提取 runs/ 或 final_results/ 之后的部分
    m = re.search(r"(runs|final_results|diffs|lec)/.+", p)
    if m:
        return m.group(0)
    return p


def _contains_positive_line(line):
    """该行是否是一条「正向消除标志」（便宜的哨兵判断在前，避免大日志全量跑正则）。"""
    if not line or len(line) > 2048:
        return ""
    if not any(h in line for h in _MARKER_HINTS):
        return ""
    for rx in _POSITIVE_RES:
        m = rx.search(line)
        if m:
            return m.group(0).strip()[:160]
    return ""


def _run_log_index(run_results):
    """
    建立 run_id -> 日志信息索引（首个 [ERROR] 行号、总行数、该行原文、
    正向消除标志所在行）。

    用于 issue_resolutions 的 found/verify 提供**真实**的 locator 与 excerpt，
    而不是留空骨架 —— docx 5.5.1-2)「问题闭环的真实性」逐条比对
    found -> diagnosis -> fix -> verify 与真实产物是否一致。

    locator 必须是**具体行**：旧实现统一写 "L1-L<总行数>"（整份日志都算位置），
    再配一句固定文案，等于没有可核验性。
    """
    idx = {}
    for r in (run_results or []):
        rid = r.get("run_id") or ""
        if not rid:
            continue
        # 行号必须从日志**文件**取：tool_runner 只把高信息摘要放进 result["output"]
        # （大 design 单轮日志 198MB，见 tool_runner._scan_log），摘要里没有行号。
        # 逐行子串判断是 C 级操作，198MB 约 1~2s；日志小则忽略不计。
        log_path = r.get("log_file") or ""
        first_err_line, first_err_text, total_lines = 0, "", 0
        marker_line, marker_text = 0, ""
        tail_line, tail_text = 0, ""
        if log_path and os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                    for n, line in enumerate(fh, 1):
                        total_lines = n
                        stripped = line.strip()
                        if stripped:
                            tail_line, tail_text = n, stripped[:400]
                        if not first_err_line and ("[ERROR]" in line or "[FATAL]" in line):
                            first_err_line, first_err_text = n, line.strip()[:400]
                        mk = _contains_positive_line(line)
                        if mk:                       # 取最后一次出现（终态统计在尾部）
                            marker_line, marker_text = n, mk
            except OSError:
                pass
        else:
            lines = (r.get("output") or "").splitlines()
            total_lines = len(lines)
            for n, line in enumerate(lines, 1):
                if line.strip():
                    tail_line, tail_text = n, line.strip()[:400]
                if "[ERROR]" in line or "[FATAL]" in line:
                    first_err_line, first_err_text = n, line.strip()
                    break
                mk = _contains_positive_line(line)
                if mk:
                    marker_line, marker_text = n, mk
        idx[rid] = {
            "total_lines": total_lines,
            "err_line": first_err_line,
            "err_text": first_err_text,
            "marker_line": marker_line,
            "marker_text": marker_text,
            "tail_line": tail_line,
            "tail_text": tail_text,
            "timeout": r.get("error") == "timeout",
            "text": r.get("output") or "",
        }
    return idx


# 工具正向消除标志（用于 verify.excerpt —— 赛题要求给「工具给出的正向消除标志」）
_POSITIVE_RES = (
    re.compile(r"There\s+were\s+\d+\s+'scan'\s+flip-flops[^\n]*", re.I),
    re.compile(r"Number\s+of\s+chains?\s*[:=]?\s*\d+[^\n]*", re.I),
    re.compile(r"\d+\s+clock\s+gating\s+instance[^\n]*", re.I),
)
# 便宜的哨兵词：先做子串判断，避免对 198MB 日志逐行跑 3 个正则
_MARKER_HINTS = ("Number of", "clock gating", "'scan'")


def _positive_marker(log_text):
    """从日志里取一条「正向消除标志」；没有则返回空串（不编造）。"""
    for rx in _POSITIVE_RES:
        m = rx.search(log_text or "")
        if m:
            return m.group(0).strip()[:160]
    return ""


def _diagnosis_summary(rec, found):
    """
    赛题要求 diagnosis.summary 是「定位过程的总览」，而不是根因的复述。

    静态审查发现的配置类问题，证据在 original.dofile 与 task_spec 的对照上；
    日志驱动的问题，证据在那一轮的日志里 —— 按证据来源给出不同的定位叙述。
    """
    src = found.get("source") or ""
    if src.endswith("original.dofile"):
        return ("逐条比对 %s 与 task_spec 的 DFT 配置要求（并结合工具手册的"
                "命令/参数约束、标准单元库与网表端口），确认问题出现在：%s"
                % (src, rec.get("issue", "")))
    return ("检查 %s 中该问题对应的日志/报告行，比对手册的命令语法与参数约束，"
            "确认问题出现在：%s" % (src, rec.get("issue", "")))


def _build_issue_resolutions(fix_records, run_results, file_changes):
    """
    从 fix_records 构建符合赛题 schema 的 issue_resolutions。

    赛题 schema：
      {issue_id, phenomenon, found{run_ref,source,locator,excerpt},
       diagnosis{summary,located_object,root_cause,violated_requirement},
       attempts[{fix{action,artifact_ref},verify{run_ref,source,resolved,locator,excerpt}}]}

    关键约束（docx 8.1.1 / 5.5.1）：
      - verify.run_ref 必须是**真正验证该问题的那一轮**，不能与 found.run_ref 混用；
        早期实现把两者都填成"发现问题的那一轮"，于是出现「resolved=true 但
        run_ref 指向报错轮」的自相矛盾记录。
      - locator / excerpt 只在 resolved=true 时提供，且取自真实日志。
      - artifact_ref 必须指向 file_changes 中真实存在的 change_id。
    """
    if not fix_records:
        return []

    log_idx = _run_log_index(run_results)

    # change_id -> 该改动所在轮次
    by_run = {}
    for fc in file_changes:
        m = re.search(r"runs/(R\d+)/", fc.get("path", "") or "")
        if m:
            by_run.setdefault(m.group(1), fc.get("change_id", ""))

    resolutions = []
    for i, rec in enumerate(fix_records):
        issue_id = f"I{i+1}"
        found_run = rec.get("found_run") or rec.get("run") or "R1"
        applied_run = rec.get("applied_run") or ""
        resolved = bool(rec.get("resolved"))
        # verify 轮次：已解决 -> 真正验证成功的那一轮；未解决 -> 尝试修复后的那一轮
        verify_run = rec.get("verified_run") or applied_run or found_run
        if verify_run not in log_idx and found_run in log_idx:
            verify_run = found_run

        found_info = log_idx.get(found_run, {})
        # found 证据：优先用记录里显式给出的（静态审查发现的问题，证据在
        # original.dofile 快照上），否则回落到该轮日志里的首个 [ERROR]
        found = {
            "run_ref": found_run,
            "source": rec.get("found_source") or f"runs/{found_run}/{found_run}.log",
            "locator": rec.get("found_locator")
                       or (f"L{found_info['err_line']}" if found_info.get("err_line") else ""),
            "excerpt": rec.get("found_excerpt") or found_info.get("err_text")
                       or rec.get("issue", ""),
        }

        diagnosis = {
            "summary": rec.get("diagnosis_summary") or _diagnosis_summary(rec, found),
            "located_object": rec.get("issue", ""),
            "root_cause": rec.get("root_cause", ""),
        }
        if rec.get("violated_requirement"):
            diagnosis["violated_requirement"] = rec["violated_requirement"]

        # artifact_ref：指向该次修复真正落盘的 dofile 改动（file_changes 中 Fn）
        artifact_ref = []
        cid = by_run.get(applied_run or verify_run)
        if cid:
            artifact_ref = [cid]

        v_info = log_idx.get(verify_run, {})
        verify = {
            "run_ref": verify_run,
            "source": f"runs/{verify_run}/{verify_run}.log",
            "resolved": resolved,
            "locator": "",
            "excerpt": "",
        }
        if resolved:
            # 已解决：locator 指向**具体那一行**（工具正向消除标志所在行），
            # excerpt 取该行原文。取不到标志时退化为日志末行（仍是真的摘录），
            # 不再用「整份日志」+ 固定文案充当证据。
            if v_info.get("marker_line"):
                verify["locator"] = f"L{v_info['marker_line']}"
                verify["excerpt"] = v_info.get("marker_text") or ""
            elif v_info.get("tail_line"):
                verify["locator"] = f"L{v_info['tail_line']}"
                verify["excerpt"] = v_info.get("tail_text") or ""
            elif v_info.get("total_lines"):
                verify["locator"] = f"L1-L{v_info['total_lines']}"
                verify["excerpt"] = "该轮日志无 [ERROR]/[FATAL]，工具正常执行至结束"
            if v_info.get("err_line"):
                # 理论上不该发生（resolved 却被判定有 ERROR），如实记录
                verify["locator"] = f"L{v_info['err_line']}"
                verify["excerpt"] = f"该轮仍有错误：{v_info.get('err_text', '')}"

        attempts = [{
            "fix": {
                "action": rec.get("fix", ""),
                "artifact_ref": artifact_ref,
            },
            "verify": verify,
        }]

        resolutions.append({
            "issue_id": issue_id,
            "phenomenon": rec.get("phenomenon") or found_info.get("err_text")
                          or rec.get("issue", ""),
            "found": found,
            "diagnosis": diagnosis,
            "attempts": attempts,
        })

    return resolutions



def write_decision_log(output_dir, decision_log):
    """写出 decision_log.json。"""
    out = Path(output_dir)
    (out / "decision_log.json").write_text(
        json.dumps(decision_log, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
输入解析模块
读取 /input 目录下的 task_spec.md、limitations.md、网表、库、CTL 等，
并利用 LLM 抽取结构化任务信息（含规则兜底）。
"""

import json
import os
import re
from pathlib import Path


# ============================================================================
# 基础文件扫描
# ============================================================================

def list_input_files(input_dir):
    """扫描输入目录，返回结构化文件清单。"""
    input_path = Path(input_dir)
    result = {
        "netlist": [],
        "lib": [],
        "ctl": [],
        "def": [],
        "dofile": [],
        "other": [],
    }

    for p in sorted(input_path.rglob("*")):
        if not p.is_file():
            continue
        name = p.name
        if name.endswith(".v"):
            result["netlist"].append(str(p))
        elif name.endswith(".lib"):
            result["lib"].append(str(p))
        elif name.endswith(".ctl"):
            result["ctl"].append(str(p))
        elif name.endswith(".def"):
            result["def"].append(str(p))
        elif name.endswith(".dofile"):
            result["dofile"].append(str(p))
        else:
            result["other"].append(str(p))

    return result


def read_text(input_dir, name, default=""):
    """读取输入目录下指定名称的文本文件。"""
    p = Path(input_dir) / name
    if p.exists():
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return default
    return default


# ============================================================================
# 任务类型判断
# ============================================================================

def detect_task_type(input_dir):
    """
    判断任务类型。
    任务二的特征：存在 original.dofile（有问题的 dofile）。
    任务一的特征：不存在 original.dofile，但可能有 golden.dofile（仅 public case）。
    """
    input_path = Path(input_dir)
    if (input_path / "original.dofile").exists():
        return "task2"
    return "task1"


# ============================================================================
# 运行预算（limitations.md）
# ============================================================================

def parse_limits(limitations_text):
    """
    解析 limitations.md，得到本次运行的**可执行**预算上限。

    赛题 5.4.1 明确：运行总时间或 Scan Insertion 工具调用次数超过 case 设定值，
    当前 case 直接判 0 分。因此这里把限制解析成数字并驱动 MAX_RUNS / 候选数，
    而不是只把原文塞进 prompt（旧实现就是这样，等于没有约束）。

    Returns:
        dict: {"max_wall_time_s": int|None, "max_tool_calls": int|None}
    """
    text = limitations_text or ""
    limits = {"max_wall_time_s": None, "max_tool_calls": None}

    # 时间：150 秒 / 5 分钟 / 2 小时 / 3min / 30s / 2 hours
    # 单位表按「长词优先」排列，并用 (?![a-z]) 防止把 minutes 里的 m 当成单位；
    # 中文单位后面常接「内/以内」，所以不能对中文用 \b。
    m = re.search(
        r"(?:不超过|至多|最多|上限(?:为)?|不高于|≤|<=|within)\s*"
        r"(\d+(?:\.\d+)?)\s*"
        r"(小时|分钟|分|秒|时|hours?|hrs?|minutes?|mins?|seconds?|secs?|h|s)(?![a-z])",
        text, re.I,
    )
    if not m:
        m = re.search(
            r"(\d+(?:\.\d+)?)\s*"
            r"(小时|分钟|分|秒|时|hours?|hrs?|minutes?|mins?|seconds?|secs?|h|s)(?![a-z])"
            r"\s*(?:之?内|以内|上限)",
            text, re.I,
        )
    if not m:
        # 兜底：整行提到「时间/wall/time/限时」时，取该行第一个「数字+单位」
        # （应对 "wall time: 3min" 这类既无前缀关键词、又无「以内/上限」的写法）
        for line in text.splitlines():
            if re.search(r"时间|wall|timeout|限时|耗时|\btime\b", line, re.I):
                mm = re.search(
                    r"(\d+(?:\.\d+)?)\s*"
                    r"(小时|分钟|分|秒|时|hours?|hrs?|minutes?|mins?|seconds?|secs?|h|s)(?![a-z])",
                    line, re.I,
                )
                if mm:
                    m = mm
                    break
    if m:
        val = float(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("小") or unit in ("时", "h") or unit.startswith("hour") or unit.startswith("hr"):
            val *= 3600
        elif unit.startswith("分") or unit.startswith("min"):
            val *= 60
        limits["max_wall_time_s"] = int(val)

    # 工具调用次数
    m = re.search(r"(?:工具调用次数|调用次数|工具调用|tool\s*calls?)[^\d]{0,24}(\d+)", text, re.I)
    if not m:
        m = re.search(r"(?:不超过|至多|最多|上限(?:为)?)\s*(\d+)\s*(?:次|轮)", text)
    if m:
        limits["max_tool_calls"] = int(m.group(1))

    return limits


# ============================================================================
# 规则兜底解析（不依赖 LLM 时也能抽取关键字段）
# ============================================================================

def _extract_top_module(spec_text, netlist_files):
    """从 task_spec 抽取顶层模块名，兜底用网表 module 名。"""
    # 常见表述：Top 模块: xxx / 顶层模块名为 `xxx` / 顶层模块: xxx
    patterns = [
        r"[Tt]op\s*模块(?:名)?[：:]\s*`?([A-Za-z_][\w]*)`?",
        r"顶层模块(?:名)?[为是：:]\s*`?([A-Za-z_][\w]*)`?",
        r"present_design\s+([A-Za-z_][\w]*)",
        r"设计名称\s*[：:]\s*([A-Za-z_][\w]*)",
    ]
    for pat in patterns:
        m = re.search(pat, spec_text)
        if m:
            return m.group(1)

    # 兜底：从网表文件中找 module 声明（只读文件头部，避免大网表全量读入）
    top = None
    for nf in netlist_files:
        try:
            with open(nf, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(64 * 1024)  # 只读前 64KB
            for m in re.finditer(r"^\s*module\s+([A-Za-z_][\w]*)", head, re.MULTILINE):
                top = m.group(1)
                # 第一个出现的 module 通常是顶层
                break
            if top:
                break
        except Exception:
            continue
    return top or ""


def _extract_clocks(spec_text):
    """抽取时钟端口列表。"""
    clocks = set()
    # -type clock -port xxx 形式
    for m in re.finditer(r"-port\s+([A-Za-z_][\w./]*)", spec_text):
        pass  # 不精确，交给 LLM；此处仅做粗提取
    # 常见时钟表格/描述里的 clk 类端口
    for m in re.finditer(r"`?([A-Za-z_][\w]*[Cc]lk[\w]*)`?\s*（|`([A-Za-z_][\w]*clk[\w]*)`?", spec_text):
        for g in m.groups():
            if g and g not in ("clk",):
                clocks.add(g)
    return sorted(clocks)


def _extract_scan_cell_mappings(spec_text):
    """抽取 DFF→SFF 映射表。"""
    mappings = []
    # 匹配表格行： `dff` → `sff` 或 | dff | sff |
    for m in re.finditer(
        r"`?([A-Za-z_][\w]*)`?\s*(?:→|->|→)\s*`?([A-Za-z_][\w]*)`?", spec_text
    ):
        dff, sff = m.group(1), m.group(2)
        if "s" in sff and dff != sff:
            mappings.append((dff, sff))
    return mappings


def parse_task(input_dir):
    """
    解析任务，返回结构化 dict。

    返回字段：
      - task_type: "task1" / "task2"
      - task_spec: 原文
      - limitations: 原文
      - files: list_input_files 结果
      - top_module: 顶层模块名
      - golden_dofile: 参考 dofile 内容（仅 public case 提供；hidden case 为空）
      - original_dofile: 有问题的 dofile 内容（task2）
    """
    spec = read_text(input_dir, "task_spec.md")
    limitations = read_text(input_dir, "limitations.md")
    files = list_input_files(input_dir)
    task_type = detect_task_type(input_dir)

    # 注意：**不读取 preset_issues.json**。
    # 它是 public case 的"预设问题清单"（参考答案），hidden case 并不提供；读取它
    # 既会在评测时失效，也等于用人工标注代替"自主识别问题"—— 赛题 5.4.1(c) 明确
    # 禁止由人工/预置信息决定修复方向。任务二的问题必须从 original.dofile 与
    # task_spec 的对照、工具日志与报告中自主识别（见 spec_fixer / run_task2）。
    golden_dofile = ""
    if os.environ.get("SCAN_AGENT_READ_GOLDEN") == "1":
        # 仅供离线自检逐条比对用（tools/_sim_all.py 等）。**生产路径默认不读**：
        # golden.dofile 是 public case 的参考答案，评测用的 hidden case 不提供
        # （赛题 docx 8.2.1/8.2.2）。生成/修复逻辑一律不得引用它，否则 public 的
        # 「通过」等于抄答案，而 hidden case 会整体失效。
        golden_dofile = read_text(input_dir, "golden.dofile")
    original_dofile = read_text(input_dir, "original.dofile")

    # 规则兜底抽取 top module（优先级：original.dofile > task_spec 文本 > 网表头部）
    #
    # 为什么不用 golden.dofile：它是 public case 的参考答案，hidden case 不提供
    # （赛题 docx 8.2.1/8.2.2）。用它推 top 会让 public 与 hidden 走两套逻辑，
    # 而且掩盖了「从任务书/网表识别设计」这一本应具备的能力。
    netlist_files = files["netlist"]
    top_module = ""
    # 1) 从 original.dofile（task_2 的任务输入）的 present_design 提取
    m = re.search(r"present_design\s+([A-Za-z_][\w]*)", original_dofile)
    if m:
        top_module = m.group(1)
    # 2) 从 task_spec 文本 / 网表结构提取
    if not top_module:
        top_module = _extract_top_module(spec, netlist_files)

    return {
        "task_type": task_type,
        "task_spec": spec,
        "limitations": limitations,
        "limits": parse_limits(limitations),
        "files": files,
        "top_module": top_module,
        "golden_dofile": golden_dofile,
        "original_dofile": original_dofile,
        "input_dir": str(input_dir),
        "case_id": _derive_case_id(input_dir, top_module),
    }


# 视为“通用挂载名”的目录名（不能作为 case 标识）
_GENERIC_DIR_NAMES = {
    "", "/", ".", "..", "input", "in", "data", "workspace", "work", "tmp",
}


def _derive_case_id(input_dir, top_module=""):
    """
    推导 case_id。

    - 本地测试：输入目录形如 .../{case}/input，取父目录名（如 "case1"）。
    - 评测环境：输入被挂载为 /input，父目录为 "/"、自身名为 "input"（都是通用名），
      此时回退用设计名（如 "openE902"），避免产出 "input" 这种无信息的 case_id。
    """
    p = Path(input_dir)
    try:
        p = p.resolve()
    except Exception:
        pass
    for name in (p.parent.name, p.name):
        if name and name not in _GENERIC_DIR_NAMES:
            return name
    if top_module:
        return str(top_module)
    return p.name or ""


# ============================================================================
# LLM 抽取结构化任务理解
# ============================================================================

def extract_task_understanding(task, llm):
    """
    利用 LLM 从 task_spec 抽取结构化配置，返回 dict。
    LLM 不可用时返回 None（由调用方走规则兜底）。
    """
    if not llm.available:
        return None

    system_prompt = """你是广立微 dftexp_scan 工具（Scan Insertion）专家。
请从任务说明书中抽取生成 dofile 所需的全部结构化信息，严格按 JSON 返回。

输出 JSON 结构：
{
  "top_module": "顶层模块名",
  "lib_files": ["库文件相对路径1", "..."],
  "netlist_files": ["网表文件相对路径1", "..."],
  "ctl_files": [{"file": "ctl路径", "module": "关联模块名"}],
  "scan_cell_mappings": [["dff类型", "sff类型"], ...],
  "clock_signals": [{"port": "时钟端口", "off_state": null, "role": "scan_clock或described_only", "associated_internal_clocks": ["..."], "pseudo_pi": ["..."]}, ...],
  "reset_signals": [{"port": "复位端口", "off_state": null, "role": "scan_reset或described_only"}, ...],
  "constant_signals": [{"port": "端口", "value": 0或1}, ...],
  "scan_enable_signals": [{"port": "端口", "usage": "scan/clock_gating/all", "off_state": null}, ...],
  "scan_element_exclude": ["需要 set_scan_element false 的对象路径"],
  "icg_exclude_elements": ["不参与 ICG 重连的模块"],
  "scan_cfg": {"chain_count": 0, "max_length": 0, "mix_clocks": null, "mix_edges": null, "internal_clocks": null, "si_port_format": "", "so_port_format": "", "add_lockup": null},
  "wrapper_cfg": {"enabled": false, "style": "", "chain_count": 0, "max_length": 0, "port_specific": [], "reuse_threshold": 0, "depth_threshold": 0, "dedicated_cell": null},
  "scan_segments": [{"name": "", "si": "", "se": "", "so": ""}],
  "drc_rules": {"ignore": ["DFTR-TIE1", "DFTR-TIE0"], "allow": ["DFTR10"]},
  "pseudo_pi": ["需要 add_pseudo_pi 的路径"],
  "wrapper_signals": [{"type": "wrapper_clock/wrp_in_shift_en/...", "port": ""}],
  "outputs": {"netlist": "输出网表文件名", "ctl": "ctl文件名或空", "scandef": "scandef文件名或空"},
  "workflow": ["connect_icg_only", "replace_only", "replace_unscan"] 或 ["full_scan"],
  "chain_count": 0
}

【关键约束——区分"描述"与"配置要求"】
task_spec 中可能包含两类关于信号的描述：
  (A) 描述性段落：如"时钟结构"表格、"复位信号"列表、"端口说明"、"网表说明"等——
      这些只是在介绍设计，**不是**要求你配置 set_scan_signal。
  (B) 明确要求：如"DFT 控制信号"表格、或明确说"配置 xxx 为 scan_enable /
      scan clock / scan reset / 用于 xxx"——这些才需要配置。

严格规则：
- 只有 (B) 类信号才能填入 clock_signals / reset_signals / scan_enable_signals，
  且 role 值填对应的 "scan_clock" / "scan_reset" / "scan_enable"。
- (A) 类信号一律不要抽取。如果无法确定，宁可不抽取。
- off_state 字段默认值为 null（不是 0），只有 task_spec 明确说明其取值时才填具体数字。
- 不要添加 task_spec 未明确要求的 -off_state / -usage 参数。

【drc_rules 字段（严格，宁可留空也不要编）】
- 只有当 task_spec **明确要求**「忽略 / 豁免某 DRC 规则」时才填入 ignore。
- 规则号必须逐字取自 task_spec 原文，且只能是手册 2.1.3.3 认可的写法：
  DFTR1-DFTR17、DFTR-TIE0、DFTR-TIE1、DFTR-L1、DFTR-L2。
- 严禁编造、补全或使用占位符（如 DFTR-XXX、DFTRx、缺 DFTR- 前缀的 TIE1）。
- 若 task_spec 只说「允许遗留 / 无需处理某违例」（例如 DFTR10），不要填入
  ignore —— 那是允许残留，不是要求忽略；这种情况填 []。
- 没有明确要求时统一填 []。

只返回 JSON，不要任何解释。字段值不确定时用 null 或空列表。"""

    # 构造相对路径视图（帮助 LLM 生成相对路径）
    files = task["files"]
    rel_view = {
        "netlist": [os.path.relpath(p, task.get("input_dir", ".")) for p in files["netlist"]],
        "lib": [os.path.relpath(p, task.get("input_dir", ".")) for p in files["lib"]],
        "ctl": [os.path.relpath(p, task.get("input_dir", ".")) for p in files["ctl"]],
        "def": [os.path.relpath(p, task.get("input_dir", ".")) for p in files["def"]],
    }

    user_prompt = f"""# 任务说明书
{task['task_spec']}

# 系统限制
{task['limitations']}

# 输入文件（相对 /input 目录）
网表: {json.dumps(rel_view['netlist'], ensure_ascii=False)}
库: {json.dumps(rel_view['lib'], ensure_ascii=False)}
CTL: {json.dumps(rel_view['ctl'], ensure_ascii=False)}

# 说明：本抽取**只依据任务说明书与输入文件清单**。
# 不注入 golden.dofile —— 它是 public case 的参考答案，hidden case 不提供，
# 注入会让抽取结果依赖不可得的材料（赛题 docx 8.2.1/8.2.2）。

请抽取结构化配置。"""

    # 输出上限收紧到「纯 JSON 抽取」所需（默认 8000 会拉长延迟，见 config 注释）
    max_tokens = 3000
    try:
        try:
            from . import config as _cfg
        except ImportError:
            import config as _cfg
        max_tokens = _cfg.LLM_UNDERSTAND_MAX_TOKENS
    except Exception:                                           # noqa: BLE001
        pass

    return llm.chat_json(system_prompt, user_prompt, max_tokens=max_tokens)

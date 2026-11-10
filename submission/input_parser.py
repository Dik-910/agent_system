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

def extract_task_understanding(task, llm, thinking=True):
    """
    利用 LLM 从 task_spec 抽取结构化配置，返回 dict。

    Args:
        thinking: 本次调用是否开思考。默认 True（语义理解需要推理）；
            调用方在预算窗口紧张时传 False 走快档（实测 62~68s → 6~7s）。
    LLM 不可用时返回 None（由调用方走规则兜底）。
    """
    if not llm.available:
        return None

    system_prompt = """你是广立微 dftexp_scan 工具（Scan Insertion）专家。
从任务说明书中抽取生成 dofile 所需的结构化配置，按 JSON 返回。

## 输出字段（下面用「<说明>」表示此处应填的值，**不要照抄说明文字**）

{
  "top_module": "<顶层模块名>",
  "lib_files": ["<相对 input 目录的库文件路径>"],
  "netlist_files": ["<相对 input 目录的网表文件路径>"],
  "ctl_files": [{"file": "<ctl 路径>", "module": "<关联模块名>"}],
  "scan_cell_mappings": [["<dff 单元名>", "<sff 单元名>"]],
  "clock_signals": [{"port": "<顶层端口名>", "associated_internal_clocks": [], "pseudo_pi": [], "off_state": null}],
  "reset_signals": [{"port": "<顶层端口名>", "off_state": null}],
  "constant_signals": [{"port": "<顶层端口名>", "value": <0 或 1>}],
  "scan_enable_signals": [{"port": "<顶层端口名>", "usage": "<all 或 scan>", "off_state": null}],
  "scan_element_exclude": ["<对象路径>"],
  "icg_exclude_elements": ["<模块名>"],
  "scan_cfg": {"<选项名>": "<该选项的取值>"},
  "wrapper_cfg": {"enabled": true, "style": "<样式名>", "reuse_threshold": <整数>,
                  "depth_threshold": <整数>, "dedicated_cell": "<单元名>"},
  "scan_segments": [{"name": "<段名>", "si": "<输入端口>", "se": "<使能端口>", "so": "<输出端口>"}],
  "drc_rules": {"ignore": ["<规则号>"], "allow": ["<规则号>"]},
  "pseudo_pi": ["<端口路径>"],
  "wrapper_signals": [{"type": "<wrapper_clock|wrap_in_shift_en|wrap_out_shift_en|wrap_capture_en>", "port": "<端口名>"}],
  "outputs": {"netlist": "<输出网表文件名>", "ctl": "<ctl 文件名或空串>", "scandef": "<scandef 文件名或空串>"},
  "workflow": ["full_scan"]
}

**重要的书写约定**：上面 `<...>` 里的内容是你必须替换掉的说明。
任务书**没有给出**的字段，整个键都不要写（或给空数组 []），
**绝对不要**为了"填满结构"而编一个数字或枚举值 —— 工具会把它当真配置执行。

## 填值规则（按信号类型分开，**不要一律照抽**）

**clock_signals —— 凡任务书提到的顶层时钟都要配。**
工具必须知道用哪个时钟做移位，所以无论任务书是写在"DFT 控制信号"表里、
还是只在"时钟结构"段落里描述，只要它是顶层时钟端口就填进来。

**scan_enable_signals —— 任务书指定作扫描使能的端口都要配。**

**reset_signals —— 默认不要填。**
这是最容易错的地方：任务书里常有一段「复位信号」列表（"pad_cpu_rst_b — CPU 复位"），
那是在**介绍设计结构**，不是要求你配置它。工具对复位有默认推断，
显式声明反而会引入 golden 里并不存在的配置。
只有任务书**明确要求把某端口配置为扫描复位**（例如"将 xxx 配置为 scan reset"
"复位在扫描状态下必须保持 0"）才填，并同时给 off_state。

**constant_signals**：任务书明确要求固定电平的端口（如 test_mode 必须拉 0）。

**扫描被排除的信号**（"不参与扫描""仅功能""不接入扫描路径"）：
不要填进任何 *_signals，按任务书指示放到 scan_element_exclude。

**scan_enable_signals 的 usage**：
**默认不写这个键。** 只有任务书明确要求某个用途时才填：
  · 明确说"用于时钟门控"/"控制时钟门控" → `"clock_gating"`；
  · 明确说"同时用于扫描和时钟门控" → `"all"`。
不要因为网表里有 ICG 就自行补 `all` —— 工具自己会推导，多写的 -usage
会变成与参考答案不一致的多余配置。

**off_state**：任务书写明关闭电平（如"关闭状态为 0"）才填 0/1，否则保持 null。

**scan_cfg**：任务书没提到的选项，**省略该键**（不要填 null、不要填 0）。
  · chain_count：任务书给的链数；只说"不超过 N 条"时填 N。
  · max_length：单链最大长度。
  · mix_clocks：单时钟域 → false；要求不同时钟域混插同链 → true。
  · mix_edges：**只在任务书明确谈到边沿策略时才填**（如"禁止混沿"→ false，
    "允许正负沿混合"→ true）。任务书没提边沿就省略，不要自行推断。
  · internal_clocks：时钟经组合逻辑派生内部时钟 → 按扇出填 "single"（单路）或
    "multi"（多路）；任务书不提内部时钟 → 省略该键。
  · add_lockup：任务书要求"插入 lockup latch" → true（官方答疑：这就是默认要求）。
  · insert_terminal_lockup：**只在任务书明确指定 latch 位置（链尾/末端/terminal）时才填**。
    官方答疑原话：「如果任务书没说明 lockup latch 位置的话，默认要求 -add_lockup」。

**workflow**：完整插链 → ["full_scan"]；
  只做前置处理（不建链）→ ["connect_icg_only","replace_only","replace_unscan"]。

**drc_rules**：只填任务书**明确要求忽略**的规则号，逐字抄原文，
  只能是 DFTR1~DFTR17、DFTR-TIE0、DFTR-TIE1、DFTR-L1、DFTR-L2。
  「允许残留某违例」不等于「要求忽略」—— 这种情况不要填。

## 硬性要求

- 端口名必须是**顶层端口**，不能是内部信号（网表事实里给了顶层端口清单，照它校验）。
- 只依据任务说明书与输入文件清单，**不要参考任何 golden 参考答案**。
- `lib_files` / `netlist_files` 用相对 input 目录的路径。
- 只返回 JSON，不要解释。字段确实不存在于本 case 时用 `[]` / `{}`；
  **能从任务书读到的信息必须填，不要因为"格式不确定"而留空**。
"""

    # 构造相对路径视图（帮助 LLM 生成相对路径）
    files = task["files"]
    rel_view = {
        "netlist": [os.path.relpath(p, task.get("input_dir", ".")) for p in files["netlist"]],
        "lib": [os.path.relpath(p, task.get("input_dir", ".")) for p in files["lib"]],
        "ctl": [os.path.relpath(p, task.get("input_dir", ".")) for p in files["ctl"]],
        "def": [os.path.relpath(p, task.get("input_dir", ".")) for p in files["def"]],
    }

    # 网表事实预填：把「设计里客观存在什么」直接告诉模型，省掉它去猜。
    #
    # 为什么（2026-10-28，读了开思考的 reasoning 原文才发现的）：
    # 旧 prompt 让模型自己判断「clk 算不算扫描时钟」，它于是花了两千多字纠结
    # 「任务书没有明确要求配置」，最后返回 clock_signals=[] —— 任务书明明写了
    # 「设计为单时钟域，仅有一个顶层时钟端口 clk」。
    # 这类信息网表事实里已经算出来了，不该让模型猜：给它端口清单和时钟候选，
    # 它只需要在半成品上做语义标注（哪个当 scan_enable、usage 填什么）。
    facts = task.get("_facts") or {}
    ports = facts.get("top_ports") or {}
    hints = []
    if ports:
        hints.append("顶层端口清单（只能从这里选 port）："
                     + ", ".join(sorted(ports)))
    if facts.get("clock_leaf_nets"):
        hints.append(f"该设计顶层时钟派生出 {len(facts['clock_leaf_nets'])} 个内部时钟网络")
    if facts.get("clock_inverted"):
        hints.append("存在经反相器派生的时钟（存在混沿）")
    dff = facts.get("dff_types") or {}
    if dff:
        hints.append("DFF 单元：" + ", ".join(f"{k}×{v}" for k, v in list(dff.items())[:6]))
        if not facts.get("sff_types"):
            hints.append("网表里没有现成 SFF 单元 → 需要 scan_cell_mappings")
    icg = facts.get("icg") or {}
    if icg.get("cells") or icg.get("floating_clk") or icg.get("floating_clock_roots"):
        hints.append("网表含时钟门控单元(ICG) → scan_enable 的 usage 必须是 all")
    facts_block = "\n".join(f"- {h}" for h in hints) or "（未解析到）"

    user_prompt = f"""# 任务说明书
{task['task_spec']}

# 系统限制
{task['limitations']}

# 输入文件（相对 /input 目录）
网表: {json.dumps(rel_view['netlist'], ensure_ascii=False)}
库: {json.dumps(rel_view['lib'], ensure_ascii=False)}
CTL: {json.dumps(rel_view['ctl'], ensure_ascii=False)}

# 网表事实（已由工具确定性解析，直接采信，不要重新推断）
{facts_block}

请抽取结构化配置。"""

    # 输出上限：**按思考档位分别取值**。
    #
    # 3000 是为「关思考」调出来的 —— 不产出 reasoning，正文只有一段 JSON，
    # 3000 绰绰有余，且能压低延迟。
    # 但开思考时 reasoning token 也吃这个闸门：实测 3000 会在「想完了、还没开始
    # 写 JSON」时被截断，正文 0 字符 → 整次理解白跑（2026-10-28 t1/case6 实测：
    # `reasoning 占满 3000 tokens（正文为空），放弃本次调用`）。
    # 所以开思考时放到 30000（与 llm_client._REASONING_RETRY_CAP 同口径）。
    max_tokens = 3000
    try:
        try:
            from . import config as _cfg
        except ImportError:
            import config as _cfg
        max_tokens = _cfg.LLM_UNDERSTAND_MAX_TOKENS
    except Exception:                                           # noqa: BLE001
        pass
    if thinking:
        max_tokens = max(int(max_tokens), 30000)

    # 理解阶段默认**开思考**：这一步是把任务书自然语言读成「哪段是设计描述、
    # 哪段是配置要求、单时钟域该怎么配」，属于推理活。调用方按预算窗口
    # 传 thinking=False 时才降级为快档（仍是 LLM 调用，只是不输出 reasoning）。
    return llm.chat_json(system_prompt, user_prompt, max_tokens=max_tokens,
                         thinking=thinking)

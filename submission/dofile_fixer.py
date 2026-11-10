#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dofile 针对性修复模块

针对「修复策略太粗暴」的改进：不再让 LLM 重写整个 dofile，
而是：
  1. 从运行日志定位出错的具体命令行（错误码 + 命令名 + 行号）；
  2. 检索手册相关命令/错误码说明（RAG）；
  3. 让 LLM 只输出「局部补丁」（patch），即针对出错命令的修正；
  4. 规则化地把补丁应用到原 dofile，保留其余正确部分。

补丁格式（让 LLM 输出 JSON）：
{
  "fixes": [
    {
      "target": "原命令的定位特征（命令名或唯一子串）",
      "action": "replace | delete | insert_after | insert_before",
      "content": "新命令内容（replace 时是完整新命令；insert 时是要插入的命令）",
      "reason": "修复原因"
    }
  ]
}
"""

import json
import re

try:
    from . import manual_rag
    from . import dofile_generator
    from . import spec_verifier
    from . import llm_client
except ImportError:
    import manual_rag
    import dofile_generator
    import spec_verifier
    import llm_client


# ============================================================================
# 错误定位
# ============================================================================

# 错误码 -> 可能关联的命令名（用于定位）
_ERROR_HINTS = {
    "CMD-0074": None,       # Command execution failed，需看上下文
    "SCAN-1315": None,      # Port does not exist
    "SCAN-1604": None,      # Signal type does not match -usage
    "COM-2001": None,       # Nothing implicitly matched
    "COM-3008": None,       # Nothing matched for design
    "COM-3010": None,       # Cannot find cell matching
    "SCAN-1009": "examine_scan_drc",  # order 错误
    "SCAN-4902": "set_scan_cfg",      # max_length warning
    "DFTDRC-3006": "set_scan_drc_rule_handling",  # Invalid DRC ID（规则号非法）
}

# 从错误行中提取命令名（形如 Command 'xxx' execution failed）
_CMD_IN_ERR_RE = re.compile(r"Command\s+'([\w_]+)'\s+execution\s+failed", re.IGNORECASE)
# 从错误行提取被引用的命令名（形如 Cannot execute command 'xxx'）
_CMD_QUOTE_RE = re.compile(r"[Cc]ommand\s+'([\w_]+)'", re.IGNORECASE)
# 从错误行提取「无效命令名」（形如 Invalid command name 'xxx'）
_INVALID_CMD_RE = re.compile(r"[Ii]nvalid\s+command\s+name\s+'([\w_]+)'", re.IGNORECASE)
# ScanInsertion 引擎级环境错误（SCAN-0620：Failed to read the environment variable）
#
# 经真机日志验证（public_cases/task_1/case1/output）：
#   - load_lib / load_netlist / present_design 均成功，SCAN-0626 明确报告 license
#     配置文件路径设置成功（/root/BitAnswer/），但紧接其后的每一条 ScanInsertion 命令
#     （set_scan_cell_mapping / set_scan_signal / set_dft_clock_gating_cfg /
#      set_scan_element / insert_dft_logic）全部报 SCAN-0620。
#   - 该错误在不同轮次、不同 load 路径（正确绝对路径 / 错误相对路径 / 加载彻底失败）
#     下完全一致地出现，与 dofile 内容无关。
#   => 根因是运行环境缺少 license 相关环境变量（如 SCANINSERTION_LICENSE_SERVER
#      未被注入），改 dofile 无法修复。绝不能据此去"修" load 路径。
_ENV_VAR_ERR_RE = re.compile(
    r"Failed\s+to\s+read\s+the\s+environment\s+variable|SCAN-0620",
    re.IGNORECASE,
)
# 从错误行提取端口名（形如 Port 'xxx' does not exist）
_PORT_RE = re.compile(r"Port\s+'([\w_]+)'", re.IGNORECASE)
# 从错误行提取 cell 名（形如 Cannot find xxx matching）
_CELL_RE = re.compile(r"Cannot\s+find\s+(\w+)", re.IGNORECASE)
# 从错误行提取选项名（形如 argument '-off_state' / option '-active_state'）
_OPT_IN_ERR_RE = re.compile(r"(?:argument|option)\s+'(-[\w]+)'", re.IGNORECASE)


def classify_error(err):
    """
    分类错误类型，返回类别字符串，用于指导修复策略。

    Returns:
        "invalid_command"  命令名不存在（拼写错误）—— 需改命令名
        "exec_failed"      命令名正确但执行失败 —— 需改参数，不改命令名
        "engine_env"       ScanInsertion 引擎级环境失败（SCAN-0620）—— 运行环境缺少
                           license 相关环境变量，与 dofile 内容无关，改 dofile 无法修复
        "other"            其他
    """
    if _INVALID_CMD_RE.search(err):
        return "invalid_command"
    if _ENV_VAR_ERR_RE.search(err):
        return "engine_env"
    if _CMD_IN_ERR_RE.search(err):
        return "exec_failed"
    return "other"


def is_engine_env_failure(parsed):
    """
    判断本次运行是否属于「ScanInsertion 引擎级环境失败」。

    SCAN-0620 一旦出现，所有 ScanInsertion 命令都会失败（真机日志已验证），
    此时任何针对 dofile 的修复都无效，应立即停止重试。

    Returns:
        bool
    """
    if not parsed or not parsed.get("errors"):
        return False

    if not any(_ENV_VAR_ERR_RE.search(e) for e in parsed["errors"]):
        return False

    # 统计因该错误而 execution failed 的命令，>=2 个即判定为引擎级
    failed_cmds = set()
    for e in parsed["errors"]:
        m = _CMD_IN_ERR_RE.search(e)
        if m:
            failed_cmds.add(m.group(1).lower())
    return len(failed_cmds) >= 2


def locate_error_lines(dofile_text, parsed):
    """
    根据日志解析结果，在 dofile 中定位出错的命令行。

    Returns:
        list of dict: [{"line_no": 行号(1-based), "text": 命令行原文, "error": 错误描述}]
    """
    if not parsed or not parsed.get("errors"):
        return []

    # 引擎级环境失败（SCAN-0620）：所有 ScanInsertion 命令都会失败，
    # 不存在"某一行写错了"这回事，定位命令行只会误导修复方向。
    # （注意 SCAN-0620 与其伴随的 CMD-0074 是两行，必须整体判定）
    if is_engine_env_failure(parsed):
        return []

    dofile_lines = dofile_text.splitlines()
    located = []

    for err in parsed["errors"]:
        # 提取命令名（优先 Invalid command name，其次 execution failed，最后 command quote）
        cmd = None
        m = _INVALID_CMD_RE.search(err)
        if m:
            cmd = m.group(1)
        else:
            m = _CMD_IN_ERR_RE.search(err)
            if m:
                cmd = m.group(1)
            else:
                m = _CMD_QUOTE_RE.search(err)
                if m:
                    cmd = m.group(1)

        # 提取端口/对象名
        port = None
        m = _PORT_RE.search(err)
        if m:
            port = m.group(1)

        cell = None
        m = _CELL_RE.search(err)
        if m:
            cell = m.group(1)

        # 提取选项名
        opt = None
        m = _OPT_IN_ERR_RE.search(err)
        if m:
            opt = m.group(1)

        # 引擎级环境错误（SCAN-0620）：与 dofile 内容无关，不定位到任何命令行，
        # 避免误导修复逻辑去改 load 路径或命令参数（历史上正是这样把好路径改坏的）。
        if _ENV_VAR_ERR_RE.search(err):
            continue

        # 在 dofile 中定位命令行（按优先级：cell > port > 命令名 > 选项名）
        target_substr = None
        if cell:
            target_substr = cell
        elif port:
            target_substr = port
        elif cmd:
            target_substr = cmd
        elif opt:
            target_substr = opt

        if target_substr:
            for i, line in enumerate(dofile_lines):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if target_substr in line:
                    located.append({
                        "line_no": i + 1,
                        "text": line,
                        "error": err,
                    })
                    break

    # 去重（按 line_no）
    seen = set()
    uniq = []
    for l in located:
        if l["line_no"] not in seen:
            seen.add(l["line_no"])
            uniq.append(l)
    return uniq


def build_error_context(dofile_text, parsed, located_lines, task):
    """
    构造修复用的上下文：错误信息 + 定位到的命令行上下文 + 手册段落。

    Returns:
        str: 供 LLM 参考的上下文文本
    """
    parts = []
    dofile_lines = dofile_text.splitlines()

    # 1. 错误信息
    parts.append("## 运行错误")
    for err in parsed.get("errors", [])[:10]:
        parts.append(f"- {err}")
    if parsed.get("total_violations"):
        parts.append(f"\nDRC 违例总数: {parsed['total_violations']}")
        # 按规则号分组：只给总数，LLM 无法判断根因（例如「DFTR1 上万条」意味着
        # 整体时钟不可控，而「DFTR10 几条」只是个别时钟接到数据端）。
        # 这是让 LLM 在**未知新 case** 上自行定位的关键信息，必须给出。
        rules = parsed.get("drc_rules") or {}
        if rules:
            top = sorted(rules.items(), key=lambda kv: -kv[1])[:8]
            parts.append("DRC 按规则分布: "
                         + "、".join(f"{r}×{n}" for r, n in top))
            parts.append("（规则含义见手册 2.1.3.1：DFTR1/2/3 时钟/置位/复位不可控，"
                         "DFTR9 时钟在测试态不可活动，DFTR10 时钟接到数据端…）")

    # 2. 定位到的命令行（含上下文）
    if located_lines:
        parts.append("\n## 出错命令（含上下文）")
        for loc in located_lines:
            ln = loc["line_no"]
            start = max(0, ln - 2)
            end = min(len(dofile_lines), ln + 2)
            parts.append(f"--- 第 {ln} 行附近 ---")
            for i in range(start, end):
                marker = ">>" if i == ln - 1 else "  "
                parts.append(f"{marker} {i+1}: {dofile_lines[i]}")
            parts.append(f"错误: {loc['error']}")

    # 3. 手册检索（RAG）
    query_parts = []
    for err in parsed.get("errors", []):
        # 提取错误码
        for code in re.findall(r"[A-Z]+-\d+", err):
            query_parts.append(code)
        # 提取命令名
        m = _CMD_QUOTE_RE.search(err)
        if m:
            query_parts.append(m.group(1))
    # 也检索 DRC 规则
    for v in parsed.get("drc_violations", []):
        query_parts.append(v.get("rule", ""))

    rag_text = ""
    if query_parts:
        query = " ".join(query_parts)
        rag_text = manual_rag.search_manual(query)
    if rag_text:
        parts.append("\n## 手册相关段落（RAG）")
        parts.append(rag_text)

    return "\n".join(parts)


# ============================================================================
# LLM 补丁生成 + 应用
# ============================================================================

def generate_patch_with_llm(task, current_dofile, parsed, llm, diagnosis=None):
    """
    让 LLM 生成局部补丁（JSON 格式），只修出错命令。

    Returns:
        list 或 None: 补丁列表
    """
    if not llm.available:
        return None

    located = locate_error_lines(current_dofile, parsed)
    context = build_error_context(current_dofile, parsed, located, task)

    system_prompt = """你是广立微 dftexp_scan（Scan Insertion）工具专家。
根据错误信息和手册段落，对 dofile 中的**出错命令**进行针对性修复。

严格要求：
1. 只修复出错的命令，不要改动其他正确部分，不要重写整个 dofile。
2. 以 JSON 输出补丁列表，格式如下：
{
  "fixes": [
    {
      "target": "用于定位原命令的唯一子串（如命令名+关键参数）",
      "action": "replace",
      "content": "修复后的完整命令（单行或多行，保持缩进风格）",
      "reason": "修复原因"
    }
  ]
}
3. action 取值：
   - replace：用 content 替换 target 定位到的命令
   - delete：删除 target 定位到的命令
   - insert_after / insert_before：在 target 定位到的命令后/前插入 content
4. target 必须是原 dofile 中真实存在的子串，且尽量唯一。
5. 只返回 JSON，不要 Markdown 代码块，不要解释。

【命令名必须来自白名单，严禁脑补命令名】
命令名必须严格来自下方白名单（一字不差）。严禁使用 set_scan_configuration /
set_scan_config / scan_configuration / scan_cell_mapping / set_scan_chain 等
不存在的命令名。若原命令名已在白名单中，则命令名本身没有错，不要改命令名。

【错误类型区分（关键）】
- "Invalid command name 'xxx'"：命令名拼写错误，需改成白名单中的正确命令名。
- "Command 'xxx' execution failed"：命令名正确，但参数/取值错误导致执行失败，
  此时不要改命令名，而是检查并修正命令的参数（如 cell 映射目标、选项取值）。
- "Failed to read the environment variable"（SCAN-0620）：这是 ScanInsertion 引擎
  初始化阶段读不到 license 相关环境变量导致的【运行环境问题】，与 dofile 内容无关。
  load_lib / load_netlist 无论成功还是失败，该错误都会出现。遇到此错误时：
    · 不要修改 load_lib / load_netlist 的路径（路径本来是对的，改了反而破坏）；
    · 不要修改命令名或参数；
    · 若没有其他独立错误，可不做任何修改（系统会判定为环境问题并停止重试）。

【DFTDRC-3006 / CMD-0074：set_scan_drc_rule_handling 规则号非法】
- 规则号只能是手册 2.1.3.3 认可的：DFTR1-DFTR17、DFTR-TIE0、DFTR-TIE1、
  DFTR-L1、DFTR-L2。出现 DFTR-XXX / DFTRx / 缺少"DFTR-"前缀的 TIE1 等均属非法。
- 修复方式：把规则号改成 task_spec 原文给出的合法规则号，通常即
  set_scan_drc_rule_handling {DFTR-TIE1 DFTR-TIE0} Ignore。
- **严禁**只把 Ignore 改成 Warning 来绕过报错（官方文档明确视为无效修复）。
- 若 task_spec 并未要求忽略该规则，则直接删除这一行。

可用命令白名单：
""" + dofile_generator.build_command_whitelist_prompt()

    user_prompt = f"""# 任务说明书
{task['task_spec']}

# 当前 dofile
{current_dofile}

{context}

# 诊断信息（如有）
{json.dumps(diagnosis or {}, ensure_ascii=False, indent=2)}

请输出修复补丁（JSON）。"""

    patch = llm.chat_json(system_prompt, user_prompt)
    if patch and isinstance(patch.get("fixes"), list):
        return patch["fixes"]
    return None


def generate_spec_patch_with_llm(task, current_dofile, issues, llm):
    """
    针对「DFT 配置不符合任务要求」（日志不报 ERROR）的规格类问题生成补丁。

    与 generate_patch_with_llm 的区别：后者由**日志错误**驱动（有 ERROR 行与出错命令
    定位），本函数由 **task_spec 规格断言**驱动（spec_verifier 产出），上下文里没有
    ERROR 行，取而代之的是「要求原文 + 证据 + 建议修复」。

    Returns:
        list 或 None: 补丁列表（格式同 generate_patch_with_llm）
    """
    if not llm.available or not issues:
        return None

    system_prompt = """你是广立微 dftexp_scan（Scan Insertion）工具专家。

工具运行**没有报错**，但最终 Dofile 的 DFT 配置**不满足插链任务说明书的要求**。
请针对每条「不满足项」修改 Dofile，使其满足任务说明书。

严格要求：
1. 只修改为满足任务说明书所必需的配置，不要重写整个 dofile，不要动无关部分。
2. 以 JSON 输出补丁列表：
{
  "fixes": [
    {
      "target": "用于定位原命令的唯一子串（如命令名+关键参数）",
      "action": "replace | delete | insert_after | insert_before",
      "content": "修复后的完整命令（单行或多行，保持缩进风格）",
      "reason": "对应哪条任务要求"
    }
  ]
}
   - replace：用 content 替换 target 定位到的命令
   - delete：删除 target 定位到的命令
   - insert_after / insert_before：在 target 定位到的命令后/前插入 content
3. target 必须是原 dofile 中真实存在的子串；若需要「新增」配置，请选择插入点
   所在行（如 present_design 行）用 insert_after。
4. 命令名必须严格来自下方白名单，严禁编造命令名或参数。
5. 只返回 JSON，不要 Markdown 代码块，不要解释。

可用命令白名单：
""" + dofile_generator.build_command_whitelist_prompt()

    user_prompt = f"""# 插链任务说明书
{task['task_spec']}

# 当前 Dofile
{current_dofile}

# 不满足的任务要求（逐条列出，含证据与建议）
{spec_verifier.format_issues(issues)}

请输出修复补丁（JSON）。"""

    patch = llm.chat_json(system_prompt, user_prompt)
    if patch and isinstance(patch.get("fixes"), list):
        return patch["fixes"]
    return None


# 工具在插链流程里**自己生成**的 net 名：`insert_dft_logic` 之后才存在。
# ECO 阶段（插链前）引用它们必然失败 —— `-from test_se` 报 COM-3008。
_TOOL_NET_RE = re.compile(
    r"(?i)^(?:test_(?:se|si|so)\d*|scan_(?:se|en|si|so)\d*|"
    r"subscanchain_\w+|scan_chain_\w+)$")
_DECL_PORT_RE = re.compile(r"(?:-port|add_port)\s+\{?([\w./\[\]]+)\}?")


def _drop_unknown_eco_refs(dofile_text):
    """
    删掉 `connect_pin -from <工具内部 net>` 这种**必然报错**的 ECO 行。

    2026-11-01（测试17 / task_2/case6）：LLM 补丁在 R2→R3 之间加了一行
        connect_pin -from test_se -to u_pwm/clk_gate_pwm/SCE
    `test_se` 是 `insert_dft_logic` 之后才生成的内部 net，ECO 阶段不存在，
    工具直接报 `[ERROR] [COM-3008] Nothing matched for '-from'`，R3 整轮白跑
    （平台仍判通过，因为 exit code 是 0 —— 属于"跑了一遍但什么都没做"）。

    判据收窄到「名字明显是工具生成的 net」且「dofile 里没有声明过该端口」，
    不去猜网表里的真实 net，避免误删合法 ECO。
    """
    text = dofile_text or ""
    # declared 只能算**在 ECO 之前**就已经存在的端口：
    # 实测 R3.dofile 里 `connect_pin -from test_se` 在第 31 行，而
    # `set_scan_signal -type scan_enable -port test_se` 在第 47 行 ——
    # 工具执行到第 31 行时 test_se 还没被创建，照样报 COM-3008。
    lines = text.splitlines()
    eco_start = len(lines)
    for i, ln in enumerate(lines):
        if re.match(r"\s*(?:disconnect_net|add_inst|connect_net)\b", ln):
            eco_start = i
            break
    prefix = "\n".join(lines[:eco_start])
    declared = {m.group(1) for m in _DECL_PORT_RE.finditer(prefix)}
    # ECO 块自己的 add_port 是有效的（就在同一块里，先建后用）
    declared |= {m.group(1) for m in re.finditer(r"add_port\s+\{?([\w./\[\]]+)\}?", text)}
    out, dropped = [], []
    for ln in lines:
        m = re.match(r"\s*connect_pin\b[^\n]*-from\s+(\S+)", ln)
        if m:
            ref = m.group(1).strip("{}")
            if _TOOL_NET_RE.match(ref) and ref not in declared:
                dropped.append(ref)
                continue
        out.append(ln)
    for r in dropped[:3]:
        print(f"  ⚠ 删除引用工具内部 net 的 ECO 行：-from {r}"
              f"（插链前不存在，会报 COM-3008）")
    if dropped:
        return "\n".join(out) + ("\n" if text.endswith("\n") else "")
    return text


def _block_span(lines, idx):
    """
    把第 idx 行扩展到它所属的**整个 Tcl 逻辑语句**（续行 + `{`…`}` 跨行块）。

    为什么必须有（2026-11-01，新 case ex3 首跑实测）：
      `set_scan_signal -type clock -port clk -off_state 2 \\` 这种三行块，
      按行 replace 只换掉首行，中间的
      `    -associated_internal_clocks {\\ u_clk_latch/Q \\}` 留在文件里，
      工具当成独立命令 → `[CMD-0074]`，整个流程停在 DRC 之前（0 条链、无 DRC）。
      删除同理：只删首行会留下一堆孤儿续行。

    返回 (start, end)（左闭右开），只向**后**扩展 + 必要时向前回退续行链。
    """
    if not lines:
        return idx, idx + 1
    idx = max(0, min(idx, len(lines) - 1))

    # 1) 向前：只要上一行以续行反斜杠结尾，当前行就是同一条语句的一部分
    start = idx
    for _ in range(64):                       # 防御：异常输入时不死循环
        if start == 0:
            break
        if lines[start - 1].rstrip().endswith("\\"):
            start -= 1
        else:
            break

    # 2) 向后：行尾续行 or {} 未配平就继续吞
    end = idx + 1
    depth = 0
    for i in range(start, end):
        depth += lines[i].count("{") - lines[i].count("}")
    while end < len(lines):
        if depth > 0 or lines[end - 1].rstrip().endswith("\\"):
            depth += lines[end].count("{") - lines[end].count("}")
            end += 1
        else:
            break
    return start, end


# 受保护的声明行：删掉会引发大面积违例，只允许「改值」不允许「删行」
_PROTECTED_DECL_RE = re.compile(
    r"^[ \t]*(?:set_scan_signal[ \t]+-type[ \t]+(?:clock|reset)\b"
    r"|set_scan_cell_mapping\b)")

# 题面允许「不声明」的口径（官方 task_2/case1 原文：resetn 为低有效异步复位…无需配置 reset 扫描信号）
_DECL_WAIVER_RE = re.compile(
    r"无需配置|无需声明|不需要配置|不需要声明|不必配置|不必声明|"
    r"不参与扫描|不作为扫描|不纳入扫描|禁止声明")


def _decl_delete_blocked(line_text, task):
    """
    删除/清空一条「时钟 / 复位 / 映射」声明前，判断是否该拦。

    2026-11-03 第六批报告 P0-1：某多时钟域 case 的时钟端口名笔误（少后缀），
    agent 第一轮诊断与修复**都正确**，但修复后 DRC 没清零（该设计还有其它独立缺陷），
    于是它把「DRC 未清零」误归因为「我上次改错了」，**把正确的时钟声明整行删掉** ——
    该时钟域彻底不声明，DFTR1 涨到上万条，链数从几十条塌到 2 条。
    也就是说：正确诊断最终产出错误结果，且日志里看不出来（诊断记录是对的）。

    这里加一道硬约束：端口在网表里真实存在、且题面没有「无需配置」这类豁免时，
    拒绝删除该声明行 —— 想改就改值（`_fix_scan_signals` 的纠错路径），不能删行。

    Returns:
        True 表示应当拦截。
    """
    if not _PROTECTED_DECL_RE.match(line_text or ""):
        return False
    m = re.search(r"-port[ \t]+(\S+)", line_text or "")
    port = m.group(1) if m else ""
    spec = (task or {}).get("task_spec", "") or ""
    if port and spec:
        # 题面在这个端口附近明说不用声明 -> 允许删（t2/c1 的 resetn 就是这种）
        for mm in re.finditer(r"(?<![\w])%s(?![\w])" % re.escape(port), spec):
            seg = spec[max(0, mm.start() - 80):mm.end() + 80]
            if _DECL_WAIVER_RE.search(seg):
                return False
    return True


def apply_patch(dofile_text, fixes, report=None, task=None):
    """
    把补丁应用到 dofile，返回新 dofile 文本。

    2026-11-01 两处修正（都来自新 case 的实测）：
      ① **按逻辑语句**替换/删除，不再只动一行 —— 续行块按行换会留残行（CMD-0074）。
      ② target 找不到时**不再静默丢弃**，而是记进 report["missed"]。
         旧实现直接 `continue`，于是「LLM 说改了 8 条、产物里一条没变」，
         决策记录却标着问题已识别 —— 看对了没改对，属于最坏的一类失败。

    Args:
        dofile_text: 原 dofile
        fixes: 补丁列表（LLM 输出）
        report: 可选 dict，回填 {"applied": [...], "missed": [...]}

    Returns:
        str: 修复后的 dofile
    """
    rep = report if report is not None else {}
    rep.setdefault("applied", [])
    rep.setdefault("missed", [])
    if not fixes:
        return dofile_text

    lines = dofile_text.splitlines(keepends=True)

    # 先把每个 fix 定位到**语句块**
    resolved = []
    for fix in fixes:
        target = (fix.get("target") or "").strip()
        action = fix.get("action", "replace")
        content = fix.get("content", "")

        line_no = None
        if target:
            for i, line in enumerate(lines):
                if target in line:
                    line_no = i
                    break
        if line_no is None:
            rep["missed"].append({
                "target": target[:120],
                "action": action,
                "reason": "target 在 dofile 中找不到",
            })
            continue

        start, end = _block_span(lines, line_no)
        resolved.append({
            "start": start,
            "end": end,
            "action": action,
            "content": content,
            "reason": fix.get("reason", ""),
            "target": target[:120],
        })

    # 按起始行从后往前应用，避免偏移
    resolved.sort(key=lambda x: -x["start"])
    for r in resolved:
        start, end = r["start"], r["end"]
        action, content = r["action"], r["content"]
        if content and not content.endswith("\n"):
            content += "\n"

        if action == "delete":
            blocked = [ln for ln in lines[start:end]
                       if _decl_delete_blocked(ln, task)]
            if blocked:
                # 不删、不改，记进 blocked 供上层汇报 —— 静默跳过会让
                # 「诊断说改了、产物没改」再次不可见（r51 已踩过一次）。
                rep.setdefault("blocked", []).append({
                    "target": r["target"], "action": action,
                    "reason": "受保护声明（时钟/复位/映射），禁止删行，只能改值",
                })
                print("  ⚠ 拒绝删除受保护的声明行（应改值而非删行）："
                      + blocked[0].strip()[:80])
                continue
            del lines[start:end]
            rep["applied"].append({"target": r["target"], "action": "delete"})
        elif action == "replace":
            # 把受保护声明「换成空白/换成别的东西」等价于删除，同样拦。
            _old_blk = "".join(lines[start:end])
            if _decl_delete_blocked(_old_blk, task) and \
                    not _PROTECTED_DECL_RE.match(content or ""):
                rep.setdefault("blocked", []).append({
                    "target": r["target"], "action": action,
                    "reason": "受保护声明被替换为非声明内容（等价删行）",
                })
                print("  ⚠ 拒绝把受保护声明替换成非声明内容：" + _old_blk.strip()[:80])
                continue
            # 保留原首行的缩进风格（若 content 未带缩进，则继承）
            indent = re.match(r"\s*", lines[start]).group(0)
            if content.lstrip() == content and indent:
                content = indent + content
            lines[start:end] = [content]
            rep["applied"].append({"target": r["target"], "action": "replace"})
        elif action == "insert_after":
            lines.insert(end, content)
            rep["applied"].append({"target": r["target"], "action": "insert_after"})
        elif action == "insert_before":
            lines.insert(start, content)
            rep["applied"].append({"target": r["target"], "action": "insert_before"})

    # ── 落地校验：声称改了 ≠ 真的改了 ─────────────────────────────────────
    # 2026-11-01 多源实测（官方公开 case 的 t2/c6、两套 AI 变体 case）：
    # decision_log 里"定位对、决策对"，final.dofile 里却原样保留 —— 全部是
    # 「补丁应用环节静默失败」。这里对每条动作做**应用后回读**：
    #   · delete   —— target 行必须从结果里消失；
    #   · replace/insert_* —— content 的关键内容必须出现在结果里。
    # 未通过的记入 missed，让上层能发现"修了个寂寞"，而不是空欢喜。
    new_text = "".join(lines)
    for r in resolved:
        target, action, content = r["target"], r["action"], r["content"]
        if action == "delete":
            if any(target in ln for ln in new_text.splitlines()):
                rep["missed"].append({
                    "target": target[:120], "action": action,
                    "reason": "应用后 target 行仍存在（可能被后续动作恢复或有多处副本）",
                })
        elif content:
            # 取 content 的首个非空、非纯注释行做存在性检查
            core = next((ln.strip() for ln in content.splitlines()
                         if ln.strip() and not ln.strip().startswith("#")), "")
            if core and core not in new_text:
                rep["missed"].append({
                    "target": target[:120], "action": action,
                    "reason": f"应用后未找到期望内容：{core[:80]}",
                })

    return new_text


def targeted_fix(task, current_dofile, parsed, llm, diagnosis=None):
    """
    针对性修复的统一入口。

    Returns:
        str: 修复后的 dofile（若无修复则返回原 dofile）
    """
    # 1. 先做规则化修复（确定性，只覆盖有唯一正确写法的错误模式；不依赖任何参考答案）
    rule_fixed = rule_based_fix(task, current_dofile, parsed)
    if rule_fixed and rule_fixed != current_dofile:
        # 规则修复也要过命令白名单校验
        rule_violations = dofile_generator.validate_dofile_commands(rule_fixed)
        if not rule_violations:
            return rule_fixed
        # 规则修复引入了非法命令（理论上不会），回退
        print(f"  ⚠ 规则修复引入非法命令 {rule_violations[0][1]}，回退")

    # 2. LLM 针对性补丁
    fixes = generate_patch_with_llm(task, current_dofile, parsed, llm, diagnosis)
    if not fixes:
        return current_dofile

    report = {}
    new_dofile = apply_patch(current_dofile, fixes, report=report, task=task)
    if report.get("blocked"):
        # 被硬约束拦下的补丁（禁止删时钟/复位/映射声明）。单独汇总一条，
        # 便于事后判断是不是「LLM 想用删行来掩盖 DRC 未清零」。
        print(f"  ⚠ 补丁有 {len(report['blocked'])} 条被安全约束拦下（未执行）")
    if report.get("missed"):
        # 旧实现静默丢弃：LLM 说改了 8 条、产物一条没变，决策记录却标"已识别"。
        # 现在至少让它可见，便于判断"是补丁写错"还是"定位方式不对"。
        print(f"  ⚠ 补丁有 {len(report['missed'])} 条 target 未命中（已跳过）")
        for m in report["missed"][:3]:
            print(f"      - {m['action']}: {m['target'][:70]}")
    new_dofile = _drop_unknown_eco_refs(new_dofile)

    # 3. 命令白名单校验：补丁不得引入非法命令（防止 LLM 脑补命令名，越修越糟）
    new_violations = dofile_generator.validate_dofile_commands(new_dofile)
    if new_violations:
        print(f"  ⚠ LLM 补丁引入 {len(new_violations)} 个非法命令，拒绝该补丁")
        for ln, cmd in new_violations[:5]:
            print(f"      - L{ln}: {cmd}")
        # 尝试用幻觉命令映射表自动纠正；纠正后仍非法则回退原 dofile
        corrected = dofile_generator._fix_hallucinated_commands(new_dofile, new_violations)
        corrected_violations = dofile_generator.validate_dofile_commands(corrected)
        if not corrected_violations:
            print("  ↻ 已用命令映射表纠正非法命令")
            return corrected
        return current_dofile

    return new_dofile


def targeted_spec_fix(task, current_dofile, spec_issues, llm):
    """
    规格类问题的针对性修复入口（无 ERROR，仅「DFT 配置不符合任务要求」）。

    与 targeted_fix 的区别：不依赖日志错误定位，而是以 spec_verifier 产出的
    「要求原文 + 证据 + 建议修复」为修复目标。

    Returns:
        str: 修复后的 dofile（无法修复时返回原文本）
    """
    if not spec_issues:
        return current_dofile

    # 1) 确定性优先：set_scan_cfg 的 chain_count / max_length / 端口命名这几项，
    #    spec_verifier 报得出、dofile_generator 就一定能确定性地补/改，
    #    不必也不应交给 LLM 去猜（LLM 不可用或幻觉时这些项就修不掉了）。
    det = dofile_generator.ensure_scan_cfg_from_spec(task, current_dofile)
    if det != current_dofile:
        print("  ✓ 已按 task_spec 原文确定性修正 set_scan_cfg")
        return dofile_generator.ensure_output_vars(det)

    # 2) 其余规格项交给 LLM 补丁
    #
    # 预算护栏（2026-10-10 平台实测引入）：150s 级 case 里，「LLM 诊断 + 工具 +
    # 规格复核」之后通常只剩 30~50s，而平台单次调用实测需 78~148s —— 发出去
    # 必然超时。平台首跑 t2/c2、t2/c3、t2/c4 正是这样白等了 48~52s。
    # 剩余不足一次调用时直接返回确定性结果，把时间留给后续轮次。
    if (getattr(llm, "available", False)
            and not llm_client.has_useful_time(llm)):
        left = llm.time_left() if hasattr(llm, "time_left") else None
        print(f"  ⚠ 剩余 {0 if left is None else left:.0f}s 不足以再调一次 LLM"
              f"（需 {llm_client.config.LLM_MIN_USEFUL_S:.0f}s），跳过 LLM 规格补丁")
        return current_dofile

    fixes = generate_spec_patch_with_llm(task, current_dofile, spec_issues, llm)
    if not fixes:
        return current_dofile

    report = {}
    new_dofile = apply_patch(current_dofile, fixes, report=report, task=task)
    if report.get("blocked"):
        print(f"  ⚠ 规格补丁有 {len(report['blocked'])} 条被安全约束拦下（未执行）")
    if report.get("missed"):
        # 落地校验失败 = "决策记录里改了、文件里没改"，评分口径下属于最坏失败，
        # 必须让上层看见（否则 verify 回填还会标 resolved=true）。
        print(f"  ⚠ 规格补丁有 {len(report['missed'])} 条未真正落地（已记录）")
        for m in report["missed"][:3]:
            print(f"      - {m['action']}: {m.get('reason', m['target'][:60])}")
    if new_dofile == current_dofile:
        return current_dofile

    # 命令白名单校验：规格补丁同样不得引入非法命令
    violations = dofile_generator.validate_dofile_commands(new_dofile)
    if violations:
        corrected = dofile_generator._fix_hallucinated_commands(new_dofile, violations)
        if dofile_generator.validate_dofile_commands(corrected):
            print(f"  ⚠ 规格补丁引入 {len(violations)} 个非法命令，拒绝该补丁")
            return current_dofile
        new_dofile = corrected

    # 补丁后重跑前必须保证 dofile 仍然完整（输出变量 / 必备命令 / 产物导出）
    new_dofile = dofile_generator.ensure_output_vars(new_dofile)
    return pad_missing_artifacts_safe(new_dofile, task)


def pad_missing_artifacts_safe(dofile_text, task):
    """
    规格修复后补齐可能被补丁破坏的导出/报告命令。

    这里只做「缺什么补什么」，不触发基础命令回退（避免整份 dofile 被规则模板替换
    而丢掉 LLM 的配置），因此直接复用 scan_agent 的补齐逻辑会引入循环依赖，
    故在本模块内做一份最小实现。
    """
    try:
        arts = dofile_generator.required_artifacts(task)
    except Exception:
        return dofile_text

    missing = []
    for cmd, fname in arts["reports"]:
        if not re.search(r"\b" + re.escape(cmd) + r"(?![A-Za-z0-9_])", dofile_text):
            missing.extend(dofile_generator.report_lines([(cmd, fname)]))
    if not re.search(r"\bdump_netlist\b", dofile_text):
        missing.append(f'dump_netlist -file "$out_res_dir/{arts["netlist"]}"')
    # CTL / SCANDEF 只在 task_spec / 参考 dofile 确实要求时才补
    # （避免给 task_2/case1 这类输出清单里没有 CTL/SCANDEF 的用例补多余文件）
    if arts.get("ctl_explicit", True) and not re.search(r"\bdump_ctl\b", dofile_text):
        missing.append(f'dump_ctl "$out_res_dir/{arts["ctl"]}"')
    if arts.get("scandef_explicit", True) and not re.search(r"\bdump_(?:scan_)?def\b", dofile_text):
        missing.append(dofile_generator.scandef_dump_line(
            arts["scandef"], arts["scandef_cmd"]))

    if not missing:
        return dofile_text

    block = "\n# ── 产物补齐（规格修复后校验）────\n" + "\n".join(missing) + "\n\n"
    m = re.search(r"\bexit\b(?!.*\bexit\b)", dofile_text)
    if m:
        return dofile_text[:m.start()] + block + dofile_text[m.start():]
    return dofile_text.rstrip("\n") + "\n" + block + "exit\n"


# ============================================================================
# 规则化修复（兜底，常见错误模式的确定性替换）
# ============================================================================

def rule_based_fix(task, current_dofile, parsed):
    """
    基于规则的确定性修复（不含 LLM）：只覆盖有明确、唯一正确写法的错误模式。

    包含 6 条规则：-off_state 非法值、clock 误加 -usage、非选项 -active_state、
    set_scan_cell_mapping 误用 -dff/-sff、以及 load_* 相对路径重写为绝对路径。

    说明：本函数**不读取 preset_issues.json**（旧 docstring 描述有误，已订正）。
    preset_issues 是 public case 的参考答案，hidden case 并不提供；且其「修复方式」
    为自然语言，无法确定性执行。任务二的修复统一走「日志错误驱动 + task_spec 规格
    断言驱动 + 手册 RAG + LLM 补丁」的路径，不依赖 preset_issues。

    Returns:
        str: 修复后的 dofile（无改动时返回原文本）
    """
    # 常见错误模式的确定性修复规则
    fixed = current_dofile

    # 规则1：set_scan_cell_mapping 目标为非 FF 单元（如 nand2_1）→ 无法自动推断 SFF，跳过
    # 规则2：-off_state 取非法值（非 0/1）→ 改为 0
    def fix_off_state(m):
        val = m.group(1)
        if val not in ("0", "1"):
            return m.group(0).replace(f"-off_state {val}", "-off_state 0")
        return m.group(0)

    fixed = re.sub(r"-off_state\s+(-?\d+)", fix_off_state, fixed)

    # 规则3：set_scan_signal clock 类型误加 -usage（-usage 仅 scan_enable 可用）
    def fix_usage_on_clock(m):
        line = m.group(0)
        if "-type clock" in line and "-usage" in line:
            return re.sub(r"\s*-usage\s+\w+", "", line)
        return line

    fixed = re.sub(
        r"set_scan_signal\s+[^\n]*",
        fix_usage_on_clock,
        fixed,
    )

    # 规则4：-active_state 是非选项 → 移除
    fixed = re.sub(r"\s*-active_state\s+\w+", "", fixed)

    # 规则4.5：`-associated_internal_clocks` 指向的 pin 不存在（SCAN-1620）
    # → 删掉这个参数，回到"不声明"。
    #
    # 2026-11-02 变体 case 报告（P0-1）：LLM 把时钟缓冲单元的**实例名**当成其输出的
    # **时钟路径**写进 -associated_internal_clocks，工具报
    #   [ERROR] [SCAN-1620] ... does not exist
    #   [ERROR] [CMD-0074] 命令执行失败
    # 整条 set_scan_signal 失败 → 后面的 scan_enable/复位/常量全没配上 →
    # 最终连 post_scan 网表都没有，整 case 归零。
    # 这里做**确定性兜底**：既然路径猜错时"不填"远远好过"填错"，只要日志里
    # 的 SCAN-1620 点名了某个我们写进去的关联时钟路径，就把该参数摘掉。
    _errs = " ".join(str(e) for e in ((parsed or {}).get("errors") or []))
    if "SCAN-1620" in _errs:
        _bad = {m.group(1).strip() for m in
                re.finditer(r"-associated_internal_clocks\s+\{([^}]*)\}", fixed)}
        _bad |= {m.group(1).strip() for m in
                 re.finditer(r'-associated_internal_clocks\s+"([^"]*)"', fixed)}
        # 日志没点名具体 pin 时也删：这个参数是唯一的"可选 pin 引用"，
        # 而 SCAN-1620 的语义就是"引用的 pin 不存在"。
        if not _bad or any(b and b in _errs for b in _bad):
            _before = fixed
            fixed = re.sub(r"\s+-associated_internal_clocks\s+\{[^}]*\}", "", fixed)
            fixed = re.sub(r'\s+-associated_internal_clocks\s+"[^"]*"', "", fixed)
            if fixed != _before:
                print("  ↻ SCAN-1620：移除猜测的 -associated_internal_clocks（宁可不填）")

    # 规则5：set_scan_cell_mapping 误用 -dff/-sff 选项 → 转成两个位置参数
    # 正确形式：set_scan_cell_mapping <dff_cell> <sff_cell>（无选项）
    def fix_cell_mapping(m):
        dff = re.search(r"-dff\s+([\w]+)", m.group(0))
        sff = re.search(r"-sff\s+([\w]+)", m.group(0))
        if dff and sff:
            return f"set_scan_cell_mapping {dff.group(1)} {sff.group(1)}"
        return m.group(0)

    fixed = re.sub(r"set_scan_cell_mapping\s+[^\n]*", fix_cell_mapping, fixed)

    # 规则6：load_lib/load_netlist/load_ctl 若用了 $script_dir/$case_dir 相对路径，
    #        重写为 task['input_dir'] 下的绝对路径。
    #        真正的相对路径（如 [file join $script_dir "lib" "sky130.lib"]）在 dofile
    #        位于 deliverables/ 下时必然失效，必须改成绝对路径。
    #        重要：绝不要拼出 ".." / "input" 这类段 —— input_dir 已经是 input 根目录，
    #        拼了就会得到 <run_dir>/input/lib/xxx 或 /input/../input/lib/xxx 这种错路径。
    input_dir = (task.get("input_dir") or "/input").replace("\\", "/").rstrip("/")

    def _clean_parts(parts):
        return [p for p in parts if p not in ("..", ".", "input", "")]

    def _join_parts(parts):
        cleaned = _clean_parts(parts)
        return input_dir + "/" + "/".join(cleaned) if cleaned else input_dir

    def fix_load_path(m):
        cmd = m.group(1)
        expr = m.group(2)

        def sub_join(inner):
            parts = re.findall(r'"([^"]+)"', inner.group(0))
            if parts:
                return '"' + _join_parts(parts) + '"'
            return inner.group(0)

        expr = re.sub(r'\[file\s+join\s+\$(?:script_dir|case_dir)[^\]]*\]', sub_join, expr)
        expr = re.sub(
            r'"\$(?:script_dir|case_dir)/([^"]+)"',
            lambda mm: '"' + _join_parts(mm.group(1).split("/")) + '"',
            expr,
        )
        expr = re.sub(
            r'\$(?:script_dir|case_dir)/([^\s"]+)',
            lambda mm: _join_parts(mm.group(1).split("/")),
            expr,
        )
        return cmd + " " + expr

    fixed = re.sub(
        r"(load_lib|load_netlist|load_ctl)\s+(.*?)(?=\n|$)",
        fix_load_path,
        fixed,
    )

    return fixed


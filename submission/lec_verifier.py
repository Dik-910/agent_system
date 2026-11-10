#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网表级修复 + 等价性校验（LEC）模块

任务二允许「改 dofile 或改网表」，但改网表必须通过逻辑等价性检查（LEC）。
本模块负责：
  1. 判断当前问题是否需要网表级修复（而非仅 dofile 修复）；
  2. 执行网表修复（备份 -> 修改 -> LEC 校验 -> 通过采用 / 失败回滚）；
  3. 把网表变更与 LEC 结果记录下来，供 decision_log 使用。

设计原则：
  - 网表修复是高风险的（可能破坏功能等价性），因此默认只做 dofile 修复；
  - 仅当诊断明确指示「必须改网表」或 dofile 修复耗尽仍未解决时，才尝试网表级修复；
  - 任何网表变更都必须经过 LEC，未通过则自动回滚。
"""

import re
from pathlib import Path

try:
    from . import tool_runner
except ImportError:
    import tool_runner


# ============================================================================
# 网表级修复决策
# ============================================================================

def needs_netlist_fix(diagnosis, parsed, fix_records):
    """
    判断是否应该尝试网表级修复。

    触发条件（满足任一）：
      1. diagnosis 的 fix 字段明确提到「修改网表 / 改网表 / netlist」；
      2. dofile 修复已多次（>=2）未能消除 DRC 违例，且仍有违例；
      3. 诊断根因指向网表本身（如单元缺失、连接错误），而非 dofile 配置。

    Returns:
        bool
    """
    if not diagnosis:
        return False

    text = " ".join([
        str(diagnosis.get("issue", "")),
        str(diagnosis.get("root_cause", "")),
        str(diagnosis.get("fix", "")),
    ]).lower()

    # 1. 明确指示改网表（需「改/修改」动词 + 网表/netlist 名词，避免仅提及网表就触发）
    netlist_hint = (
        ("网表" in text or "netlist" in text)
        and any(kw in text for kw in ("改", "修改", "修正", "fix", "modify", "replace", "连接", "rewire"))
    )
    if netlist_hint:
        return True

    # 1b. DRC 规则层面的确定性判据（不依赖 LLM 措辞）
    #     task2/case6 实测：缺陷是「u_stat 组时钟穿过组合门 U901」「ICG 的 SCE 被
    #     功能组合链占据」，LLM 的 fix 面板常写成「为 clk_a 追加声明为扫描时钟」
    #     之类，文本里既无「改网表」也无「netlist」—— 纯文本判据必然漏判，
    #     于是整轮反复改 dofile 声明、DRC 恒为 28，ECO 链路一次都没进。
    #     这两类规则的根因按定义就在网表里（dofile 声明无法消除，preset_issues 亦载明）。
    rules = _violation_rules(parsed)
    if rules & _NETLIST_ROOT_DRC_RULES:
        return True

    # 2. dofile 修复多次仍未清零
    if len(fix_records) >= 2 and parsed.get("total_violations", 0) > 0:
        return True

    return False


# 根因只可能在网表结构上的 DRC 规则（dofile 声明调整无法清零）：
#   DFTR1  时钟输入不可控（时钟路径穿过组合门 / 时钟接错源）
#   DFTR9  时钟在设置时不可活动（ICG 的 TE/SCE 被功能逻辑占据，测试态不放行）
_NETLIST_ROOT_DRC_RULES = {"DFTR1", "DFTR9"}


def _violation_rules(parsed):
    """从解析结果里取出实际出现的 DRC 规则号集合（DFTR1 / DFTR9 / DFTR-TIE1 …）。"""
    rules = set()
    for v in (parsed or {}).get("drc_violations") or []:
        if isinstance(v, dict):
            for key in ("rule", "rule_id", "code", "type"):
                val = v.get(key)
                if val:
                    rules.add(str(val).strip().upper())
        elif v:
            rules.add(str(v).strip().upper())
    # 工具自身的汇总口径：drc_rules = {规则号: 条数}（report_parser.parse_log）
    for r in ((parsed or {}).get("drc_rules") or {}):
        rules.add(str(r).strip().upper())
    return rules


# ============================================================================
# 网表级修复执行
# ============================================================================

def apply_netlist_fix(task, netlist_path, fix_content, output_dir, llm=None, run_id="R1"):
    """
    执行网表级修复：**在 output 侧的工作副本上修改** -> LEC 校验 -> 通过则采用副本。

    合规要点（docx 5.4.2「受保护文件不得修改」+ 评测 input 可能只读挂载）：
    本函数**绝不写入 input 目录下主办方提供的网表**。而是先把原始网表复制到
    `<output>/netlist_work/pre_scan_<run_id>.v`，在副本上修改并做 LEC；通过则把
    dofile 的 load_netlist 指向该副本，失败则直接丢弃副本（原网表始终未被触碰）。

    Args:
        task: parse_task 结果
        netlist_path: 原始（input 侧）网表路径，只读
        fix_content: 修改后的完整网表内容
        output_dir: 顶层输出目录（工作副本 / LEC 报告都落在这里）
        llm: 未使用（保留接口兼容）
        run_id: 当前轮次，用于命名 pre_scan_<run_id>.v

    Returns:
        dict: {
            "applied": bool,            # 是否最终采用（LEC 通过）
            "lec": dict,                # LEC 结果
            "modified_path": str,       # 采用时指向副本；未采用时指向原网表
            "pre_scan_name": str,       # 副本文件名（如 pre_scan_R2.v）
            "original_path": str,       # 原始网表路径（始终未改动）
            "reason": str,              # 未采用原因（若未通过）
        }
    """
    original = Path(netlist_path)
    output_dir = Path(output_dir)
    work_dir = output_dir / "netlist_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    # LEC 报告必须落在赛题 schema 指定的位置：runs/R{n}/reports/lec_report.rpt
    # （final_results/reports/ 由 output_manager 在 applied 时复制）。
    # 旧实现写到 <output>/lec/lec_report.rpt，既不规范，也让
    # 「本轮是否做过 LEC」在产物里不可见。
    reports_dir = output_dir / "runs" / str(run_id) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    original_content = original.read_text(encoding="utf-8", errors="replace")
    pre_scan_name = f"pre_scan_{run_id}.v"
    modified = work_dir / pre_scan_name

    if original_content == fix_content:
        return {
            "applied": False,
            "lec": None,
            "modified_path": str(original),
            "pre_scan_name": "",
            "original_path": str(original),
            "applied_run": run_id,
            "reason": "fix_content 与原网表相同，无实际修改",
        }

    # 1. 写入工作副本（原网表不动）
    modified.write_text(fix_content, encoding="utf-8")

    # 2. 提取顶层模块与库
    top_module = task.get("top_module") or ""
    lib_files = task.get("files", {}).get("lib", [])

    # 3. LEC 校验：原始网表 vs 工作副本
    lec = tool_runner.run_yosys_lec(
        original_netlist=str(original),
        modified_netlist=str(modified),
        run_dir=reports_dir,
        top_module=top_module or None,
        lib_files=lib_files,
    )

    if lec.get("success"):
        return {
            "applied": True,
            "lec": lec,
            "modified_path": str(modified),
            "pre_scan_name": pre_scan_name,
            "original_path": str(original),
            "applied_run": run_id,
            "reason": "",
        }

    # 4. LEC 未通过 -> 丢弃副本（原网表从未被修改）
    try:
        modified.unlink()
    except OSError:
        pass
    return {
        "applied": False,
        "lec": lec,
        "modified_path": str(original),
        "pre_scan_name": pre_scan_name,
        "original_path": str(original),
        "applied_run": run_id,
        "reason": "LEC 校验未通过，已丢弃修改（原网表未被改动）",
    }


def verify_netlist_equivalence(task, original_path, modified_path, output_dir):
    """
    纯校验：不修改任何文件，只做两个网表之间的等价性检查。
    用于「网表已由外部修改，需要确认等价」的场景。

    Returns:
        dict: LEC 结果（同 run_yosys_lec）
    """
    output_dir = Path(output_dir)
    lec_dir = output_dir / "lec"
    lec_dir.mkdir(parents=True, exist_ok=True)

    return tool_runner.run_yosys_lec(
        original_netlist=original_path,
        modified_netlist=modified_path,
        run_dir=lec_dir,
        top_module=task.get("top_module") or None,
        lib_files=task.get("files", {}).get("lib", []),
    )


# ============================================================================
# 记录辅助
# ============================================================================

def build_netlist_change_record(netlist_result):
    """
    把网表修复结果转成 decision_log 的 file_changes / issue_resolutions 记录。

    Returns:
        dict 或 None
    """
    if not netlist_result:
        return None
    lec = netlist_result.get("lec") or {}
    return {
        "type": "netlist_modification",
        "netlist_path": netlist_result.get("modified_path", ""),
        "original_path": netlist_result.get("original_path", ""),
        "pre_scan_name": netlist_result.get("pre_scan_name", ""),
        "applied": netlist_result.get("applied", False),
        "lec_passed": bool(lec.get("success")),
        "reason": netlist_result.get("reason", ""),
        "lec_report": lec.get("report", ""),
    }


# ============================================================================
# 确定性网表 ECO：时钟路径 / ICG 测试使能
# ============================================================================
#
# 为什么要有这条路径（task2/case6 实测）：
#   让 LLM「重写整份网表」风险最高 —— case6 真跑时它把顶层 scan_mode 端口声明
#   漏掉了，随后 connect_pin -from scan_mode 直接 SCAN-1315；而 ECO 命令本身
#   只需要「一条连接 + 一个 MUX」，完全可以从网表结构确定性推导。
#
# 统一接法（任务书「MUX 接法约定 A/B 统一」+ preset_issues 的修复成功判据）：
#   A0 = 原连接（原逻辑，功能态 scan_mode=0 透传）
#   A1 = 测试逻辑（时钟路径：扫描时钟；ICG TE：tie 1）
#   S  = 测试模式端口（网表里已声明但无负载的那个，case6 的 scan_mode）
#   X  → 原 net（被修引脚的连接一律不动）

# 时钟类 MUX 与常量单元：优先用库里的 mux2_1 / conb_1（sky130 命名），
# 由 lib_cell_names 校验存在性，不存在返回 None 交给 LLM 兜底。
_MUX_NAME_HINTS = ("mux2_1", "mux2", "mx2", "mux_2")
_TIEHI_NAME_HINTS = ("conb_1", "conb", "tiehi", "tie_hi", "tielogic_hi")


def _pick_lib_cell(cell_names, hints):
    """在库里按命名优先级挑一个候选单元（只做子串匹配，不猜不造）。"""
    low = {c.lower(): c for c in (cell_names or [])}
    for h in hints:
        for lc, orig in low.items():
            if h in lc:
                return orig
    return None


def _parse_insts(netlist_text, lib_meta):
    """
    从网表文本解析实例连接：[(module, inst, cell, {pin: net})]。

    只处理 `cell inst ( .pin(net), ... );` 这种门级形态 —— 与 netlist_facts
    使用同一套假设（门级网表，无表达式）。位置连接（无 .pin）跳过。
    """
    insts = []
    cur_module = ""
    for m in re.finditer(r"\bmodule\s+([A-Za-z_]\w*)", netlist_text):
        pass
    # 逐模块切块，保留 module 归属（层次路径要 module+inst）
    blocks = re.split(r"(?m)^\s*module\s+", netlist_text)
    for blk in blocks[1:]:
        nm = re.match(r"([A-Za-z_]\w*)", blk)
        if not nm:
            continue
        cur_module = nm.group(1)
        for im in re.finditer(
                r"\b([A-Za-z_]\w*)\s+\\?([A-Za-z_][\w\[\]\.\\]*)\s*\(", blk):
            cell, inst = im.group(1), im.group(2).replace("\\", "")
            # 取到匹配括号为止
            depth, start = 1, im.end()
            i = start
            while i < len(blk) and depth:
                if blk[i] == "(":
                    depth += 1
                elif blk[i] == ")":
                    depth -= 1
                i += 1
            conn_txt = blk[start:i - 1]
            pins = {p.group(1): p.group(2).strip()
                    for p in re.finditer(r"\.([A-Za-z_]\w*)\s*\(\s*([^)]*)\)", conn_txt)}
            if pins:
                insts.append((cur_module, inst, cell, pins))
    return insts


def _fresh_inst(module, tag, existing):
    """生成不与网表已有实例重名的 ECO 实例名（如 u_stat/eco_clkmux、u_stat/eco_clkmux_2）。"""
    n = 1
    while True:
        nm = f"{tag}" if n == 1 else f"{tag}{n}"
        if nm not in existing:
            return f"{module}/{nm}" if module else nm
        n += 1



def _hierarchy_prefixes(insts, lib_meta, top):
    """
    模块名 -> 顶层层次前缀（实例路径）。

    为什么必须有这一步（case6 实测，差点整块 ECO 报错）：
      网表里 `module stat_unit` 的实例名是 `u_stat`，FF 的真实层次路径是
      `u_stat/status_reg[0]`。工具的所有 ECO 命令都按**实例路径**解析，
      按模块名写成 `stat_unit/nclk_a` 会直接找不到对象。
      官方 golden 也正是写 `u_stat/nclk_a` / `u_stat/U901/X` / `u_stat/U912`。

    Returns:
        dict: {module: prefix}；prefix 为空串表示 top 自身。
        同一模块被多处实例化时**只取第一个前缀**（ECO 只改一处即可说明根因，
        但这种情况会记进 notes 由调用方决定是否放弃）。
    """
    libs = set((lib_meta or {}).keys())
    children = {}          # parent_module -> [(inst_name, child_module)]
    for mod, inst, cell, _pins in insts:
        if cell in libs:
            continue
        # 非库单元 = 子模块实例
        children.setdefault(mod, []).append((inst, cell))

    if not top:
        return {}

    prefixes = {top: ""}
    queue = [top]
    multi = set()
    while queue:
        parent = queue.pop(0)
        pfx = prefixes[parent]
        for inst, child in children.get(parent, []):
            child_pfx = f"{pfx}{inst}" if not pfx else f"{pfx}/{inst}"
            if child in prefixes and prefixes[child] != child_pfx:
                multi.add(child)          # 多处实例化：同名模块不同路径
                continue
            if child in prefixes:
                continue
            prefixes[child] = child_pfx
            queue.append(child)
    for m in multi:
        prefixes.pop(m, None)             # 有歧义的模块不生成 ECO（交给 LLM 兜底）
    return prefixes


def _fp(prefix, name):
    """把模块内的相对名拼成顶层层次路径。"""
    if not prefix:
        return name
    return f"{prefix}/{name}"


def _seq_cells(lib_meta):
    return {c for c, m in (lib_meta or {}).items() if m and m.get("seq")}


def _is_icg_cell(cell, meta):
    """
    是否时钟门控单元（ICG）。

    不能只看 `meta["seq"]`：sky130 的 sdlclkp_1 在 Liberty 里被描述成锁存器，
    parse_lib_metadata 会给出 seq=True，于是「跳过时序单元」的写法会把 ICG 一起
    跳掉 —— case6 的 16 条 DFTR9 因此推导不出来。这里按「名字像 ICG」且
    「有 GATE/SCE(TE) 这类门控输入 + 输出脚」判定。
    """
    meta = meta or {}
    if not re.search(r"clkp|clkgat|icg|clk_gate|cgls|dlclkp", cell, re.I):
        return False
    ins = {p.upper() for p in (meta.get("in_pins") or set())}
    if not ins & {"GATE", "E", "EN", "TE", "SCE"}:
        return False
    return bool(meta.get("clock_pins")) and bool(meta.get("out_pins"))


def _conns_of(insts, module, inst):
    for mod, i, cell, pins in insts:
        if mod == module and i == inst:
            return pins
    return {}


# 顶层 input 端口里「像时钟」的名字（最后手段用；只用于兜底，不覆盖 spec 明示值）
_CLK_NAME_RE = re.compile(
    r"(clk|clock|cg_|_cg\b|ck\b|_ck$|clkin|clk_in)", re.I)


def _module_body(netlist_text, top):
    """
    取出顶层 module 的「头 + 体」文本（按 endmodule 截断）。

    兼容两种常见 Verilog 写法（隐藏 case 两种都可能出现）：
      ① 非 ANSI：`module top ( clk, scan_mode, a );  input clk; input scan_mode;`
         —— case6 就是这种；
      ② ANSI：  `module top ( input clk, input scan_mode, ... );`
    """
    if top:
        m = re.search(r"(?ms)^\s*module\s+%s\b(.*?)^\s*endmodule" % re.escape(top),
                      netlist_text)
        if m:
            return m.group(1)
    m = re.search(r"(?ms)^\s*module\s+\w+.*?^\s*endmodule", netlist_text)
    return m.group(0) if m else ""


def _top_input_ports(netlist_text, top):
    """
    从顶层 module 取出 input 端口名列表（保持声明顺序）。

    旧实现只认 ANSI 写法（在端口表里找 `input`），对 case6 这种非 ANSI 写法
    返回空列表 —— 于是「网表反推扫描时钟」静默失效。
    """
    body = _module_body(netlist_text, top)
    if not body:
        return []
    ports = []
    # ① 非 ANSI：body 内 `input [3:0] a, b;` 形式的声明
    for m in re.finditer(r"(?m)^\s*input\b([^;]*);", body):
        decl = re.sub(r"\[[^\]]*\]", " ", m.group(1))
        ports += re.findall(r"[A-Za-z_]\w*", decl)
    # ② ANSI：端口表里带 input 关键字的条目
    head = body.split(";", 1)[0]
    if "input" in head:
        for decl in head.split(","):
            if not re.search(r"\binput\b", decl, re.I):
                continue
            names = re.findall(r"[A-Za-z_]\w*",
                               re.sub(r"\[[^\]]*\]", " ", decl))
            if names:
                ports.append(names[-1])
    # 去重保序（ANSI + 非 ANSI 混写时可能重复）
    seen, out = set(), []
    for p in ports:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def discover_clock_port(netlist_text, lib_meta, top=None):
    """
    从网表反推「本设计的工作时钟端口」，供 ECO 的 A1 端使用。

    优先级：
      1. 顶层 input 端口里**真有 FF 时钟接上去**的那个（最可信）；
      2. 顶层 input 端口里名字最像时钟的（clk / clock / cg_ / ck）。
         名字像但没接 FF 的不选 —— 那多半是测试时钟或未用端口。

    为什么要这个函数：隐藏 case 的 spec 不一定写明扫描时钟端口；缺了它
    derive_clock_path_eco 就无法填 A1，整条 DFTR1 修复路径会被静默跳过。
    """
    ports = _top_input_ports(netlist_text, top or _guess_top(netlist_text, lib_meta))
    if not ports:
        return ""
    insts = _parse_insts(netlist_text, lib_meta)
    # 统计「被时序单元时钟脚直接引用的顶层 input 端口」
    used = set()
    for _mod, _inst, cell, pins in insts:
        meta = (lib_meta or {}).get(cell) or {}
        if not meta.get("seq"):
            continue
        for pin in (meta.get("clock_pins") or set()):
            net = pins.get(pin)
            if net in ports:
                used.add(net)
    if used:
        # 多个都接了 FF 时，优先名字像时钟的，其次保持端口顺序
        for p in ports:
            if p in used and _CLK_NAME_RE.search(p):
                return p
        for p in ports:
            if p in used:
                return p
    for p in ports:
        if _CLK_NAME_RE.search(p):
            return p
    return ""


def derive_clock_path_eco(netlist_text, lib_meta, mode_port, clock_port, top=None):
    """
    时钟路径穿过组合门的确定性 ECO。

    判定：找「时序单元的时钟 pin 所接 net」的驱动源；若驱动源是**组合逻辑门**
    （非库里的时序单元、非端口、非 ICG），则该组 FF 的时钟不可控（DFTR1）。

    以 case6 形态为例（12 个 status_reg 的 CLK=nclk_a，nclk_a 由 U901 and2 驱动）：
        disconnect_net  u_stat/nclk_a  u_stat/U901/X
        add_net         u_stat/nclk_src
        add_inst        u_stat/U912  <mux2_1>
        connect_net     u_stat/nclk_src  {u_stat/U901/X  u_stat/U912/A0}
        connect_net     u_stat/nclk_a    u_stat/U912/X
        connect_pin     -from <clk>      -to u_stat/U912/A1
        connect_pin     -from <mode>     -to u_stat/U912/S

    A1（扫描时钟）的来源有三档，按可靠性降序：
      1. task["scan_clock_port"]  —— spec 明确指定的扫描时钟端口；
      2. **同一个 net 的 FF 组里已有正常时钟树驱动**的端口 —— 从网表里反推：
         若某个 FF 组的时钟 net 由上一级 FF / ICG 驱动（即正常时钟），或直接
         接的是顶层 input 端口，则那个端口就是本设计真正的工作时钟；
      3. 顶层唯一/第一个落在 clock 名模式上的 input 端口（最后手段）。

    为什么必须加 2/3（泛化缺陷，2026-10-22）：
      旧实现 `if not clock_port: return None`。隐藏 case 的 task_spec 只要没有
      明确点名扫描时钟，DFTR1 的 **整条确定性 ECO 路径就被静默关闭** —— 不报错、
      不写日志，只在最后表现为「DRC 一直在，怎么修都不降」。这只是「恰好
      case6 的 spec 里写了时钟名」才没暴露，属于必须消除的硬编码假设。

    Returns:
        dict 或 None：{"groups": [...], "lines": [...], "reason": str}
        None 表示「没有可确定性推导的时钟路径缺陷」（交给 LLM 兜底）。
    """
    if not netlist_text or not mode_port:
        return None
    seq = _seq_cells(lib_meta)
    mux = _pick_lib_cell(lib_meta.keys() if hasattr(lib_meta, "keys") else lib_meta,
                         _MUX_NAME_HINTS)
    if not mux:
        return None

    insts = _parse_insts(netlist_text, lib_meta)
    prefixes = _hierarchy_prefixes(insts, lib_meta, top)
    # 建 net -> 驱动 (module, inst, pin, cell) 索引（只看库单元的输出脚）
    drivers = {}
    for mod, inst, cell, pins in insts:
        meta = (lib_meta or {}).get(cell) or {}
        out_pins = meta.get("out_pins") or set()
        for pin, net in pins.items():
            if pin in out_pins and net:
                drivers.setdefault((mod, net), []).append((inst, pin, cell))

    # 分组：同一 (module, 时钟 net, 驱动源) 的 FF 归为一组
    groups = {}
    for mod, inst, cell, pins in insts:
        meta = (lib_meta or {}).get(cell) or {}
        if not meta.get("seq"):
            continue
        clk_pins = meta.get("clock_pins") or set()
        for pin in clk_pins:
            net = pins.get(pin)
            if not net:
                continue
            for (dmod, dnet), lst in drivers.items():
                if dmod != mod or dnet != net:
                    continue
                for dinst, dpin, dcell in lst:
                    if dcell in seq:
                        continue          # 上一级 FF 驱动 -> 正常时钟树
                    if "clkp" in dcell.lower() or "icg" in dcell.lower() \
                            or "clk_gate" in dcell.lower() or "dlclkp" in dcell.lower():
                        continue          # ICG 驱动 -> 另一类缺陷（DFTR9）
                    groups.setdefault((mod, net, dinst, dpin), []).append(f"{inst}/{pin}")

    if not groups:
        return None

    existing = {i for _m, i, _c, _p in insts}
    lines, detail = [], []
    for gi, ((mod, net, dinst, dpin), ffs) in enumerate(sorted(groups.items()), 1):
        if mod not in prefixes:
            continue                      # 层次路径有歧义/不可达 -> 该组跳过
        pfx = prefixes[mod]
        src_net = net + "_src"
        mux_name = _fresh_inst("", "eco_clkmux", existing)
        existing.add(mux_name)
        n_net, n_src = _fp(pfx, net), _fp(pfx, src_net)
        n_drv = _fp(pfx, f"{dinst}/{dpin}")
        n_mux = _fp(pfx, mux_name)
        lines += [
            f"# 时钟路径 ECO（{len(ffs)} 个时序单元，根因：{_fp(pfx, dinst)} 组合门驱动 {n_net}）",
            f"disconnect_net  {n_net}  {n_drv}",
            f"add_net         {n_src}",
            f"add_inst        {n_mux}  {mux}",
            f"connect_net     {n_src}  {{{n_drv}  {n_mux}/A0}}",
            f"connect_net     {n_net}  {n_mux}/X",
            f"connect_pin     -from {clock_port}  -to {n_mux}/A1",
            f"connect_pin     -from {mode_port}   -to {n_mux}/S",
        ]
        detail.append({"kind": "clock_path", "module": mod, "prefix": pfx,
                       "clock_net": n_net, "driver": n_drv, "ffs": len(ffs),
                       "mux": n_mux})
    return {"groups": detail, "lines": lines,
            "reason": f"时钟路径穿过组合逻辑（{len(detail)} 组），dofile 声明无法消除"}


def derive_icg_test_enable_eco(netlist_text, lib_meta, mode_port, top=None):
    """
    ICG 测试使能（TE/SCE）被功能组合逻辑占据的确定性 ECO（DFTR9）。

    以 case6 形态为例（clk_gate_pwm/SCE 由 U903 and2 输出 n_en1 驱动）：
        disconnect_net  u_pwm/n_en1  u_pwm/clk_gate_pwm/SCE
        add_net         u_pwm/n_tsel
        add_net         u_pwm/n_hi
        add_inst        u_pwm/U910  <mux2_1>
        add_inst        u_pwm/U911  <conb_1>
        connect_net     u_pwm/n_tsel  {u_pwm/U910/X  u_pwm/clk_gate_pwm/SCE}
        connect_net     u_pwm/n_en1   u_pwm/U910/A0
        connect_net     u_pwm/n_hi    {u_pwm/U911/HI  u_pwm/U910/A1}
        connect_pin     -from <mode>  -to u_pwm/U910/S
    """
    if not netlist_text or not mode_port:
        return None
    mux = _pick_lib_cell((lib_meta or {}).keys(), _MUX_NAME_HINTS)
    tie = _pick_lib_cell((lib_meta or {}).keys(), _TIEHI_NAME_HINTS)
    if not mux or not tie:
        return None

    insts = _parse_insts(netlist_text, lib_meta)
    prefixes = _hierarchy_prefixes(insts, lib_meta, top)
    seq = _seq_cells(lib_meta)
    drivers = {}
    for mod, inst, cell, pins in insts:
        meta = (lib_meta or {}).get(cell) or {}
        for pin, net in pins.items():
            if pin in (meta.get("out_pins") or set()) and net:
                drivers.setdefault((mod, net), []).append((inst, pin, cell))

    existing = {i for _m, i, _c, _p in insts}
    lines, detail = [], []
    icg_hit = 0
    for mod, inst, cell, pins in insts:
        meta = (lib_meta or {}).get(cell) or {}
        if not _is_icg_cell(cell, meta):
            continue
        # ICG 的测试使能脚：库里的 scan_pins 里带 E 的那个（SCE / TE）
        te_pins = [p for p in (meta.get("scan_pins") or set())
                   if re.fullmatch(r"(SCE|TE)(_B)?", p, re.I)]
        if not te_pins:
            continue
        te = sorted(te_pins)[0]
        te_net = pins.get(te)
        if not te_net:
            continue
        src = drivers.get((mod, te_net)) or []
        if not src:
            continue                      # 悬空 -> 另一类问题（伪主输入），不在这里修
        dinst, dpin, dcell = src[0]
        if dcell in seq:
            continue                      # 上一级 FF 驱动 TE：不是「被功能组合链占据」
        if mod not in prefixes:
            continue                      # 层次路径有歧义/不可达 -> 该处跳过
        pfx = prefixes[mod]
        n_tsel = te_net + "_tsel"
        n_hi = te_net + "_hi"
        icg_hit += 1
        mux_name = _fresh_inst("", "eco_scmux", existing)
        existing.add(mux_name)
        tie_name = _fresh_inst("", "eco_tie1", existing)
        existing.add(tie_name)

        n_te_net = _fp(pfx, te_net)
        n_te_pin = _fp(pfx, f"{inst}/{te}")
        n_drv = _fp(pfx, f"{dinst}/{dpin}")
        n_tsel_p, n_hi_p = _fp(pfx, n_tsel), _fp(pfx, n_hi)
        n_mux, n_tie = _fp(pfx, mux_name), _fp(pfx, tie_name)
        lines += [
            f"# ICG 测试使能 ECO（{n_te_pin} 被 {n_drv} 驱动，测试态无法置 1）",
            f"disconnect_net  {n_te_net}  {n_te_pin}",
            f"add_net         {n_tsel_p}",
            f"add_net         {n_hi_p}",
            f"add_inst        {n_mux}   {mux}",
            f"add_inst        {n_tie}   {tie}",
            f"connect_net     {n_tsel_p}  {{{n_mux}/X  {n_te_pin}}}",
            f"connect_net     {n_te_net}  {n_mux}/A0",
            f"connect_net     {n_hi_p}    {{{n_tie}/HI  {n_mux}/A1}}",
            f"connect_pin     -from {mode_port}  -to {n_mux}/S",
        ]
        detail.append({"kind": "icg_test_enable", "module": mod, "prefix": pfx,
                       "icg": n_te_pin, "te_pin": te, "driver": n_drv,
                       "mux": n_mux})
    if not detail:
        return None
    return {"groups": detail, "lines": lines,
            "reason": f"ICG 测试使能被功能组合链占据（{len(detail)} 处）"}


def _guess_top(netlist_text, lib_meta):
    """兜底推断顶层模块：未被任何模块实例化的那个（与 netlist_facts 同口径）。"""
    insts = _parse_insts(netlist_text, lib_meta)
    libs = set((lib_meta or {}).keys())
    mods, instantiated = [], set()
    for m in re.finditer(r"(?m)^\s*module\s+([A-Za-z_]\w*)", netlist_text):
        mods.append(m.group(1))
    for _mod, _inst, cell, _pins in insts:
        if cell not in libs:
            instantiated.add(cell)
    for m in mods:
        if m not in instantiated:
            return m
    return mods[0] if mods else ""


def derive_netlist_eco(task, netlist_text, lib_meta, parsed=None, mode_port=None,
                       top=None):
    """
    综合入口：按 DRC 规则挑 ECO 类型并合成完整命令块。

    Returns:
        dict 或 None：{"lines": [...], "groups": [...], "needs_mode_port": bool}
    """
    rules = _violation_rules(parsed)
    mode_port = mode_port or ((task or {}).get("mode_port") or "")
    clock_port = ((task or {}).get("scan_clock_port") or "")
    if not mode_port:
        # 没有确定的测试模式端口就不要生成 ECO（S 端无来源，必然接错）
        return None

    if not top:
        top = (task or {}).get("top_module") or _guess_top(netlist_text, lib_meta)
    parts, groups = [], []
    used_clock = ""
    if rules & {"DFTR1"} or not rules:
        # spec 没写明扫描时钟时从网表反推（否则 A1 无来源、整条 DFTR1 路径失效）
        cp = clock_port or discover_clock_port(netlist_text, lib_meta, top)
        if cp != clock_port:
            print(f"  ↳ 扫描时钟端口未在 spec 中明确，按网表反推为 '{cp}'")
        used_clock = cp
        got = derive_clock_path_eco(netlist_text, lib_meta, mode_port, cp, top)
        if got:
            parts += got["lines"]
            groups += got["groups"]
    if rules & {"DFTR9"} or not rules:
        got = derive_icg_test_enable_eco(netlist_text, lib_meta, mode_port, top)
        if got:
            parts += got["lines"]
            groups += got["groups"]
    if not parts:
        return None
    return {"lines": parts, "groups": groups, "clock_port": used_clock}

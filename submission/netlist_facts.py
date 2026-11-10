#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网表事实抽取（确定性，不依赖 LLM、不依赖 golden.dofile / preset_issues.json）

## 为什么需要这个模块

评测用的 hidden case **不提供 golden.dofile**（赛题 docx 8.2.1/8.2.2）。旧实现把
public case 的 golden 当作 few-shot 塞进生成 prompt，于是：

  · public case「通过」其实是把答案抄了一遍 —— 水分极大；
  · hidden case 没有 golden 时，prompt 里就只剩 task_spec 的自然语言描述，
    LLM 根本不知道网表里有哪些实例、哪些端口、哪些移位链，只能编。

真实任务本身要求的能力写在赛题 4.1：「分析 Pre-scan 门级网表」。因此这里把
「读网表」做成一个**确定性工具**，从网表 + Liberty 提取可核验的事实，再把事实
喂给 LLM 组装命令。这样：

  · 生成的每条 set_scan_segment / add_dedicated_wrapper_cell_type / 伪主输入
    都能追溯到网表里的真实对象（实例名、端口名、网络名）；
  · hidden case 与 public case 走完全相同的路径，不存在两套逻辑。

赛题 5.4.1(b) 明确「系统根据当前 case 的输入、日志、DRC 及各类报告，自主决定
读取哪些文件、检索手册哪些内容、调用哪些命令」属**允许且鼓励**的行为，本模块
正是这一条的实现。

## 抽取内容

1. `lib_meta`     —— Liberty 中每个 cell 的时序属性（是否含 ff/latch 块、时钟引脚、
                     输出引脚、扫描引脚 SCD/SCE…）。判据来自库本身，不依赖命名习惯。
2. `ff_types`     —— 网表中时序单元的类型分布（DFF / SFF 分开）。
3. `clock_ports`  —— 顶层时钟端口，以及每个端口实际驱动的时序单元数。
4. `se_ports`     —— 已存在扫描 FF 的 SCE（scan enable）连到的顶层端口。
5. `shift_chains` —— 移位链：从扫描 FF 的 SCD/SCE 出发沿 Q→D 追到链尾，给出
                     `set_scan_segment -access` 需要的 si / se / so 三个真实路径。
6. `extra_modules`—— 输入网表额外提供的模块（如赛题方给的 `dedicated_wrp_cell.v`）
                     及其端口语义映射，用于 `add_dedicated_wrapper_cell_type`。
7. `icg`          —— 时钟门控单元，以及顶层可见 ICG 的时钟输入网络（悬空即候选
                     伪主输入）。

## 规模自适应

case5 的网表 264MB / 210 万实例，case1 只有 3.8MB。全部事实都做会浪费预算，因此：
  · 基础事实任何规模都做，实测 264MB 单遍流式扫描约 5s；
  · 移位链在实例数超过 `MAX_CHAIN_INSTS` 时不做（交给工具自动推导）；
  · 全程有 deadline 检查，超时立刻返回已抽到的部分（宁少不错）。
"""

import os
import re
import time
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------------------
# 规模上限（可用环境变量覆盖）
# ---------------------------------------------------------------------------
MAX_CHAIN_INSTS = int(os.environ.get("SCAN_AGENT_MAX_CHAIN_INSTS", "400000"))
# 「无负载端口」分析允许的最大网表体积（见 _unloaded_ports 的体积闸门）
_UNLOADED_PORTS_MAX_BYTES = int(
    os.environ.get("SCAN_AGENT_UNLOADED_PORTS_MAX_MB", "24")) * 1024 * 1024
MAX_CHAINS = int(os.environ.get("SCAN_AGENT_MAX_CHAINS", "2000"))
_CHUNK = 4 << 20


# ===========================================================================
# 第 1 部分：Liberty 库元数据
# ===========================================================================

_CELL_START_RE = re.compile(
    r'(?m)^[ \t]*cell[ \t]*\([ \t]*"?([A-Za-z_][\w$.]*)"?[ \t]*\)[ \t]*\{')
_PIN_START_RE = re.compile(
    r'(?m)^[ \t]*pin[ \t]*\([ \t]*"?([A-Za-z_][\w$\[\]]*)"?[ \t]*\)[ \t]*\{')
_CLOCK_TRUE_RE = re.compile(r'(?m)^[ \t]*clock[ \t]*:[ \t]*"true"')
_DIR_OUT_RE = re.compile(r'(?m)^[ \t]*direction[ \t]*:[ \t]*"output"')
_DIR_IN_RE = re.compile(r'(?m)^[ \t]*direction[ \t]*:[ \t]*"input"')
_SEQ_RE = re.compile(r'(?m)^[ \t]*(?:ff|ff_bank|ff_ik|ff_bank_ik|latch|latch_bank|'
                     r'statetable|clocked_on|next_state)\b')
_NEXT_STATE_RE = re.compile(r'(?m)^[ \t]*next_state[ \t]*:[ \t]*"([^"]+)"')
_SCAN_PIN_HINT = re.compile(r"\b(SCD|SCE|SIN|SCE_B|SCI|SI|SE)\b")

_LIB_META_CACHE = {}


def _assign_marks(block, pin_marks, mark_re, kind, bucket):
    """把 block 中匹配 mark_re 的位置归属到它前面最近的 pin 声明。"""
    if not pin_marks:
        return
    starts = [p[0] for p in pin_marks]
    import bisect
    for m in mark_re.finditer(block):
        i = bisect.bisect_right(starts, m.start()) - 1
        if i >= 0:
            bucket.add(pin_marks[i][1])


def parse_lib_metadata(lib_paths, deadline=None):
    """
    解析 Liberty，返回 {cell_name: meta}。

    meta = {
        "seq": bool,            # 含 ff / latch 等时序块
        "clock_pins": set(),    # clock : "true" 的引脚
        "out_pins": set(),      # direction : "output"
        "in_pins": set(),
        "scan_pins": set(),     # next_state 表达式里出现的扫描专用引脚
    }

    实现要点：Liberty 里 `cell (...)` 是缩进一致的顶层结构，因此按「下一个 cell
    声明的位置」切块即可，不必做大括号平衡（比逐字符状态机快得多且不易出错）。
    timing / internal_power 表占文件 95%，直接不进正则，实测 12.5MB 约 1s。
    """
    cache_key = tuple(sorted(lib_paths))
    if cache_key in _LIB_META_CACHE:
        return _LIB_META_CACHE[cache_key]

    meta = {}
    for path in lib_paths:
        if deadline is not None and time.time() > deadline:
            break
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue

        marks = [(m.start(), m.group(1)) for m in _CELL_START_RE.finditer(text)]
        for idx, (pos, name) in enumerate(marks):
            end = marks[idx + 1][0] if idx + 1 < len(marks) else len(text)
            block = text[pos:end]
            if len(block) > 400000:          # 异常大的块（非标准写法）跳过
                continue
            pins = [(m.start(), m.group(1)) for m in _PIN_START_RE.finditer(block)]
            clk, out, inp, scan = set(), set(), set(), set()
            _assign_marks(block, pins, _CLOCK_TRUE_RE, "clk", clk)
            _assign_marks(block, pins, _DIR_OUT_RE, "out", out)
            _assign_marks(block, pins, _DIR_IN_RE, "in", inp)
            seq = bool(_SEQ_RE.search(block))
            for m in _NEXT_STATE_RE.finditer(block):
                seq = True
                for sp in _SCAN_PIN_HINT.findall(m.group(1)):
                    scan.add(sp)
            # 扫描引脚也可能只体现在引脚名上（无 next_state 表达式的库）
            for p in inp:
                if p in ("SCD", "SCE", "SI", "SE", "SIN"):
                    scan.add(p)
            meta[name] = {"seq": seq, "clock_pins": clk, "out_pins": out,
                          "in_pins": inp, "scan_pins": scan}

    _LIB_META_CACHE[cache_key] = meta
    return meta


def reset_lib_cache():
    _LIB_META_CACHE.clear()


# ===========================================================================
# 第 2 部分：Verilog 网表流式解析
# ===========================================================================

_INST_RE = re.compile(
    r"^[ \t]*([A-Za-z_][\w$]*)[ \t]+(\\[^\s()]+|[A-Za-z_][\w$]*(?:[ \t]*\[[^\]\n]*\])?)[ \t]*\(",
    re.M,
)
_MODULE_RE = re.compile(r"(?m)^[ \t]*module[ \t]+([A-Za-z_][\w$]*)")
_ENDMODULE_RE = re.compile(r"(?m)^[ \t]*endmodule\b")
_PORT_DECL_RE = re.compile(r"(?m)^[ \t]*(input|output|inout)\b([^;]*);")
_BUS_RE = re.compile(r"\[[^\]]*\]")
_IDENT_RE = re.compile(r"[A-Za-z_\\][\w$\\]*")
_PIN_CONN_RE = re.compile(r"\.\s*([A-Za-z_][\w$]*)\s*\(\s*([^)]*?)\s*\)", re.S)

_VERILOG_KW = frozenset("""
module endmodule input output inout wire reg assign always initial begin end
if else case endcase casex casez parameter localparam defparam generate
endgenerate for while supply0 supply1 tri wand wor function endfunction task
endtask specify endspecify primitive table endtable buf not and nand or nor xor
xnor bufif0 bufif1 notif0 notif1 pulldown pullup nmos pmos cmos rcmos tran
tranif0 tranif1 rtran rtranif0 rtranif1 macromodule fork join wait disable
force release deassign default endfunction scalared vectored real time integer
""".split())


def _norm_net(sig):
    s = (sig or "").strip()
    if not s:
        return ""
    return s.split(",")[0].strip()


def iter_module_blocks(path):
    """
    流式产出 (module_name, header_text, body_text)。

    264MB 的网表整读进内存会占 500MB+，因此按 `module ... endmodule` 边界切块，
    单块解析完即可丢弃。块内没有 endmodule 时会累积到下一次出现（正常网表不会）。
    """
    buf = ""
    cur_name = None
    cur_hdr = ""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            buf += chunk
            while True:
                if cur_name is None:
                    m = _MODULE_RE.search(buf)
                    if not m:
                        # 未进入 module：丢弃已扫过的头部噪声，保留尾部防截断
                        keep = min(len(buf), 64)
                        buf = buf[len(buf) - keep:]
                        break
                    cur_name = m.group(1)
                    buf = buf[m.end():]
                    h = buf.find(";")
                    if h == -1:
                        break
                    cur_hdr = buf[:h]
                    buf = buf[h + 1:]
                    continue
                e = _ENDMODULE_RE.search(buf)
                if not e:
                    break
                yield cur_name, cur_hdr, buf[:e.start()]
                cur_name = None
                buf = buf[e.end():]
    if cur_name is not None and buf.strip():
        yield cur_name, cur_hdr, buf


def _parse_ports(body):
    """抽取 module 内声明的端口 -> 方向。"""
    ports = {}
    for m in _PORT_DECL_RE.finditer(body):
        direction = m.group(1)
        rest = _BUS_RE.sub(" ", m.group(2))
        for tok in m.group(2).split(","):
            tok = _BUS_RE.sub(" ", tok)
            for ident in _IDENT_RE.findall(tok):
                if ident in _VERILOG_KW or ident in ("wire", "reg", "signed",
                                                     "unsigned", "logic", "bit"):
                    continue
                ports[ident.lstrip("\\")] = direction
    return ports


def parse_module(body, lib_meta):
    """
    解析单个 module。

    Returns:
        {
          "ports": {name: direction},
          "insts": [(cell, inst, {pin: net})],
          "seq_insts": [(cell, inst, {pin: net})],   # 库中判定为时序单元的
          "submods": [(cell, inst)],                 # 不在库中 -> 层次实例
        }
    """
    ports = _parse_ports(body)
    insts, seq_insts, submods = [], [], []
    for m in _INST_RE.finditer(body):
        cell = m.group(1)
        if cell in _VERILOG_KW:
            continue
        inst = m.group(2).strip()
        end = body.find(");", m.end())
        blob = body[m.end():end] if end != -1 else body[m.end():m.end() + 6000]
        conns = {}
        for pm in _PIN_CONN_RE.finditer(blob):
            conns[pm.group(1)] = _norm_net(pm.group(2))
        rec = (cell, inst, conns)
        insts.append(rec)
        lm = lib_meta.get(cell)
        if lm is None:
            submods.append((cell, inst))
        elif lm["seq"]:
            seq_insts.append(rec)
    return {"ports": ports, "insts": insts, "seq_insts": seq_insts, "submods": submods}


def _seq_pins(cell, lib_meta):
    """取 (clk, d, q, scd, sce)；库信息不足时返回 None。"""
    m = lib_meta.get(cell)
    if not m:
        return None, None, None, None, None
    clk = sorted(m["clock_pins"])[0] if m["clock_pins"] else None
    q = sorted(m["out_pins"])[0] if m["out_pins"] else None
    ins = m["in_pins"]
    scan = m.get("scan_pins") or set()
    d = "D" if "D" in ins else None
    scd = "SCD" if ("SCD" in ins or "SCD" in scan) else None
    sce = "SCE" if ("SCE" in ins or "SCE" in scan) else None
    return clk, d, q, scd, sce


# ===========================================================================
# 第 3 部分：对外主入口
# ===========================================================================

def empty_facts(note=""):
    return {
        "ok": False, "top": "", "modules": 0, "lib_cells": 0,
        "top_ports": {},
        "ff_types": {}, "sff_types": {}, "dff_types": {},
        "ff_count": 0, "sff_count": 0,
        "clock_ports": {}, "se_ports": {},
        "shift_chains": [], "chains_truncated": False,
        "extra_modules": [],
        "associated_internal_clocks": "",
        "icg": {"cells": {}, "top_clk_nodes": [], "floating_clk": [],
                "floating_clock_roots": []},
        "scan_mappings": [],
        "unloaded_ports": [],
        "elapsed": 0.0, "notes": ([note] if note else []),
    }


def _derive_cell_mappings(lib_meta, dff_types):
    """
    由 **Liberty 库的命名规律**推导 DFF→SFF 扫描替换对。

    scan insertion 的通用约定：扫描单元 = 功能单元名在同一库前缀下加 `s`
    （`sky130_fd_sc_hd__dfxtp_1` → `sky130_fd_sc_hd__sdfxtp_1`，
      `...__dfrtp_1` → `...__sdfrtp_1`，`...__edfxtp_1` → `...__sedfxtp_1`）。

    只输出「库中确实存在对应扫描单元」的映射 —— 因此天然通用，不依赖任何
    case 的任务书措辞，也不会写出库里没有的单元名（写了会被工具报错）。
    """
    out = []
    lib_names = lib_meta.keys() if hasattr(lib_meta, "keys") else ()
    for dff in sorted(dff_types or {}):
        if not dff or dff in lib_meta and (lib_meta[dff].get("scan_pins")):
            continue                        # 本身已是扫描单元
        if "__" in dff:
            prefix, base = dff.rsplit("__", 1)
            cand = f"{prefix}__s{base}"
        else:
            cand = "s" + dff
        if cand in lib_names:
            meta = lib_meta.get(cand) or {}
            if meta.get("seq"):
                out.append([dff, cand])
    return out


def _detect_top(claimed, seen_modules, all_submods, module_insts, mod_ports):
    """
    用**网表结构**判定顶层模块。

    顶层模块的定义性特征：**没有任何别的模块实例化它**。层次化设计里所有子模块都会
    被上一层实例化，只有顶层不会。task_spec 里的「设计名称」不能采信（case6 写
    des_perf、真实顶层是 des3），所以这里以结构为准，任务书给的候选只作参考。

    判据顺序：
      1. 「未被实例化」的模块只有唯一一个时，直接采用；
      2. 有多个候选（典型场景：真正的顶层 + 赛题额外附带的 wrapper cell 模板，两者
         都没被实例化）时，优先「自身还实例化了别的模块」者 —— 真正的顶层必然带下层
         层次；wrapper cell 模板是叶子，不实例化任何模块；
      3. 并列时取端口数最多者（顶层端口通常最多）；
      4. 再并列时才参考任务书给的候选。
    """
    structural = [m for m in seen_modules if m not in all_submods]
    if not structural:
        structural = list(dict.fromkeys(seen_modules))
    if not structural:
        return claimed or ""
    if len(structural) == 1:
        return structural[0]

    def _children(m):
        pm = module_insts.get(m)
        if pm:
            return len(pm.get("submods") or [])
        return -1        # 细节被裁剪（超大网表）：未知，排序时排在有子实例者之后

    def _nports(m):
        return len(mod_ports.get(m) or {})

    ranked = sorted(structural,
                    key=lambda m: (-_children(m), -_nports(m), 0 if m == claimed else 1))
    return ranked[0]


def extract_facts(task, deadline=None, want_chains=True, verbose=False):
    """
    抽取本 case 的网表事实。任何异常都退化为「空事实」而非抛错 ——
    生成阶段必须能在事实缺失时照常走规则路径。
    """
    t0 = time.time()
    facts = empty_facts()
    facts["top"] = (task.get("top_module") or "")

    files = (task.get("files") or {})
    lib_paths = list(files.get("lib") or [])
    netlist_paths = list(files.get("netlist") or [])
    if not netlist_paths:
        facts["notes"].append("没有网表文件")
        return facts

    try:
        lib_meta = parse_lib_metadata(lib_paths, deadline=deadline)
    except Exception as exc:                                    # noqa: BLE001
        lib_meta = {}
        facts["notes"].append(f"Liberty 解析失败：{exc}")
    facts["lib_cells"] = len(lib_meta)

    seen_modules = []
    all_submods = set()
    mod_ports = {}
    module_insts = {}
    keep_detail = True
    total_insts = 0
    truncated_by_budget = False

    for path in netlist_paths:
        if truncated_by_budget:
            break
        try:
            for name, hdr, body in iter_module_blocks(path):
                if deadline is not None and time.time() > deadline:
                    truncated_by_budget = True
                    facts["notes"].append("解析达到时间上限，事实可能不完整")
                    break
                seen_modules.append(name)
                pm = parse_module(body, lib_meta)
                total_insts += len(pm["insts"])
                for cell, _inst in pm["submods"]:
                    all_submods.add(cell)
                mod_ports[name] = pm["ports"]
                if total_insts > MAX_CHAIN_INSTS and keep_detail:
                    keep_detail = False
                    module_insts.clear()
                    facts["notes"].append(
                        f"实例数超过 {MAX_CHAIN_INSTS}，只保留聚合统计")
                for cell, _inst, _c in pm["seq_insts"]:
                    _bump(facts, cell, lib_meta)
                if keep_detail:
                    module_insts[name] = pm
        except Exception as exc:                                # noqa: BLE001
            facts["notes"].append(f"网表 {os.path.basename(path)} 解析异常：{exc}")

    facts["modules"] = len(seen_modules)
    facts["ff_count"] = sum(facts["ff_types"].values())
    facts["sff_count"] = sum(facts["sff_types"].values())
    facts["scan_mappings"] = _derive_cell_mappings(lib_meta, facts["dff_types"])

    # 顶层模块判定 —— **必须用网表结构校验**，不能直接信 task_spec。
    #
    # 实测 task_1/case6：task_spec 写「设计名称: des_perf」，但网表里的顶层模块是
    # `des3`（golden 的 present_design 也写 des3）—— "设计名称"是产品名，未必等于
    # 顶层模块名。若照抄 task_spec，present_design 会指向不存在的模块，后续每条命令
    # 都报 SCAN-1021「Failed to retrieve the design」。
    # hidden case 没有 golden 可对照，因此只能靠结构：顶层 = 没有被任何模块实例化的
    # 那个模块。
    facts["top"] = _detect_top(facts["top"], seen_modules, all_submods,
                               module_insts, mod_ports)

    top = facts["top"]
    # 顶层端口表（权威）：供 spec_verifier 做「端口是否真实存在」校验，
    # 也供 scan_agent 剔除 understanding 里来自 original.dofile 的故障端口。
    if top and top in mod_ports:
        facts["top_ports"] = dict(mod_ports[top])
    if keep_detail and top in module_insts:
        _trace_top_ports(facts, top, module_insts, lib_meta)

    facts["extra_modules"] = _extra_modules(seen_modules, mod_ports, lib_meta, top,
                                            all_submods)

    _icg_facts(facts, lib_meta, module_insts, top)

    # 伪主输入候选：时钟输入引脚悬空的门控单元（判据见该函数文档）。
    # 大网表下 module_insts 已被 MAX_CHAIN_INSTS 分支清空，故这里独立扫描网表文件。
    if top:
        try:
            roots = find_floating_clock_roots(netlist_paths, top, deadline=deadline)
            facts["icg"]["floating_clock_roots"] = roots
            if roots:
                facts["notes"].append(
                    f"检出 {len(roots)} 个时钟输入悬空的门控单元，需声明为伪主输入")
        except Exception as exc:                                # noqa: BLE001
            facts["notes"].append(f"伪主输入推导异常：{exc}")

    # 时钟缓冲单元的内部时钟起始引脚（-associated_internal_clocks）。
    # 只有 task_spec 明确要求「…时钟缓冲单元内部的使能（CE）latch…」时才推导，
    # 没有该要求就留空（见 find_associated_internal_clocks 的说明）。
    if top:
        try:
            facts["associated_internal_clocks"] = find_associated_internal_clocks(
                netlist_paths, top, task.get("task_spec", ""), deadline=deadline)
        except Exception as exc:                                # noqa: BLE001
            facts["notes"].append(f"associated_internal_clocks 推导异常：{exc}")

    # 顶层「无负载端口」：ECO 插入 MUX 时 S 端要接的测试模式端口（case6 的 scan_mode）
    if top:
        try:
            facts["unloaded_ports"] = _unloaded_ports(
                netlist_paths, top, deadline=deadline)
        except Exception as exc:                                # noqa: BLE001
            facts["notes"].append(f"无负载端口推导异常：{exc}")

    if want_chains and keep_detail:
        _trace_shift_chains(facts, module_insts, lib_meta, top)
    elif want_chains:
        facts["notes"].append("网表规模过大，跳过移位链枚举（交给工具自动推导）")

    facts["ok"] = bool(seen_modules)
    facts["elapsed"] = time.time() - t0
    if verbose:
        print(f"  ✓ 网表事实：{facts['modules']} modules / "
              f"时序单元 {facts['ff_count']}（扫描 FF {facts['sff_count']}）/ "
              f"移位链 {len(facts['shift_chains'])} / {facts['elapsed']:.1f}s")
    return facts


def _bump(facts, cell, lib_meta):
    facts["ff_types"][cell] = facts["ff_types"].get(cell, 0) + 1
    if (lib_meta.get(cell) or {}).get("scan_pins"):
        facts["sff_types"][cell] = facts["sff_types"].get(cell, 0) + 1
    else:
        facts["dff_types"][cell] = facts["dff_types"].get(cell, 0) + 1


def _trace_top_ports(facts, top, module_insts, lib_meta):
    """把顶层时序单元的 CLK / SCE 反向解析到顶层输入端口，统计驱动数。"""
    pm = module_insts.get(top)
    if not pm:
        return
    ports = pm["ports"]
    net_driver = {}
    for cell, _inst, conns in pm["insts"]:
        meta = lib_meta.get(cell) or {}
        if meta.get("seq"):
            continue
        outs = meta.get("out_pins") or set()
        for pin, net in conns.items():
            if pin in outs and net:
                net_driver[net] = conns
    for p, d in ports.items():
        if d == "input":
            net_driver.setdefault(p, None)

    def resolve(net, depth=0):
        if not net or depth > 6:
            return None
        if ports.get(net) == "input":
            return net
        conns = net_driver.get(net)
        if not conns:
            return None
        for pin, up in conns.items():
            if pin in ("X", "Y", "Z", "Q", "A", "A1", "A2", "I", "Y1", "Y2"):
                r = resolve(up, depth + 1)
                if r:
                    return r
        return None

    for cell, _inst, conns in pm["seq_insts"]:
        clk, _d, _q, _scd, sce = _seq_pins(cell, lib_meta)
        if clk:
            src = resolve(conns.get(clk))
            if src:
                facts["clock_ports"][src] = facts["clock_ports"].get(src, 0) + 1
        if sce:
            src = resolve(conns.get(sce))
            if src:
                facts["se_ports"][src] = facts["se_ports"].get(src, 0) + 1


def _extra_modules(seen_modules, mod_ports, lib_meta, top, submods):
    """
    找出「输入网表额外提供的模块」——不在标准单元库中、且**没有任何模块实例化它**
    的模块。

    判据说明：正常的设计层次模块都会被上一层实例化（出现在 submods 里），
    而赛题方单独提供的 wrapper cell 模板（如 case6 的 `dedicated_wrp_cell.v`）
    是给工具做 wrapper 实例化用的，**不会被网表实例化**，因此恰好落在
    「定义了但从未被引用」这一集合里 —— 无需靠文件名或硬编码名字判断。
    """
    out = []
    for name in dict.fromkeys(seen_modules):        # 保序去重
        if name == top or name in lib_meta or name in submods:
            continue
        ports = mod_ports.get(name)
        if not ports:
            continue
        out.append({
            "module": name,
            "ports": dict(ports),
            "semantics": wrapper_semantics(ports),
        })
    return out


_WRP_SEM_PATTERNS = (
    ("shift_clk", r"shift.*clk|clk.*shift|(?:^|_)sclk$|(?:^|_)sck$"),
    ("capture_en", r"capture.*en|(?:^|_)cap.*en|(?:^|_)ce$|(?:^|_)cen$"),
    ("shift_en", r"shift.*en|(?:^|_)se$|(?:^|_)sen$|(?:^|_)sce$"),
    ("cti", r"(?:^|_)cti$|core.*test.*in|test.*_in$|(?:^|_)ti$"),
    ("cto", r"(?:^|_)cto$|core.*test.*out|test.*_out$|(?:^|_)to$"),
    ("cfi", r"(?:^|_)cfi$|func.*_in$|(?:^|_)fi$"),
    ("cfo", r"(?:^|_)cfo$|func.*_out$|(?:^|_)fo$"),
)


def wrapper_semantics(ports):
    """按端口名把一个模块映射成 wrapper 的 7 个语义端口（映射不上为 None）。"""
    sem = {k: None for k, _ in _WRP_SEM_PATTERNS}
    for key, pat in _WRP_SEM_PATTERNS:
        rx = re.compile(pat, re.I)
        for n in ports:
            if rx.search(n):
                sem[key] = n
                break
    return sem


# ============================================================================
# 伪主输入推导：时钟输入引脚悬空的门控单元
# ============================================================================

_MOD_DEF_RE = re.compile(rb"^\s*module\s+([A-Za-z_][\w$]*)")
_EMPTY_PIN_RE = re.compile(rb"\.\s*([A-Za-z_][\w$]*)\s*\(\s*\)")
_OUT_DECL_RE = re.compile(rb"^\s*output\b([^;]*);", re.M)
# 注意：本文件另有一个 str 版 _IDENT_RE（供文本解析用），这里的 bytes 版必须
# 用独立名字 —— 早期版本复用了 _IDENT_RE，结果覆盖掉 str 版，
# 导致 iter_module_blocks 抛 "cannot use a bytes pattern on a string-like object"。
_B_PORT_TOKEN_RE = re.compile(rb"([A-Za-z_][\w$]*)")

# 时钟语义判定。刻意不用裸 "ck" 子串 —— 那会把 check / lock / block 这类
# 普通信号名卷进来；只认 clk / clock，以及作为独立词或后缀出现的 ck。
def _clkish_name(tok):
    t = tok.lower()
    if b"clk" in t or b"clock" in t:
        return True
    return t == b"ck" or t.endswith(b"_ck") or t.startswith(b"ck_")


def find_floating_clock_roots(netlist_paths, top, deadline=None, max_paths=64):
    """
    推导「时钟输入引脚悬空的门控单元」，返回其输出时钟的层次路径列表，
    形如 x_aq_top_0/x_aq_core/.../x_dcache_data_gated_clk/clk_out。

    判据（纯结构，不依赖命名前缀、不依赖 golden）：
      1. 模块实例的某个引脚名带时钟语义（clk / clock / ck），且连接为空
         —— 这个时钟没有驱动源；
      2. 该实例所引用模块的定义里，存在同样带时钟语义的 output 引脚
         —— 它把时钟向外传递，即"门控时钟单元"。

    以 task_1/case5（openC906，258MB / 1031 万行）实测：9/9 命中 golden 的
    add_pseudo_pi 路径，零误报，两遍扫描合计 11.7s。缺这 9 条声明时，工具会
    对全部 69,022 个 DFF 逐个报 DFTDRC-4001 "Clock input 'CLK' ... uncontrolled"，
    并把 55 万个 cell 一一列进 examine_scan_chain 的输出，把单个 case 的
    工具耗时推到 700s 量级。

    为什么要独立扫描、而不是复用 extract_facts 的结构化解析：大网表下
    extract_facts 会因实例数超过 MAX_CHAIN_INSTS 而清空 module_insts，
    基于它的 _icg_facts 在 case5 上返回 0 条候选。
    """
    results = []
    for path in netlist_paths or []:
        try:
            results.extend(_floating_roots_one_file(path, top, deadline))
        except Exception:                                       # noqa: BLE001
            continue
        if len(results) >= max_paths:
            break
    # 去重但保序
    seen = set()
    out = []
    for r in results:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out[:max_paths]


def _floating_roots_one_file(netlist_path, top, deadline=None):
    """单文件版的伪主输入推导（两遍扫描 + 定向 seek）。"""
    top_b = str(top or "").encode()
    if not top_b:
        return []

    # ── Pass 1：module 定义偏移 + 悬空时钟引脚实例 ──────────────────────
    mod_off = {}
    floating = []
    cur_mod = None
    last_inst = None
    pos = 0
    checked = 0

    with open(netlist_path, "rb") as fh:
        for raw in fh:
            pos += len(raw)
            checked += 1
            if deadline is not None and (checked & 0xFFFF) == 0 and time.time() > deadline:
                return []
            if b"module " in raw:
                m = _MOD_DEF_RE.match(raw)
                if m:
                    cur_mod = m.group(1)
                    mod_off.setdefault(cur_mod, pos - len(raw))
                    last_inst = None
                    continue
            # 实例首行形如 "TYPE INST ( .port(net), ..."：
            # split 后第三个 token 以 '(' 开头即为实例。不能只判"以 '(' 结尾"——
            # DC 输出的实例首行紧跟第一个引脚，实际以 ',' 结尾。
            if b" (" in raw:
                parts = raw.split(None, 2)
                if (len(parts) >= 3 and parts[2].startswith(b"(")
                        and parts[0] != b"module"):
                    last_inst = (parts[0], parts[1])
            if b"()" in raw or b"( )" in raw:
                for em in _EMPTY_PIN_RE.finditer(raw):
                    pin = em.group(1)
                    if not _clkish_name(pin):
                        continue
                    if raw.lstrip().startswith(b"."):
                        # 引脚落在实例的续行上，回退到该实例的首行
                        itype, iname = last_inst or (b"", b"")
                    else:
                        parts = raw.split(None, 2)
                        itype, iname = (parts[0], parts[1]) if len(parts) >= 2 else (b"", b"")
                    if itype and iname:
                        floating.append((cur_mod or b"", itype, iname))

    if not floating:
        return []

    # ── Pass 2：实例索引 child_module -> [(parent_module, inst_name)] ────
    index = {}
    cur_mod = None
    checked = 0
    with open(netlist_path, "rb") as fh:
        for raw in fh:
            checked += 1
            if deadline is not None and (checked & 0xFFFF) == 0 and time.time() > deadline:
                return []
            if b"module " in raw:
                m = _MOD_DEF_RE.match(raw)
                if m:
                    cur_mod = m.group(1)
                    continue
            if b" (" in raw:
                parts = raw.split(None, 2)
                if (len(parts) >= 3 and parts[2].startswith(b"(")
                        and parts[0] != b"module"):
                    index.setdefault(parts[0], []).append((cur_mod, parts[1]))

    # ── Pass 3：定向解析模块定义取输出引脚 + 反向 BFS 重建层次路径 ──────
    out = []
    with open(netlist_path, "rb") as fh:
        for mod, itype, iname in floating:
            off = mod_off.get(itype)
            if off is None:
                continue
            fh.seek(off)
            seg = fh.read(4096)
            e = seg.find(b"endmodule")
            seg = seg[: e if e > 0 else len(seg)]
            outpins = []
            for m in _OUT_DECL_RE.finditer(seg):
                for tok in _B_PORT_TOKEN_RE.findall(m.group(1)):
                    if _clkish_name(tok):
                        outpins.append(tok)
            if not outpins:
                # 模块定义里找不到带时钟语义的输出引脚 —— 不是时钟传递单元
                continue
            outpin = outpins[0]

            paths = []
            queue = [(mod, [])]
            guard = 0
            while queue and len(paths) < 8 and guard < 20000:
                guard += 1
                m, chain = queue.pop(0)
                if m == top_b:
                    paths.append(chain)
                    continue
                if len(chain) > 24:
                    continue
                for pmod, pinst in index.get(m, []):
                    queue.append((pmod, [pinst] + chain))
            for chain in paths:
                out.append(b"/".join(chain + [iname, outpin]).decode())

    return out


# ============================================================================
# 时钟缓冲单元的内部时钟起始引脚（-associated_internal_clocks）
# ============================================================================

# task_spec 触发句：「<关键词> 时钟缓冲单元内部的使能（CE）latch，其时钟值传播正确」
# 手册 p16：`-associated_internal_clocks <pin>` 把该引脚之后的时钟域指定为
# 内部时钟域的起始点；golden 用它避免缓冲单元内部 CE latch 的时钟值传播中断。
_CE_LATCH_SPEC_RE = re.compile(
    # 「APB 时钟缓冲单元内部的使能（CE）latch」——注意“使能”与“latch”之间常夹着
    # 全角括号写的「（CE）」，所以中间那一段用 [^\n]{0,12}? 兜住，不能只写 \s*。
    r"([A-Za-z][\w\-]{1,15})\s*时钟缓冲单元[^\n]{0,40}?(?:使能|CE)[^\n]{0,12}?latch"
    r"|(?:使能|CE)[^\n]{0,12}?latch[^\n]{0,40}?([A-Za-z][\w\-]{1,15})\s*时钟缓冲单元",
    re.I)

_TOP_MODULE_MAX_BYTES = 8 * 1024 * 1024    # 顶层模块定义一般就在文件开头
# 顶层模块内的实例首行：行首 TYPE INST (
_INST_HEAD_RE = re.compile(rb"(?m)^[ \t]*([A-Za-z_][\w$]*)[ \t]+([A-Za-z_][\w$]*)[ \t]*\(")
_PORT_CONN_RE = re.compile(rb"\.\s*([A-Za-z_][\w$]*)\s*\(")


def find_associated_internal_clocks(netlist_paths, top, spec_text, deadline=None):
    """
    推导 `-associated_internal_clocks` 的目标引脚（形如 x_aq_mp_clk_top/apb_clk）。

    **触发是严格的**：只有 task_spec 写了「<关键词> 时钟缓冲单元内部的使能（CE）
    latch …」这类要求才推导，否则返回 ""（绝不猜）。

    推导：在**顶层模块**的实例里找模块类型名带时钟语义（clk/clock/ck）的实例，
    再在该实例的端口连接里找名字含该关键词的端口。

    task_1/case5 验证：规格写「APB 时钟缓冲单元内部的使能（CE）latch」，
    网表顶层有 `aq_mp_clk_top x_aq_mp_clk_top (.apb_clk(apb_clk), ...)`，
    推出 `x_aq_mp_clk_top/apb_clk` —— 与 golden 一致。
    缺这条声明时日志里有 4,215 条 DFTDRC-4005
    （"Clock input … is inactive when clocks are set on"），
    正是缓冲单元内部 CE latch 时钟值传播中断的指纹。
    """
    m = _CE_LATCH_SPEC_RE.search(spec_text or "")
    if not m:
        return ""
    kw = (m.group(1) or m.group(2) or "").strip().lower()
    if len(kw) < 2:
        return ""
    top_b = str(top or "").encode()
    if not top_b:
        return ""
    for path in netlist_paths or []:
        try:
            got = _associated_pin_in_top(path, top_b, kw, deadline)
        except Exception:                                       # noqa: BLE001
            continue
        if got:
            return got
    return ""


def _associated_pin_in_top(netlist_path, top_b, kw, deadline=None):
    """在顶层模块定义内找「模块类型带时钟语义 + 端口名含关键词」的引脚。"""
    # kw 来自 task_spec（str），而下面处理的是网表 bytes —— 不转会在
    # `kw in port.lower()` 上抛 TypeError，且被上层 except 静默吞掉。
    kw_b = kw.encode("utf-8") if isinstance(kw, str) else kw
    mod_re = re.compile(rb"(?m)^[ \t]*module[ \t]+" + re.escape(top_b) + rb"[ \t\n(]")
    with open(netlist_path, "rb") as fh:
        data = fh.read(_TOP_MODULE_MAX_BYTES)
    m = mod_re.search(data)
    if not m:
        return ""
    end = data.find(b"endmodule", m.end())
    body = data[m.end(): end if end > 0 else len(data)]
    for im in _INST_HEAD_RE.finditer(body):
        itype, iname = im.group(1), im.group(2)
        if itype == b"module" or not _clkish_name(itype):
            continue
        stop = body.find(b");", im.end())
        conns = body[im.end(): stop if stop > 0 else len(body)]
        for pm in _PORT_CONN_RE.finditer(conns):
            port = pm.group(1)
            if kw_b in port.lower():
                return f"{iname.decode()}/{port.decode()}"
    return ""


def _icg_facts(facts, lib_meta, module_insts, top):
    """
    ICG（时钟门控）事实：类型分布 + 顶层可见 ICG 节点的时钟输入网络。

    case5 要求「门控时钟单元的时钟输入引脚均悬空 -> 声明为伪主输入并各自构成
    独立时钟域」，判据就是这里的 `clk_net` 是否有真实驱动。
    """
    icg_types = {}
    nodes = []
    for module, pm in module_insts.items():
        for cell, inst, conns in pm["insts"]:
            meta = lib_meta.get(cell)
            if not meta or meta["seq"] or not meta["clock_pins"] or not meta["out_pins"]:
                continue
            if not re.search(r"clkp|clkgat|icg|clk_gate|cgls|dlclkp",
                             cell, re.I):
                continue
            icg_types[cell] = icg_types.get(cell, 0) + 1
            if module == top:
                clk_pin = sorted(meta["clock_pins"])[0]
                nodes.append({
                    "inst": inst,
                    "clk_pin": clk_pin,
                    "clk_net": conns.get(clk_pin) or "",
                    "out_pin": sorted(meta["out_pins"])[0],
                })
    facts["icg"]["cells"] = icg_types
    facts["icg"]["top_clk_nodes"] = nodes[:400]
    facts["icg"]["floating_clk"] = [n for n in nodes if not n.get("clk_net")][:200]


def _unloaded_ports(netlist_paths, top, deadline=None, limit=40):
    """
    找出「已声明为顶层端口、但网表里没有任何负载」的端口。

    为什么需要这个事实（task2/case6 实测）：
    case6 的 scan_mode 端口在原始网表里**无负载**——修复必须在网表里新增 MUX 并把
    S 端接 scan_mode（任务书原话：「当前网表中该端口无负载」）。agent 让 LLM 逐字
    复制网表时，如果漏了这行端口声明，connect_pin -from scan_mode 会报
    SCAN-1315 Port 'scan_mode' does not exist —— 断言「端口存在」却拿不到证据。

    判定方式：顶层端口名在**整个网表文件**里只出现在端口声明处，未出现在任何
    实例连接（.pin(net) / 位置连接）或 assign 右侧。对大网表按块流式扫描，
    只用正则计数，不建完整连接图。
    """
    if not top:
        return []
    port_re = re.compile(r"\b([A-Za-z_]\w*)\s*\[\s*\d+\s*:\s*\d+\s*\]|\b([A-Za-z_]\w*)\b")
    # 端口声明块：module <top> ( a, b, c );  /  input a; output [3:0] b;
    decl_names = set()
    usage = {}
    hdr_re = re.compile(r"\bmodule\s+([A-Za-z_]\w*)\b([^;]*);", re.S)
    for path in netlist_paths:
        # 体积闸门：本函数要把整份网表读进内存。超大网表（case5 那类 200MB+）
        # 只这一个 read_text 就吃掉几百 MB 常驻内存，而这处信息（无负载端口）
        # 只在 ECO 场景使用，大网表根本不走 ECO —— 直接跳过，别为它冒 OOM 风险。
        try:
            if Path(path).stat().st_size > _UNLOADED_PORTS_MAX_BYTES:
                continue
        except OSError:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if deadline is not None and time.time() > deadline:
            break
        # 网表里 top 未必是第一个模块（case6 的 led_drv 在文件末尾，前面是
        # cfg_unit/stat_unit/pwm_unit），必须遍历到名称为 top 的那个，不能只取首个。
        m = None
        for cand in hdr_re.finditer(text):
            if cand.group(1) == top:
                m = cand
                break
        if m is None:
            continue
        hdr = m.group(2)
        # 端口名列表（去掉 #(...) 参数表）
        body = hdr.split(")", 1)[0] if "(" in hdr else hdr
        for tok in re.split(r"[,\s]+", body.replace("(", " ")):
            tok = tok.strip()
            if re.fullmatch(r"[A-Za-z_]\w*", tok or ""):
                decl_names.add(tok)
        # 方向声明（ANSI / 非 ANSI 都有）
        for dm in re.finditer(
                r"^\s*(input|output|inout)\s+(?:wire\s+|reg\s+)?"
                r"(?:\[[^\]]*\]\s*)?([^;]+);", text, re.M):
            for tok in re.split(r"[,\s]+", dm.group(2)):
                tok = tok.strip()
                if re.fullmatch(r"[A-Za-z_]\w*", tok or ""):
                    decl_names.add(tok)
        # 使用计数：**单遍**统计所有标识符出现次数。
        #
        # 旧写法是「对每个端口名各跑一次全文正则」= O(端口数 × 字节数)：
        # 实测 4.1MB 网表 80 个端口要 3.3s，折算 232MB 约 186s —— 而单遍只要 6s。
        # 大网表上这一项会白吃掉几分钟预算（平台 2 小时超时事故里也占一份）。
        counter = Counter(m.group(0) for m in re.finditer(r"[A-Za-z_]\w*", text))
        for name in decl_names:
            usage[name] = counter.get(name, 0)
        break

    out = []
    for name in sorted(decl_names):
        # 端口声明本身一般出现 2 次（端口表 1 次 + input/output 声明 1 次）；
        # 有负载时会在实例连接里再出现 -> 计数更高。
        if usage.get(name, 0) <= 2:
            out.append(name)
    return out[:limit]


def _trace_shift_chains(facts, module_insts, lib_meta, top):
    """
    每个 module 内追踪移位链：从带 SCD/SCE 的扫描 FF 出发，沿 Q→D 逐级前进。

    网表实测形态（case6 des_perf.v）：
        sky130_fd_sc_hd__sdfxtp_1 \\key_c_r_reg[0][0] ( .D(n1), .CLK(n5),
                                     .SCD(x), .SCE(y), .Q(key_c_r[0][0]) );
        sky130_fd_sc_hd__dfxtp_1  \\key_c_r_reg[1][0] ( .D(key_c_r[0][0]), ... );

    输出的 si / se / so 即 `set_scan_segment -access` 的三元组：
        si = 首级 SCD 引脚路径（scan data in）
        se = 首级 SCE 引脚路径（scan enable）
        so = 尾级 Q 引脚路径（scan data out）

    追踪是**跨模块**的：case6 的 u2/key_r_reg[i] -> u2/uk/K_r0_reg[i] -> … 这类链
    起点在父模块、主体在子模块，只看单个 module 会漏掉 168/280 条。见 _next_ff。
    """
    layer_prefixes = _layer_paths(module_insts, top)
    idx = _build_module_index(module_insts, lib_meta)
    chains = []
    truncated = False

    for module in module_insts:
        if truncated:
            break
        m = idx.get(module)
        if not m:
            continue
        base = (layer_prefixes.get(module) or [""])[0]
        for st in [r for r in m["recs"].values() if r["scd"] or r["sce"]]:
            if truncated:
                break
            seq = [(module, st, base)]
            used = {(module, st["inst"])}
            cur_mod, cur_rec, cur_pref = seq[0]
            for _ in range(8192):
                qnet = cur_rec["conns"].get(cur_rec["q"]) if cur_rec["q"] else None
                if not qnet:
                    break
                found = _next_ff(idx, lib_meta, cur_mod, qnet)
                if not found:
                    break
                nxt_mod, nxt_rec, delta = found
                key = (nxt_mod, nxt_rec["inst"])
                if key in used:
                    break
                new_pref = cur_pref + ("/" + delta if delta else "")
                seq.append((nxt_mod, nxt_rec, new_pref))
                used.add(key)
                cur_mod, cur_rec, cur_pref = nxt_mod, nxt_rec, new_pref
            if len(seq) < 2:
                continue
            _m0, first, pref0 = seq[0]
            _mN, last, prefN = seq[-1]
            si_pin = first["scd"] or first["d"] or "D"
            se_pin = first["sce"] or ""
            chains.append({
                "module": module,
                "prefix": pref0,
                "length": len(seq),
                "first": first["inst"],
                "last": last["inst"],
                "si": _join_path(pref0, first["inst"], si_pin),
                "se": _join_path(pref0, first["inst"], se_pin) if se_pin else "",
                "so": _join_path(prefN, last["inst"], last["q"] or "Q"),
            })
            if len(chains) >= MAX_CHAINS:
                truncated = True
                break

    facts["shift_chains"] = chains
    facts["chains_truncated"] = truncated


def _build_module_index(module_insts, lib_meta):
    """
    为跨模块链追踪建立按 module 的索引。

    每张表都以「本模块内的网络名」为键：
      recs     : inst -> 时序单元记录（含 conns / clk / d / q / scd / sce）
      net_snk  : net -> 消费它的时序单元列表（这些单元的 D 接在该 net 上）
      net_cell : net -> 驱动它的**组合**单元 (cell, conns)，用于穿透缓冲/逻辑
      sub_in   : net -> [(sub_inst, sub_cell, pin)]，父 net 连到子实例的引脚
    """
    idx = {}
    for name, pm in module_insts.items():
        recs, net_snk, net_cell, sub_in = {}, {}, {}, {}
        for cell, inst, conns in pm["seq_insts"]:
            clk, d, q, scd, sce = _seq_pins(cell, lib_meta)
            rec = {"cell": cell, "inst": inst, "conns": conns,
                   "clk": clk, "d": d, "q": q, "scd": scd, "sce": sce}
            recs[inst] = rec
            if d and conns.get(d):
                net_snk.setdefault(conns[d], []).append(rec)
        for cell, inst, conns in pm["insts"]:
            meta = lib_meta.get(cell)
            if meta is None:
                for pin, net in conns.items():
                    if net:
                        sub_in.setdefault(net, []).append((inst, cell, pin))
                continue
            if meta.get("seq"):
                continue
            outs = meta.get("out_pins") or set()
            for pin, net in conns.items():
                if pin in outs and net:
                    net_cell[net] = (cell, conns)
        idx[name] = {"ports": pm["ports"], "recs": recs, "net_snk": net_snk,
                     "net_cell": net_cell, "sub_in": sub_in}
    return idx


_BIT_RE = re.compile(r"^(.*?)(\[[^\]]+\])$")


def _next_ff(idx, lib_meta, module, net, depth=0):
    """
    从 (module, net) 找下一个时序单元，返回 (module, rec, prefix_delta)。

    三条通路，按优先级：
      1. net 直接接在某个时序单元的 D 上（最常见）；
      2. net 经组合单元（缓冲/反相器/逻辑门）后到达下一个时序单元；
      3. net 接在子实例的**输入**端口上 —— 进入子模块继续找，prefix_delta 累加
         该实例名（这就是 u2/key_r_reg[i] -> u2/uk/K_r0_reg[i] 的走法）。

    ── 位选归并（case6 实测必需）
    des_perf.v 里父模块的 Q 是**位选网络** `key_r[55]`，而子实例端口接的是**整条
    向量** `key_sel_1 uk ( .K(key_r) ... )`。二者的对应关系是「同一个向量的第 55 位」，
    只做字符串相等匹配会断链（168/280 条链全丢）。因此在子实例这一路补一次回退：
        key_r[55]  --(向量基名 key_r 命中 uk.K)-->  子模块内 K[55]  -> K_r0_reg[55].D
    位选后缀随端口名一起传下去，不做位宽推测 —— 只在子模块里精确查该名字。
    """
    if depth > 16 or not net:
        return None
    m = idx.get(module)
    if not m:
        return None

    for rec in m["net_snk"].get(net, ()):
        return (module, rec, "")

    drv = m["net_cell"].get(net)
    if drv:
        cell, conns = drv
        ins = (lib_meta.get(cell) or {}).get("in_pins") or set()
        for pin, up in conns.items():
            if pin in ins and up:
                r = _next_ff(idx, lib_meta, module, up, depth + 1)
                if r:
                    return r

    cands = [(net, "")]
    bm = _BIT_RE.match(net)
    if bm:
        cands.append((bm.group(1), bm.group(2)))
    for key, bit in cands:
        for sub_inst, sub_cell, pin in m["sub_in"].get(key, ()):
            sub = idx.get(sub_cell)
            if not sub or sub["ports"].get(pin) != "input":
                continue
            r = None
            if bit:
                r = _next_ff(idx, lib_meta, sub_cell, pin + bit, depth + 1)
            if not r:
                r = _next_ff(idx, lib_meta, sub_cell, pin, depth + 1)
            if r:
                m2, rec2, d2 = r
                return (m2, rec2, _norm_inst(sub_inst) + ("/" + d2 if d2 else ""))
    return None


def _norm_inst(name):
    """去掉 Verilog escaped identifier 的前导反斜杠（golden 的路径写法不带它）。"""
    return (name or "").lstrip("\\")


def _join_path(prefix, inst, pin):
    return "/".join([p for p in (prefix, _norm_inst(inst), pin) if p])


def _layer_paths(module_insts, top):
    """
    计算每个 module 在层次树中的实例路径前缀（des_1 -> ["u2"]）。

    从顶层 DFS，逐层累加「父实例名」。同一 module 被多处实例化会得到多个前缀，
    每个 module 最多保留 8 个（防止层次爆炸），仍不足以唯一确定时以第一个为准。
    """
    children = {m: list(pm["submods"]) for m, pm in module_insts.items()}
    out = {}
    stack = [(top, "")]
    guard = 0
    while stack and guard < 200000:
        guard += 1
        module, prefix = stack.pop()
        bucket = out.setdefault(module, [])
        if prefix in bucket or len(bucket) >= 8:
            continue
        bucket.append(prefix)
        for cell, inst in children.get(module, ()):
            if cell in module_insts:
                stack.append((cell, _join_path(prefix, inst, "")))
    return out


# ===========================================================================
# 第 4 部分：事实摘要 / 确定性命令生成
# ===========================================================================

_PH_NAMES = ("i", "j", "k")


def _path_pattern(paths):
    """
    把一组**结构同构**的路径压成一个带占位符的模式 + 各占位符取值范围。

    例：['u0/key_r_reg[55]/SCD', 'u0/key_r_reg[54]/SCD']
        -> ('u0/key_r_reg[$i]/SCD', {'$i': '0..54'})

    逐位判断：某一下标位在这组里取值恒定就保留原值，变化则参数化为 $i/$j/$k。
    这样 `key_b_r_reg[0][55]` 这种「第一维恒 0、第二维变化」的路径不会被
    错误地压成两个独立下标。

    Returns:
        (pattern: str, ranges: dict[str, str])
    """
    paths = [p or "" for p in paths]
    if not paths:
        return "", {}
    splits = [re.split(r"\[[^\]]*\]", p) for p in paths]
    idxs = [re.findall(r"\[([^\]]*)\]", p) for p in paths]
    n_idx = len(idxs[0])
    # 结构不一致（下标个数不同）时退化为直接给第一条，避免给出错误模式。
    if any(len(s) != len(splits[0]) or len(x) != n_idx for s, x in zip(splits, idxs)):
        return paths[0], {}

    out = splits[0][0]
    ranges = {}
    ph_at = {}
    next_ph = 0
    for j in range(n_idx):
        col = [x[j] for x in idxs]
        if len(set(col)) == 1:
            out += "[" + col[0] + "]"
        else:
            if j not in ph_at:
                name = _PH_NAMES[next_ph] if next_ph < len(_PH_NAMES) else "n%d" % next_ph
                ph_at[j] = "$" + name
                next_ph += 1
            ph = ph_at[j]
            out += "[" + ph + "]"
            ranges[ph] = _compact_range(col)
        if j + 1 < len(splits[0]):
            out += splits[0][j + 1]
    return out, ranges


def _compact_range(vals):
    """把一组下标值压成 `0..55` 或 `a,b,c` 形式。"""
    try:
        ints = sorted({int(v) for v in vals})
    except (TypeError, ValueError):
        uniq = sorted(set(vals))
        return ",".join(uniq[:8]) + ("…" if len(uniq) > 8 else "")
    if ints == list(range(ints[0], ints[0] + len(ints))):
        return "%d..%d" % (ints[0], ints[-1]) if len(ints) > 1 else str(ints[0])
    return ",".join(str(v) for v in ints[:8]) + ("…" if len(ints) > 8 else "")


def _group_chains(chains):
    """
    把同构移位链按 (prefix, 链长, se 端口) 聚成组，输出**可展开**的描述行。

    为什么必须聚合而不是只给前几条样例：case6 有 280 条链（5 组 × 56 条），
    只给 12 条样例、再写一句「其余结构同构」而 prompt 里并不存在那些命令，
    等于给 LLM 一个空指向 —— 它只能编，于是写出 1 组就被覆盖率判据拦下。
    这里改为逐组给出「条数 + si/so 模式 + 下标范围」，LLM 据此即可完整展开。
    """
    # 分组键只用 (prefix, 链长)：se 随链变化（u0/key_r_reg[55]/SCE 等），
    # 若把 se 计入键则每条链自成一组，聚合就失去意义。se 与 si/so 一样做模式化。
    buckets = {}
    for ch in chains:
        key = (ch.get("prefix") or "", ch.get("length"))
        buckets.setdefault(key, []).append(ch)

    out = []
    ordered = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0][1] or 0))
    for i, (key, chs) in enumerate(ordered, 1):
        prefix, length = key
        si_pat, si_rng = _path_pattern([c.get("si") or "" for c in chs])
        so_pat, so_rng = _path_pattern([c.get("so") or "" for c in chs])
        rng = dict(si_rng)
        rng.update({k: v for k, v in so_rng.items() if k not in rng})
        line = "[组%d] %d 条  len=%s  si=%s  so=%s" % (i, len(chs), length, si_pat, so_pat)
        ses = [c.get("se") or "" for c in chs]
        if any(ses):
            se_pat, se_rng = _path_pattern(ses)
            line += "  se=%s" % se_pat
            for k, v in se_rng.items():
                rng.setdefault(k, v)
        if prefix:
            line += "  prefix=%s" % prefix
        if rng:
            line += "  (" + ", ".join("%s=%s" % (k, v) for k, v in sorted(rng.items())) + ")"
        out.append(line)
    return out


def summary_for_prompt(facts, limit_chars=6500):
    """
    把事实压缩成可放进 prompt 的文本。

    只给**事实**（有多个实例、哪个端口驱动多少 FF、链长分布…），不给结论 ——
    LLM 需要据此自己决定写哪些命令，而不是照抄一段现成的 dofile。
    """
    if not facts or not facts.get("ok"):
        return "（网表事实抽取未完成，请严格依据任务说明书与命令白名单生成）"

    L = ["### 网表事实（由确定性解析器从网表 + Liberty 提取，可直接引用）",
         f"- 顶层设计: {facts.get('top') or '（未识别）'}",
         f"- 模块数: {facts.get('modules')}；时序单元总数: {facts.get('ff_count')}"
         f"（其中已存在的扫描 FF {facts.get('sff_count')}）"]

    if facts.get("clock_ports"):
        L.append("\n- 顶层时钟端口（按驱动的时序单元数排序，穿透缓冲后统计）:")
        for p, n in sorted(facts["clock_ports"].items(), key=lambda kv: -kv[1])[:10]:
            L.append(f"    {p}: {n} 个时序单元")

    if facts.get("se_ports"):
        L.append("\n- 已在扫描 FF 的 SCE 引脚上使用的顶层端口（scan_enable 候选）:")
        for p, n in sorted(facts["se_ports"].items(), key=lambda kv: -kv[1])[:8]:
            L.append(f"    {p}: 被 {n} 个扫描 FF 引用")

    # 无负载端口：网表缺陷修复（ECO MUX）时测试控制端的唯一合法来源。
    # case6 的 scan_mode 在原始网表里无负载，MUX 的 S 端必须接它；若漏掉这行
    # 端口声明，connect_pin -from scan_mode 会报 SCAN-1315 Port does not exist。
    if facts.get("unloaded_ports"):
        L.append("\n- 顶层已声明但网表内**无任何负载**的端口（常为测试模式/预留端口；"
                 "若任务说明书要求用某端口做修复控制，其 S/选择端应接这些端口，"
                 "且必须保留端口声明）:")
        L.append("    " + ", ".join(facts["unloaded_ports"][:20]))

    if facts.get("dff_types"):
        L.append("\n- 网表中实际存在的普通 DFF 类型（需要替换为对应扫描 FF）:")
        for c, n in sorted(facts["dff_types"].items(), key=lambda kv: -kv[1])[:12]:
            L.append(f"    {c} × {n}")
    if facts.get("sff_types"):
        L.append("\n- 网表中已存在的扫描 FF 类型:")
        for c, n in sorted(facts["sff_types"].items(), key=lambda kv: -kv[1])[:12]:
            L.append(f"    {c} × {n}")

    if facts.get("extra_modules"):
        L.append("\n- 输入网表额外提供的模块（非库单元，可能需要注册或特殊处理）:")
        for m in facts["extra_modules"][:6]:
            ports = ", ".join(f"{n}({d})" for n, d in list(m["ports"].items())[:12])
            L.append(f"    module `{m['module']}` 端口: {ports}")
            sem = ", ".join(f"{k}={v}" for k, v in (m.get("semantics") or {}).items() if v)
            if sem:
                L.append(f"      端口语义推断: {sem}")

    icg = facts.get("icg") or {}
    if icg.get("cells"):
        L.append("\n- 时钟门控单元（ICG）类型:")
        for c, n in sorted(icg["cells"].items(), key=lambda kv: -kv[1])[:6]:
            L.append(f"    {c} × {n}")
        nodes = icg.get("top_clk_nodes") or []
        if nodes:
            L.append(f"    顶层可见 ICG 节点 {len(nodes)} 个，样例:")
            for nd in nodes[:5]:
                L.append(f"      {nd['inst']}  {nd['clk_pin']}={nd.get('clk_net') or '(悬空)'}")

    chains = facts.get("shift_chains") or []
    if chains:
        L.append(f"\n- 移位链（沿 Q→D 追踪，共 {len(chains)} 条"
                 f"{'，已按上限截断' if facts.get('chains_truncated') else ''}）"
                 f"，按结构聚合如下（$i/$j 为链序号占位符）:")
        for line in _group_chains(chains):
            L.append("    " + line)
        L.append("    ▶ 上表每个「组」代表若干条结构同构的链。若 task_spec 要求把这些"
                 "移位寄存器全部配置为 scan segment，必须覆盖全部 "
                 f"{len(chains)} 条链：用 Tcl for 循环按 $i 展开（把上表模式里的 $i "
                 "替换为循环变量），**不得逐条罗列**（会超出单次输出长度上限、脚本被"
                 "截断），**也不得只写少数样例**。")

    if facts.get("notes"):
        L.append("\n- 抽取说明: " + "；".join(facts["notes"]))

    text = "\n".join(L)
    if len(text) > limit_chars:
        text = text[:limit_chars] + "\n…（事实摘要已截断）"
    return text


def segment_commands(facts, min_length=2, name_prefix="seg", max_chains=None):
    """
    由移位链事实确定性生成 `set_scan_segment` 命令行。

    task_spec 要求「所有长移位寄存器均配置为 scan segment，不得拆分」时，正确做法
    就是把每条链（长度 >= min_length）配成一个 segment，si/se/so 取链首/链尾的真实
    端口路径 —— **只需要网表事实，不需要 golden**。

    写法对齐手册 2.2.9 与实测 golden：
        set_scan_segment <name> -lockup_exists false \\
            -access [list scan_enable <se> scan_data_in <si> scan_data_out <so>]

    Returns:
        (lines: list[str], covered: int)
    """
    chains = [c for c in (facts.get("shift_chains") or []) if c["length"] >= min_length]
    if max_chains:
        chains = chains[:max_chains]
    lines = []
    for i, ch in enumerate(chains):
        acc = []
        if ch.get("se"):
            acc += ["scan_enable", '"%s"' % ch["se"]]
        acc += ["scan_data_in", '"%s"' % ch["si"]]
        acc += ["scan_data_out", '"%s"' % ch["so"]]
        lines.append('set_scan_segment %s%d -lockup_exists false \\\n'
                     '    -access [list %s]' % (name_prefix, i, " ".join(acc)))
    return lines, len(chains)


def wrapper_register_commands(facts, module=None):
    """
    由 extra_modules 事实确定性生成 `add_dedicated_wrapper_cell_type`。

    手册 2.2.3 语法（只有两个选项，7 组三元组的顺序固定）：
        add_dedicated_wrapper_cell_type -design_name <module> \\
            -interface {shift_clk <p> h capture_en <p> h shift_en <p> h \\
                        cti <p> h cto <p> h cfi <p> h cfo <p> h}
    """
    order = ("shift_clk", "capture_en", "shift_en", "cti", "cto", "cfi", "cfo")
    lines = []
    for m in (facts.get("extra_modules") or []):
        if module and m["module"] != module:
            continue
        sem = m.get("semantics") or {}
        if not all(sem.get(k) for k in order):
            continue
        triples = " ".join("%s %s h" % (k, sem[k]) for k in order)
        lines.append("add_dedicated_wrapper_cell_type -design_name %s \\\n"
                     "    -interface {%s}" % (m["module"], triples))
    return lines

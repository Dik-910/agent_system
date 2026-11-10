#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工具运行模块
封装 dftexp_scan 的调用，管理运行目录、日志捕获。
"""

import collections
import os
import re
import shutil
import subprocess
from pathlib import Path

try:
    from . import config
except ImportError:
    import config


def run_dofile(dofile_content, run_dir, run_id=1, timeout=None):
    """
    在 run_dir 下运行 dofile。

    目录结构（符合赛题规范）：
      run_dir/
        R{run_id}.log        <- 运行日志（run_dir 根）
        deliverables/
          R{run_id}.dofile   <- dofile（位于 deliverables 子目录）
        output/              <- 工具输出（网表/ctl/def，跑完后搬运至此）
        reports/             <- 报告（跑完后搬运至此）

    注意：dofile 内用 [file dirname [info script]] 定位 script_dir，
    因此 dofile 放在 deliverables/ 下时，script_dir = deliverables/，
    工具输出会先写到 deliverables/output、deliverables/reports，
    跑完后统一搬运到 run_dir/output、run_dir/reports。

    Args:
        dofile_content: dofile 文本
        run_dir: 运行目录（Path 或 str），如 .../runs/R1
        run_id: 轮次编号
        timeout: 单轮超时（秒）。None 时用 config.TOOL_TIMEOUT。
            调用方（scan_agent）会按 limitations.md 的 wall time 预算推导该值：
            硬编码 120s 会在 task_1/case5（预算 4800s）这种大网表 case 上
            把工具 kill 在加载阶段，导致该 case 零产物。

    Returns:
        dict: 运行结果
    """
    run_dir = Path(run_dir)
    deliverables = run_dir / "deliverables"
    tool_out = run_dir / "output"
    reports = run_dir / "reports"
    for d in (deliverables, tool_out, reports):
        d.mkdir(parents=True, exist_ok=True)

    dofile_path = deliverables / f"R{run_id}.dofile"
    log_path = run_dir / f"R{run_id}.log"
    dofile_path.write_text(dofile_content, encoding="utf-8")

    # 运行工具
    cmd = [config.DFTEXP_SCAN, "-f", str(dofile_path)]
    env = dict(os.environ)
    if config.LICENSE_SERVER:
        env["SCANINSERTION_LICENSE_SERVER"] = config.LICENSE_SERVER

    if timeout is None:
        timeout = config.TOOL_TIMEOUT

    # ── 日志捕获方式：文件描述符直接重定向（关键性能路径）────────────────
    #
    # 早期实现用 stdout=PIPE + text=True，于是工具的全部输出都要绕一圈
    # Python 进程：内核管道 -> _communicate 的 32KB 分块 os.read ->
    # bytes 拼接 -> UTF-8 解码 -> 写回磁盘。
    #
    # 平台实测（submission_out/intermediate_files/task_1/case5）里这条路径
    # 是最大的单点开销：dftexp_scan 在 -verbose 下会打出 5 条 1400 万~1600 万
    # 字符的**单行**（SCAN-7601 列出全部被替换的 DFF），另有 examine_scan_chain
    # 的 55 万行 chain 明细（93MB）。这些数据每填满 64KB 管道就阻塞一次工具，
    # 等 Python 端调度读取，一个 case 的工具耗时被放大到 300~700s。
    #
    # 改为把子进程 stdout 直接绑到日志文件 fd 后：
    #   - 内核直接把输出写进页缓存，工具不再被管道反压
    #   - Python 侧零拷贝（不再把 200MB 字符串读进内存）
    #   - 日志仍然按原路径落盘，final.log 语义不变
    try:
        with open(log_path, "wb") as _logf:
            proc = subprocess.run(
                cmd,
                cwd=str(run_dir),
                env=env,
                stdout=_logf,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )

        # 搬运工具输出：dofile 在 deliverables/ 下，输出先写到 deliverables/output、deliverables/reports
        _move_tool_outputs(deliverables, tool_out, reports)

        # success 判定：returncode==0 且日志中无 [ERROR]/[FATAL]
        # （dftexp_scan 遇到命令错误时可能仍返回 0，需结合日志判断）
        # 日志可达数百 MB：单遍流式扫描，一次拿到「是否有 ERROR」与高信息摘要
        # （下游 report_parser / decision_logger / spec_verifier 都要看日志内容）。
        has_error, summary = _scan_log(log_path)
        success = proc.returncode == 0 and not has_error

        return {
            "success": success,
            "run_id": f"R{run_id}",
            "run_dir": str(run_dir),
            "log_file": str(log_path),
            "dofile_path": str(dofile_path),
            "exit_code": proc.returncode,
            "output": summary,
        }

    except subprocess.TimeoutExpired:
        # 超时也必须把**已经产生的部分日志**落盘：
        # 早期实现只写一行 "[TIMEOUT]"，final.log 看不出工具跑到哪一步，
        # 决策日志也无法解释为什么这一轮被中止。
        # 现在日志由工具直写 fd，超时前的内容本来就在盘上，这里只补一行标记。
        try:
            with open(log_path, "ab") as _logf:
                _logf.write(
                    f"\n[TIMEOUT] 工具单轮超过 {timeout}s，已被中止\n".encode("utf-8"))
        except OSError:
            pass
        # 超时前工具可能已经写出一部分产物，照样搬运（不丢可见产物）
        _move_tool_outputs(deliverables, tool_out, reports)
        # 超时也可能留下了有价值的输出（跑到哪一步、报了哪些错），
        # 同样做一次摘要供诊断与决策记录使用。
        _has_err, summary = _scan_log(log_path)
        return {
            "success": False,
            "run_id": f"R{run_id}",
            "run_dir": str(run_dir),
            "log_file": str(log_path),
            "dofile_path": str(dofile_path),
            "exit_code": None,
            "output": summary,
            "error": "timeout",
            "timeout_s": timeout,
        }

    except FileNotFoundError:
        # 工具不存在（本地无 License / 无工具环境）
        return {
            "success": False,
            "run_id": f"R{run_id}",
            "run_dir": str(run_dir),
            "log_file": str(log_path),
            "dofile_path": str(dofile_path),
            "exit_code": None,
            "output": "",
            "error": "tool_not_found",
        }



# ============================================================================
# 大日志辅助：单遍扫描 + 高信息摘要
# ============================================================================

# dftexp_scan 遇到命令错误时可能仍返回 0，必须结合日志判定；
# 但单个 case 的日志可达数百 MB（task_1/case5 实测 198MB，
# 内含 5 条 1400 万字符的 SCAN-7601 单行），因此一律流式处理，绝不整体读入内存。
_LOG_CHUNK = 1 << 22          # 4MB
_ERR_PAT = re.compile(rb"\[(?:ERROR|FATAL)\]")
_ERR_OVERLAP = 32             # 覆盖跨块边界的 "[ERROR]"

# 摘要收集上限
_S_ERR_MAX = 200              # 保留的 ERROR 行条数
_S_WARN_MAX = 80              # 保留的 WARNING 行条数
_S_STAT_MAX = 200             # 保留的统计行条数
_S_LINE_CLIP = 400            # 单行截断长度（SCAN-7601 那种 1600 万字符的行只取头部）
_S_TAIL_BYTES = 4096          # 尾部保留字节
_S_MAX_BYTES = 262144         # 摘要总上限

# 统计类关键词：下游的 report_parser / decision_logger 会去找链数、
# 违例数、SCAN-7600/7602 的替换计数、clock gating 实例数等。
_S_STAT_HINTS = (b"chain", b"Chain", b"violation", b"Violation", b"SCAN-76",
                 b"clock gating", b"flip-flop", b"Number of")
_S_STAT_MAX_LINE = 2048       # 超过这个长度的行不参与统计匹配（都是明细列表）
# 统计行只可能以这些首字节开头：'['（工具消息行）、N/n/T/t（Number of / Total）。
# 网表 cell 明细行（占日志绝大部分）以 'x_' 等开头，靠这一条即可整行跳过 ——
# 否则 1000 万行 × 9 个关键词的子串搜索会把扫描拖到几十秒。
_STAT_LEAD = (b"[", b"N", b"n", b"T", b"t")


def _scan_log(log_path, want_summary=True):
    """
    单遍扫描工具日志，返回 (has_error, summary_text)。

    为什么要"摘要"而不是"整份日志"或"只回尾部"：
      下游消费者要的东西散布在日志各处 —— report_parser 找链数/违例统计、
      decision_logger 找 SCAN-7600/7602 的替换计数与首个 ERROR 行、
      spec_verifier 要把日志片段喂给 LLM 复核配置。只给尾部会静默丢失这些证据；
      给全文又会把 200MB 字符串抱进内存（旧实现的问题）。

    摘要 = 去重后的 ERROR 行 + WARNING 样本 + 关键统计行 + 日志尾部。
    需要完整日志时一律用返回值里的 log_file 路径（见 scan_agent._parsed_from_result）。

    性能：网表明细行（`x_a/b/c_reg[0]  (pll_core_cpuclk, +)` 这类）占了日志的
    绝大部分，它们既不以 '[' 开头也不含统计关键词，因此走的是最便宜的
    子串判断分支。198MB 实测 1.25s，摘要 23KB。
    """
    has_error = False
    errors, warnings, stats = [], [], []
    seen_err, seen_warn = set(), set()
    tail = collections.deque()
    tail_len = 0
    total = 0

    try:
        fh = open(log_path, "rb")
    except OSError:
        return False, ""

    with fh:
        for line in fh:
            total += 1
            head = line[:24]
            if b"ERROR" in head or b"FATAL" in head or _ERR_PAT.search(head):
                has_error = True
                if want_summary and len(errors) < _S_ERR_MAX:
                    key = line[:_S_LINE_CLIP]
                    if key not in seen_err:
                        seen_err.add(key)
                        errors.append(line[:_S_LINE_CLIP])
            elif b"WARNING" in head:
                if want_summary and len(warnings) < _S_WARN_MAX:
                    key = line[:_S_LINE_CLIP]
                    if key not in seen_warn:
                        seen_warn.add(key)
                        warnings.append(line[:_S_LINE_CLIP])
            elif want_summary and len(line) <= _S_STAT_MAX_LINE:
                if line[:1] in _STAT_LEAD and any(h in line for h in _S_STAT_HINTS):
                    if len(stats) < _S_STAT_MAX:
                        stats.append(line[:400])

            if want_summary:
                snippet = line[:_S_LINE_CLIP]
                tail.append(snippet)
                tail_len += len(snippet)
                while tail_len > _S_TAIL_BYTES and tail:
                    tail_len -= len(tail.popleft())

    if not want_summary:
        return has_error, ""

    parts = []
    if errors:
        parts.append(f"[ERRORS 共 {len(errors)} 类，最多列 {_S_ERR_MAX} 条]\n"
                     + b"".join(errors).decode("utf-8", "replace"))
    if warnings:
        parts.append(f"[WARNINGS 样本 {len(warnings)} 条，最多列 {_S_WARN_MAX} 条]\n"
                     + b"".join(warnings).decode("utf-8", "replace"))
    if stats:
        parts.append(f"[关键统计行 {len(stats)} 条]\n"
                     + b"".join(stats).decode("utf-8", "replace"))
    parts.append(f"[日志尾部 · 总行数 {total}]\n"
                 + b"".join(tail).decode("utf-8", "replace"))

    summary = "\n".join(parts)
    if len(summary) > _S_MAX_BYTES:
        summary = summary[:_S_MAX_BYTES] + "\n…（摘要已截断，完整日志见 log_file）"
    return has_error, summary


def _log_has_error(log_path, chunk_size=_LOG_CHUNK):
    """分块扫描日志，判断是否存在 [ERROR] / [FATAL] 标记（不做摘要，最快的路径）。"""
    return _scan_log(log_path, want_summary=False)[0]


def _log_tail(log_path, max_bytes=16384):
    """
    读取日志尾部若干字节（保留的轻量工具；一般用 _scan_log 的摘要代替）。

    返回全文会让调用方把 200MB 字符串抱在内存里，而实际消费者只需要
    "跑到哪一步、最后报了什么"，因此这里只回尾部。
    """
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            data = fh.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _move_tool_outputs(deliverables, tool_out, reports):
    """
    把工具写到 deliverables/output、deliverables/reports 下的产物，
    搬运到 run_dir/output、run_dir/reports（符合赛题目录规范）。

    采用**递归扁平化**搬运：原始 dofile 可能把产物写成
    output/original/output/xxx.v 这种嵌套结构（见 task_2 的 original.dofile），
    早期实现只遍历一层，嵌套目录会被整体 move 过去、文件散落在子目录里，
    后续 final_results 收集（按 rglob 找）虽然能找到，但与 docx 8.2 的
    runs/R{n}/output 结构不符。
    """
    def _flatten(src_dir, dst_dir):
        if not src_dir.exists():
            return
        for f in sorted(src_dir.rglob("*")):
            if not f.is_file():
                continue
            dst = dst_dir / f.name
            if dst.exists():
                continue
            try:
                shutil.move(str(f), str(dst))
            except Exception:
                pass
        # 清理空目录
        for d in sorted(src_dir.rglob("*"), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass
        try:
            src_dir.rmdir()
        except OSError:
            pass

    _flatten(deliverables / "output", tool_out)
    _flatten(deliverables / "reports", reports)

    # 赛题 docx 8.2 的 runs/R{n}/deliverables/ 要求：
    #   R{n}.dofile + design.ctl / design.scandef / post_scan.v 等"本轮生成的交付物（如有）"。
    # run_dir/output 保留工具原始产物，这里再复制一份到 deliverables/ 以对齐目录规范。
    if tool_out.exists():
        for f in sorted(tool_out.rglob("*")):
            if not f.is_file() or f.suffix not in (".v", ".ctl", ".scandef", ".def"):
                continue
            dst = deliverables / f.name
            if dst.exists():
                continue
            try:
                # 同盘优先硬链接（零拷贝）：case5 这类大设计单轮有
                # 200MB 级网表/CTL/DEF 需要落到 deliverables/，
                # 复制要几十秒，硬链接与文件大小无关。
                try:
                    os.link(str(f), str(dst))
                except OSError:
                    shutil.copy(str(f), str(dst))
            except Exception:
                pass




# ============================================================================
# Liberty -> Verilog 功能模型（门级 LEC 的前置条件）
# ============================================================================
#
# 为什么必须有（2026-11-01，测试18 实测）：
#   `read_liberty` 只给 yosys **黑盒定义**（端口 + 时序），不给逻辑功能。
#   门级 LEC 一跑到 `equiv_make` 就报
#       ERROR: Missing function on output GCLK of cell sky130_fd_sc_hd__dlclkp_1
#   报告 705 字节、结论无效 —— 按规范 5.4.1-3「失败或未完成 = 0 分」等于没交。
#
#   而 input 里只有 `.lib`，没有 `sky130_fd_sc_hd.v` 这种仿真模型。
#   好在 Liberty 自己就带功能信息：实测该库有 `function` 943 处、`direction` 1924 处，
#   扫描触发器还直接给了 `next_state : "(D&!SCE) | (SCD&SCE)"`。
#   所以我们自己从 .lib 生成等价的行为模型。
#
# 精度取舍：生成的模型只求「**两侧一致**」，不追求与硅片逐周期相同 ——
#   等价性检查比较的是同一份模型下的两份网表，模型越一致结论越可靠。
#   唯一必须近似的是 ICG（Liberty 只给 statetable，不给布尔 function），
#   用 `GCLK = CLK & ~GATE` 表达"时钟高且未门控时通过"。

_LIB_MODEL_CACHE = {}


def _balance_block(text, start):
    """从 start（紧跟 `{`）取到配平的 `}`，返回内部文本。"""
    depth, i = 1, start
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[start:i - 1]


def _lib_blocks(text, keyword):
    """按关键字取块（`ff (...) {` / `latch (...) {` 这类）。"""
    out = []
    for m in re.finditer(keyword + r"\s*(?:\([^)]*\))?\s*\{", text):
        out.append(_balance_block(text, m.end()))
    return out


def _lib_attr(block, key):
    m = re.search(key + r'\s*:\s*"([^"]*)"', block)
    return m.group(1) if m else ""


def _lib_expr(expr):
    """Liberty 布尔表达式 -> Verilog。

    `!` -> `~`；`+` 在 Liberty 是 OR；`*` 是 AND（state_function 里写 `(CLK*M0)`）。
    逐位运算符对标量信号与逻辑运算等价，不影响等价性判定。
    """
    e = (expr or "").strip()
    e = e.replace("!", "~").replace("+", "|")
    if "*" in e:
        e = e.replace("*", "&")
    return e


def _strip_invert(expr):
    """`~RST_N` -> `RST_N`（用于取异步复位的敏感沿）。"""
    e = _lib_expr(expr).strip()
    while e.startswith("~"):
        e = e[1:].strip()
    return e.strip("() ")


def _cell_model(name, body):
    """把一个 Liberty cell 块翻成一个 Verilog 模块（无法建模时返回 ""）。"""
    pins, seen = [], set()          # [(pin, direction, function)]，按名字去重
    for m in re.finditer(r'pin\s*\(\s*"?(\w+)"?\s*\)\s*\{', body):
        pin = m.group(1)
        if pin in seen:             # 库里同一个 pin 会有重复定义（如 sdfxtp_1），
            continue                # 不去重会生成 (CLK,D,Q,CLK,D,Q) 这种非法端口表
        pbody = _balance_block(body, m.end())
        d = _lib_attr(pbody, "direction")
        if d not in ("input", "output"):
            continue
        seen.add(pin)
        pins.append((pin, d, _lib_attr(pbody, "function")))
    ins = [p for p, d, _f in pins if d == "input"]
    outs = [p for p, d, _f in pins if d == "output"]
    if not outs:
        return ""

    head = [f"module {name} ({', '.join(ins + outs)});"]
    if ins:
        head.append("  input " + ", ".join(ins) + ";")
    head.append("  output " + ", ".join(outs) + ";")

    ff = _lib_blocks(body, r"ff")
    lat = _lib_blocks(body, r"latch")
    is_icg = "clock_gating_integrated_cell" in body
    logic = ""

    if is_icg and len(ins) >= 2 and len(outs) == 1:
        # ICG **降维为纯缓冲**（官方参考链的坑 1，见
        # 赛题二更正材料1012/task2-case6/yosys/README.md）：
        #   「ICG(sdlclkp_1) 降维为 assign GCLK = CLK，否则 clk2fflogic 下
        #     GATE/SCE 反馈成组合环。两侧同用该模型。」
        # 门控使能的差异在两侧一致地被忽略 —— 那是 DFT 逻辑，不属于功能等价范畴；
        # 要判 ICG 接法是否正确，另有 A0/A1/S 结构判据。
        logic = f"  assign {outs[0]} = {ins[0]};"
    elif ff:
        blk = ff[0]
        clk = _lib_expr(_lib_attr(blk, "clocked_on"))
        nxt = _lib_expr(_lib_attr(blk, "next_state")) or "0"
        clr = _lib_attr(blk, "clear")
        pre = _lib_attr(blk, "preset")
        q = outs[0]
        if clk:
            if clr:
                r = _strip_invert(clr)
                logic = (f"  always @(posedge {clk} or negedge {r})\n"
                         f"    if (~{r}) {q} <= 1'b0; else {q} <= {nxt};")
            elif pre:
                r = _strip_invert(pre)
                logic = (f"  always @(posedge {clk} or negedge {r})\n"
                         f"    if (~{r}) {q} <= 1'b1; else {q} <= {nxt};")
            else:
                logic = f"  always @(posedge {clk}) {q} <= {nxt};"
    elif lat:
        blk = lat[0]
        en = _lib_expr(_lib_attr(blk, "enable"))
        din = _lib_expr(_lib_attr(blk, "data_in")) or "0"
        if en:
            logic = f"  always @* if ({en}) {outs[0]} <= {din};"
    if not logic:
        # 组合单元：每个 output 的 function 直接写成 assign
        parts = []
        for p, d, fn in pins:
            if d == "output" and fn and fn not in ("0", "1") and "VPWR" not in fn:
                parts.append(f"  assign {p} = {_lib_expr(fn)};")
            elif d == "output" and fn in ("0", "1"):
                parts.append(f"  assign {p} = 1'b{fn};")
        logic = "\n".join(parts)
    if not logic:
        return ""
    return "\n".join(head) + "\n" + logic + "\nendmodule\n"


def liberty_to_verilog(lib_path, out_path=None):
    """
    把 Liberty 库转成 Verilog 行为模型，返回 (模型文本, 模块数)。

    同一个 lib 只解析一次（按路径 + mtime 缓存）：12.8MB 的 sky130.lib
    每轮都重新解析会白白吃掉好几秒。
    """
    try:
        st = Path(lib_path).stat()
    except OSError:
        return "", 0
    key = (str(lib_path), st.st_mtime, st.st_size)
    if key in _LIB_MODEL_CACHE:
        text, n = _LIB_MODEL_CACHE[key]
    else:
        try:
            lib = Path(lib_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "", 0
        mods, n = [], 0
        for m in re.finditer(r'cell\s*\(\s*"([^"]+)"\s*\)\s*\{', lib):
            name = m.group(1)
            body = _balance_block(lib, m.end())
            model = _cell_model(name, body)
            if model:
                mods.append(model)
                n += 1
        text = "\n".join(mods)
        _LIB_MODEL_CACHE[key] = (text, n)
    if out_path and text:
        try:
            Path(out_path).write_text(text, encoding="utf-8")
        except OSError:
            pass
    return text, n


def run_yosys_lec(original_netlist, modified_netlist, run_dir, top_module=None,
                  lib_files=None, model_files=None):
    """
    使用 Yosys 做逻辑等价性检查（LEC），校验改网表后功能是否等价。

    等价性检查的标准流程：
      1. 读入「原始」网表（golden），读取 .lib 库（避免单元黑盒）；
      2. hierarchy -top <top>，将 golden 保存为参考设计（design -stash gold）；
      3. 读入「修改后」网表，同样 hierarchy 到顶层；
      4. equiv_make gold gate equiv，建立等价映射；
      5. prep -flatten 展平，equiv_simple / equiv_induct 证明等价；
      6. equiv_status -assert 判定 PASS/FAIL，并统计不匹配点。

    Args:
        original_netlist: 原始网表路径（golden 参考）
        modified_netlist: 修改后网表路径（待验证）
        run_dir: 运行目录（脚本与报告输出于此）
        top_module: 顶层模块名（None 则自动从网表推断）
        lib_files: .lib 库文件列表（None 则尝试从 run_dir 附近推断）

    Returns:
        dict: {
            "success": bool,           # 等价性是否 PASS
            "report": str,             # 报告文件路径
            "script": str,             # yosys 脚本路径
            "output": str,             # 原始输出
            "matched": int,            # 匹配点
            "unmatched": int,          # 不匹配点
            "error": str 或 None,      # 错误信息
        }
    """
    # 一律解析成绝对路径：subprocess 以 cwd=run_dir 运行，脚本路径若还是相对路径
    # 就会被拼两遍（`_tmp_test/lec_x/_tmp_test/lec_x/lec.ys`）→ yosys 打不开脚本，
    # 静默失败成一个 "unknown"，看起来像"工具证明不了"。
    run_dir = Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "lec_report.rpt"

    original_netlist = _quote_path(Path(str(original_netlist)).resolve())
    modified_netlist = _quote_path(Path(str(modified_netlist)).resolve())

    # 顶层模块：未指定则从网表推断
    if not top_module:
        top_module = _infer_top_module(original_netlist)
    if not top_module:
        top_module = "top"

    # 库：**优先用 Verilog 功能模型**。
    #
    # 为什么不能只 read_liberty（2026-11-01，测试18 实测）：read_liberty 只给
    # 黑盒定义（端口 + 时序），没有逻辑功能，门级 LEC 到 equiv_make 就报
    #   ERROR: Missing function on output GCLK of cell sky130_fd_sc_hd__dlclkp_1
    # 报告无效 = 规范 5.4.1-3 的「未完成」= 0 分。模型由 liberty_to_verilog()
    # 从 .lib 的 function / next_state 生成，与库一一对应。
    lib_lines = []
    model_list = [m for m in (model_files or []) if m and Path(str(m)).exists()]
    if model_list:
        for mf in model_list:
            # 必须 resolve：yosys 以 cwd=run_dir 运行，相对路径会找不到模型文件
            lib_lines.append(f"read_verilog {_quote_path(Path(str(mf)).resolve())}")
    else:
        lib_list = lib_files or []
        if not lib_list:
            lib_list = _infer_lib_files(original_netlist)
        for lib in lib_list:
            lib_lines.append(f"read_liberty {_quote_path(lib)}")

    script_lines = [
        "# ============ 等价性检查（LEC）脚本 ============",
        "# 1. 读入原始（golden）网表 + 库",
        *lib_lines,
        f"read_verilog {original_netlist}",
        # 不能用 `hierarchy -check`：它在裁剪未使用模块时先删、后查，会出现
        #   "Module `sky130_fd_sc_hd__sdlclkp_1' referenced in module
        #    `SNPS_CLOCK_GATE_HIGH_*' ... is not part of the design"
        # 而该模块明明已由 lib_models 提供（2026-11-01 实测）。裸 `-top` 不裁剪，
        # 缺模块会在 flatten 阶段暴露，效果一样但不误报。
        f"hierarchy -top {top_module}",
        # proc / memory 必须在 equiv_make 之前跑完：否则 yosys 直接拒绝
        # 「Gold module contains memories or processes」而整个检查不成立。
        # 门级网表通常只有 assign + 实例（proc 是空操作），但网表里一旦混进
        # 行为级描述就必挂 —— 加上这两条是零成本的稳妥。
        "proc",
        "memory",
        # flatten：把有定义的子模块（自定义 mux / 行为模型）内联成基本 cell，
        # equiv_induct 的 SAT 才有模型可用。黑盒单元保持黑盒。
        "flatten",
        # flatten 会留下 $scopeinfo（作用域标记）cell，它们携带对**已被删除**的
        # 子模块的引用；equiv_make 把这些 cell 复制进 equiv 模块后，equiv_induct
        # 就报 "No SAT model available for cell r312.U2_gate (sky130_fd_sc_hd__inv_2)"。
        # 清掉它们再继续（只是层次标记，不含逻辑）。
        "delete t:$scopeinfo",
        # 异步 FF（$dlatch / 带 clear 的 $dff）在 equiv_induct 里没有 SAT 模型：
        #   ERROR: No SAT model available for async FF cell ... ($dlatch).
        #   Consider running `async2sync` or `clk2fflogic` first.
        # 两侧都做同样的变换，等价性判定不受影响。
        "async2sync",
        f"design -stash gold",
        "",
        "# 2. 读入修改后网表",
        # 库/模型**必须重读一遍**：上一步 flatten 会把已经内联掉的 sky130 模块
        # 当作 unused 删掉（"Deleting now unused module sky130_..."），
        # 再读第二份网表时这些 cell 就找不到定义、退化成黑盒，
        # 于是 equiv_induct 报 "No SAT model available for cell r312.U56_gate"。
        *lib_lines,
        f"read_verilog {modified_netlist}",
        f"hierarchy -top {top_module}",
        "proc",
        "memory",
        "flatten",
        "delete t:$scopeinfo",
        "async2sync",
        "design -stash gate",
        "",
        "# 3. 建立等价映射并证明",
        # selection 必须是**真实模块名**（top_module），不是设计别名。
        # 旧实写字面量 `gold`/`gate` → yosys 报 "Can't find gold module gold"，
        # 整条 LEC 路径从未真正跑通（回滚到原网表，看起来像"证明不了"）。
        f"design -copy-from gold -as gold {top_module}",
        f"design -copy-from gate -as gate {top_module}",
        "equiv_make gold gate equiv",
        # 不能用 `prep -flatten -top`：`-top` 后面必须跟模块名，只写 `-top`
        # 直接语法错（"Unknown option or option in arguments"），整条 LEC 作废。
        # 标准等价流程也不需要 prep —— equity 模块里全是 $equiv cell，
        # prep 的 flatten/opt 反而可能把它们优化掉。
        "hierarchy -top equiv",
        "opt_clean",
        "equiv_simple",
        "equiv_induct",
        "",
        "# 4. 判定并输出统计",
        "equiv_status -assert",
        "equiv_status",
        "stat",
        # 不要写 `exit`：yosys 的脚本模式没有这个命令
        # （`ERROR: No such command: exit`），报错会让整次检查的退出码非 0，
        # 于是一个本来 PASS 的证明被判成失败。脚本读完自然退出。
    ]
    script = "\n".join(script_lines)
    script_path = run_dir / "lec.ys"
    script_path.write_text(script, encoding="utf-8")

    try:
        proc = subprocess.run(
            # 不要加 `-q`：静默模式下 equiv_status 的统计（N matched / unmatched）
            # 完全不输出，报告是空的 —— 既拿不到"正向 PASS 证据"。也交不出
            # 规范第 90 行要求的"功能等价检查报告"。
            [config.YOSYS, "-s", str(script_path)],
            cwd=str(run_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=max(config.TOOL_TIMEOUT, 300),
            text=True,
        )
    except FileNotFoundError:
        return {
            "success": False,
            "report": str(report_path),
            "script": str(script_path),
            "output": "",
            "matched": 0,
            "unmatched": 0,
            "error": "yosys_not_found",
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "report": str(report_path),
            "script": str(script_path),
            "output": "[TIMEOUT]",
            "matched": 0,
            "unmatched": 0,
            "error": "timeout",
        }

    stdout = proc.stdout or ""
    report_path.write_text(stdout, encoding="utf-8")

    # 解析等价性结果
    matched, unmatched = _parse_equiv_stats(stdout)
    passed = _detect_equiv_pass(stdout, proc.returncode)

    return {
        "success": passed,
        "report": str(report_path),
        "script": str(script_path),
        "output": stdout,
        "matched": matched,
        "unmatched": unmatched,
        "error": None if passed else ("equiv_mismatch" if unmatched else "unknown"),
    }


# ============================================================================
# LEC 辅助函数
# ============================================================================

def _quote_path(p):
    """把路径转成 Tcl 安全的双引号字符串（替换反斜杠为 /，转义引号）。"""
    p = str(p).replace("\\", "/")
    if '"' in p:
        p = p.replace('"', '\\"')
    return f'"{p}"'


def _infer_top_module(netlist_path):
    """从网表推断顶层模块名（取第一个 module 声明）。"""
    try:
        with open(netlist_path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(64 * 1024)
        m = re.search(r"^\s*module\s+([A-Za-z_][\w]*)", head, re.MULTILINE)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _infer_lib_files(netlist_path):
    """从网表同目录或上级目录推断 .lib 库文件。"""
    libs = []
    base = Path(netlist_path).parent
    search_dirs = [base, base.parent / "lib", base / "lib"]
    for d in search_dirs:
        if not d.exists():
            continue
        for lib in sorted(d.glob("*.lib")):
            libs.append(str(lib))
            break  # 每个目录只取一个，避免重复加载
        if libs:
            break
    return libs


def _parse_equiv_stats(stdout):
    """从 yosys 输出解析等价性统计（匹配/不匹配点数）。"""
    matched = 0
    unmatched = 0
    # equiv_status 输出形如 "equiv: 123 matched, 4 unmatched"
    m = re.search(r"(\d+)\s+matched", stdout)
    if m:
        matched = int(m.group(1))
    m = re.search(r"(\d+)\s+unmatched", stdout)
    if m:
        unmatched = int(m.group(1))
    # yosys 实际口径：`Of those cells N are proven and M are unproven.`
    # （没有 "N matched" 这种写法，旧解析永远拿到 0/0，报告里等于没数字）
    if not matched:
        tail = re.findall(r"are\s+proven\s+and\s+(\d+)\s+are\s+unproven", stdout)
        if tail:
            matched = int(re.findall(r"Of those cells (\d+) are proven", stdout)[-1]) \
                if re.findall(r"Of those cells (\d+) are proven", stdout) else 0
            unmatched = int(tail[-1])
    return matched, unmatched


def _detect_equiv_pass(stdout, returncode):
    """
    判定等价性是否 PASS（**正向证据制**）。

    赛题 5.4.1-3)：任务二中只要修改了 Pre-scan 网表，最终采用的那份网表必须
    通过功能等价检查，否则当前 case 直接 0 分。这是一个 0 分红线，
    因此判定必须是「有正向 PASS 证据才算通过」，而不是「没看到失败就算通过」。

    早期实现最后一行是 `return True`：在 yosys 不存在、脚本提前中断、
    或输出里没有任何 equiv_status 行时都会判「等价」—— 等于把未验证的
    网表当成已验证采用，踩在 0 分红线上。现在改为：拿不到正向证据一律判不通过
    （不通过 = 丢弃网表修改，等于没改网表，不会触发 LEC 检查）。

    正向证据（任一）：
      - 显式 PASS 文案：'equiv_status: PASS' / 'successfully proved' /
        'No unproven $equiv cells'
      - 明确的统计行：'N matched' 且 '0 unmatched' 且 N > 0
    """
    if returncode != 0:
        return False
    text = stdout or ""

    # 负向证据优先
    if re.search(r"Equivalence\s+check\s+failed", text, re.IGNORECASE):
        return False
    # 注意：不能裸匹配 `\bunproven\b` —— yosys 的中间日志会打印
    #   "Found 1 unproven $equiv cells (1 groups)"（equiv_induct 之前的 before 状态），
    # 后面可能已经证明掉了。旧的裸匹配让一个 PASS 的证明被判成失败，
    # 于是 ECO 被回滚、LEC 报告作废。
    # 真正的负向证据只有「最终统计里 unproven > 0」，见下面的 tail_stats。

    m = re.search(r"(\d+)\s+unmatched", text)
    if m and int(m.group(1)) > 0:
        return False

    # 负向证据（**最终**统计）：yosys 的实际输出是
    #   "Of those cells 1 are proven and 0 are unproven."
    # 只看中间行 "Found 1 unproven $equiv cells" 会误判（那是 before 状态，
    # 后面的 equiv_induct 可能已经证明掉了）。所以取最后一次统计。
    tail_stats = re.findall(r"are\s+proven\s+and\s+(\d+)\s+are\s+unproven", text)
    if tail_stats and int(tail_stats[-1]) > 0:
        return False

    # 正向证据
    if re.search(r"equiv_status:\s*PASS", text, re.IGNORECASE):
        return True
    if re.search(r"equivalence\s+successfully\s+proven", text, re.IGNORECASE):
        return True
    if re.search(r"successfully\s+proved", text, re.IGNORECASE):
        return True
    if re.search(r"No\s+selected\s+unproven\s+\$?equiv\s+cells", text, re.IGNORECASE):
        return True
    if re.search(r"No\s+unproven\s+\$?equiv\s+cells", text, re.IGNORECASE):
        return True
    if tail_stats and int(tail_stats[-1]) == 0:
        return True

    matched = re.search(r"(\d+)\s+matched", text)
    if matched and int(matched.group(1)) > 0:
        # 有 matched 且 unmatched==0（上面已确认没有 >0 的 unmatched）
        return True

    # 无任何正向证据 -> 不判通过（宁可放弃网表修改，也不能未验证就采用）
    return False


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



def run_yosys_lec(original_netlist, modified_netlist, run_dir, top_module=None,
                  lib_files=None):
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
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "lec_report.rpt"

    original_netlist = _quote_path(original_netlist)
    modified_netlist = _quote_path(modified_netlist)

    # 顶层模块：未指定则从网表推断
    if not top_module:
        top_module = _infer_top_module(original_netlist)
    if not top_module:
        top_module = "top"

    # 库文件：加载 .lib 避免标准单元被当黑盒（否则等价性检查失真）
    lib_lines = []
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
        f"hierarchy -top {top_module} -check",
        f"design -stash gold",
        "",
        "# 2. 读入修改后网表",
        f"read_verilog {modified_netlist}",
        f"hierarchy -top {top_module} -check",
        "design -stash gate",
        "",
        "# 3. 建立等价映射并证明",
        "design -copy-from gold -as gold gold",
        "design -copy-from gate -as gate gate",
        "equiv_make gold gate equiv",
        "prep -flatten -top",
        "opt_clean",
        "equiv_simple",
        "equiv_induct",
        "",
        "# 4. 判定并输出统计",
        "equiv_status -assert",
        "equiv_status",
        "stat",
        "",
        "exit",
    ]
    script = "\n".join(script_lines)
    script_path = run_dir / "lec.ys"
    script_path.write_text(script, encoding="utf-8")

    try:
        proc = subprocess.run(
            [config.YOSYS, "-q", "-s", str(script_path)],
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
    if re.search(r"\bunproven\b", text, re.IGNORECASE):
        return False

    m = re.search(r"(\d+)\s+unmatched", text)
    if m and int(m.group(1)) > 0:
        return False

    # 正向证据
    if re.search(r"equiv_status:\s*PASS", text, re.IGNORECASE):
        return True
    if re.search(r"successfully\s+proved", text, re.IGNORECASE):
        return True
    if re.search(r"No\s+unproven\s+\$?equiv\s+cells", text, re.IGNORECASE):
        return True

    matched = re.search(r"(\d+)\s+matched", text)
    if matched and int(matched.group(1)) > 0:
        # 有 matched 且 unmatched==0（上面已确认没有 >0 的 unmatched）
        return True

    # 无任何正向证据 -> 不判通过（宁可放弃网表修改，也不能未验证就采用）
    return False


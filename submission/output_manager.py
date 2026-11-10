#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
输出管理模块
按赛题规范组织输出目录：
  output/
    decision_log.json
    runs/R{n}/...
    final_results/...
    diffs/...
"""

import os
import shutil
from pathlib import Path

# 构建标识：写进 <output>/.build_id。
# 换值即可让所有复用目录自动清理一次（配合 setup_output_dirs 的跨构建防护）。
BUILD_ID = "2026-10-21-p0"



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

def _link_or_copy(src, dst):
    """
    把 src 落位到 dst：优先硬链接，失败时回退为复制。

    为什么要这么做：大网表 case 的单轮产物体量极大（task_1/case5 实测
    reports 455MB + deliverables 200MB + final.log 198MB），而 final_results
    只是 runs/R{n}/ 的一份"提纯视图"，同一份字节被复制一次纯属浪费。
    硬链接是把同一个 inode 挂到第二个目录项，代价与文件大小无关，能把
    这类 case 的落盘开销从几十秒压到毫秒级。

    安全性：被链接的都是**每轮独立、落盘后只读**的产物（R{n}.log / post_scan.v /
    *.rpt 等），不存在"改一处影响另一处"的问题；dst 已存在时先 unlink，
    不会破坏已有的正确轮次。跨文件系统（EXDEV）或无权限时自动回退复制，
    行为与原来的 shutil.copy2 完全一致。
    """
    src = str(src)
    dst = str(dst)
    try:
        if os.path.exists(dst):
            os.unlink(dst)
        os.link(src, dst)
        return
    except OSError:
        pass
    shutil.copy2(src, dst)


def setup_output_dirs(output_dir):
    """
    创建顶层输出目录。

    同时做「跨构建污染」防护：把本次代码的 BUILD_ID 写进 output/.build_id，
    若目录里已有**其它** BUILD_ID 的产物，说明这个 output 目录被复用
    （本地反复跑同一 case 时最常见），此时把 runs/ final_results/ diffs/
    全部清掉 —— 否则上一版留下的文件（例如旧代码伪造的 post_scan.v）
    会被当成"本次产物"带进最终结果。
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    marker = out / ".build_id"
    stale = False
    try:
        if marker.exists():
            old = marker.read_text(encoding="utf-8", errors="replace").strip()
            stale = bool(old) and old != BUILD_ID
        else:
            # 没有标记但已有产物：同样视为旧目录（首次运行的目录是空的）
            stale = (out / "runs").exists() or (out / "final_results").exists()
    except OSError:
        stale = False

    if stale:
        for sub in ("runs", "final_results", "diffs"):
            shutil.rmtree(out / sub, ignore_errors=True)
        print("  ⚠ 输出目录含其它构建的旧产物，已清理（避免旧文件混入本次结果）")

    try:
        marker.write_text(BUILD_ID, encoding="utf-8")
    except OSError:
        pass

    (out / "runs").mkdir(parents=True, exist_ok=True)
    (out / "final_results" / "deliverables").mkdir(parents=True, exist_ok=True)
    (out / "final_results" / "reports").mkdir(parents=True, exist_ok=True)
    (out / "diffs").mkdir(parents=True, exist_ok=True)
    return out


def copy_reports_from_run(run_dir, reports_dir):
    """把工具在 run_dir/output/reports 或 run_dir/output 下生成的 .rpt 复制到指定 reports 目录。"""
    run_dir = Path(run_dir)
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)

    candidates = [
        run_dir / "reports",
        run_dir / "output" / "reports",
        run_dir / "output" / "report",
        run_dir / "output",
        # 兜底：若 dofile 的输出变量没按预期重写，报告可能落在 deliverables/ 下
        run_dir / "deliverables" / "reports",
        run_dir / "deliverables" / "output" / "reports",
        run_dir / "deliverables" / "output",
    ]
    copied = []
    for cand in candidates:
        if not cand.exists():
            continue
        for rpt in cand.rglob("*.rpt"):
            # 跳过 .old / .bak 垃圾文件
            if rpt.suffix in (".old", ".bak"):
                continue
            try:
                _link_or_copy(rpt, reports_dir / rpt.name)
                copied.append(rpt.name)
            except Exception:
                pass
    return copied


def finalize_outputs(output_dir, final_run, task, netlist_change=None, incremental=False):
    """
    将最终采用的 run 的产物整理到 final_results/。

    Args:
        output_dir: 顶层输出目录
        final_run: 最终采用的 run 结果 dict
        task: 任务解析结果
        netlist_change: 最终采用的网表级修复记录（任务二；无则 None）。
            用于按 docx 8.2.2 落位 pre_scan_final.v / lec_report.rpt。
        incremental: True 表示「每轮结束的增量落盘」。此时大产物（.v/.ctl/.def/
            .scandef）只在目标不存在时复制，避免 6 轮 × 几十 MB 网表反复拷贝；
            final.log / final.dofile / 报告仍会刷新，保证「最优轮次」的文本
            产物总是最新（被杀也能看到已完成的轮次）。

    Returns:
        list: 复制的文件清单
    """
    out = Path(output_dir)
    final_dir = out / "final_results"
    deliverables = final_dir / "deliverables"
    reports = final_dir / "reports"

    # 终态整理时**必须先清空** final_results。
    #
    # 为什么：final_results 只做「增量落盘」而不清理，就会把上一版/上一轮/上一次
    # 构建留下的文件带进最终交付物。实测事故：public_cases/task_1/case1 的
    # final_results/deliverables/post_scan.v 是旧版本代码把 post_replace_unscan.v
    # **改名**产生的伪造产物（该轮 tools 从未 dump 过 post_scan.v，两文件同为
    # 136523 行）。改名逻辑虽已删除，但文件还在，只要复用 output 目录就会被带进评测
    # —— 触碰 docx 5.4.2「不得伪造结果」。
    #
    # incremental=True 是每轮结束的中间态（可能被 kill），不能整棵删（大网表重拷贝
    # 代价高），此时保留增量语义；终态调用（incremental=False）负责清理。
    if not incremental and final_dir.exists():
        shutil.rmtree(final_dir, ignore_errors=True)

    deliverables.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)

    copied = []

    run_dir = Path(final_run.get("run_dir", ""))

    def _skip_big(dst, src=None):
        """
        增量模式下，大产物（.v/.ctl/.def/.scandef）不重复拷贝，避免多轮反复搬运
        几十 MB 网表。但**不能只看目标是否存在** —— 若 R1 已落下 post_scan.v、而
        真正的最优轮是 R5，仅凭 exists 会跳过 R5 的网表，造成
        final.log(R5) 与 post_scan.v(R1) 轮次错配；进程被外部 kill 时这个错配
        就会固化成最终结果。因此仅在「内容判同」时才跳过（大小一致视为同源）。
        """
        if not incremental or not dst.exists():
            return False
        if src is None or not Path(src).exists():
            return True
        try:
            return dst.stat().st_size == Path(src).stat().st_size
        except OSError:
            return False

    # 1. 复制日志 -> final.log
    if final_run.get("log_file") and Path(final_run["log_file"]).exists():
        _link_or_copy(final_run["log_file"], final_dir / "final.log")
        copied.append("final.log")

    # 2. 复制 dofile -> final.dofile
    if final_run.get("dofile_path") and Path(final_run["dofile_path"]).exists():
        _link_or_copy(final_run["dofile_path"], deliverables / "final.dofile")
        copied.append("deliverables/final.dofile")

    # 3. 复制工具生成的网表/ctl/def/scandef（递归查找，覆盖多级 output 子目录）
    if run_dir.exists():
        tool_out = run_dir / "output"
        if tool_out.exists():
            seen_names = set()
            for f in sorted(tool_out.rglob("*")):
                if not f.is_file():
                    continue
                # 跳过 .old / .bak 垃圾文件
                if f.suffix in (".old", ".bak"):
                    continue
                if f.suffix not in (".v", ".ctl", ".def", ".scandef"):
                    continue
                name = f.name
                # 同名文件（如多级目录下重复）只保留一个，优先保留更浅的
                if name in seen_names:
                    continue
                seen_names.add(name)
                if _skip_big(deliverables / name, f):
                    continue
                _link_or_copy(f, deliverables / name)
                copied.append(f"deliverables/{name}")

    # 注意：早期实现在这里把 post_replace_unscan.v **复制改名**成 post_scan.v，
    # 目的是「补全 docx 8.2 要求的 post_scan.v」。但工具从未产出过该文件，
    # 这属于 docx 5.4.2「不得伪造结果」明确禁止的行为（伪造产物），
    # 而且 task_1/case1、case4 的任务书明确说明本流程**不生成扫描链**、
    # 不产出 post_scan.v。已删除该步骤：工具没产出就不报，不做任何改名。

    # 4. 复制报告（递归查找所有 .rpt）
    reports_copied = copy_reports_from_run(run_dir, reports)
    for r in reports_copied:
        copied.append(f"reports/{r}")

    # 5. 网表级修复（任务二，docx 8.2.2）：
    #    最终采用的 Pre-scan 网表 -> final_results/deliverables/pre_scan_final.v
    #    功能等价检查报告           -> final_results/reports/lec_report.rpt
    if netlist_change and netlist_change.get("applied"):
        modified = netlist_change.get("modified_path", "")
        if modified and Path(modified).exists():
            if not _skip_big(deliverables / "pre_scan_final.v", modified):
                _link_or_copy(modified, deliverables / "pre_scan_final.v")
                copied.append("deliverables/pre_scan_final.v")
            # 同一份也放进"最终采用轮次"的 deliverables/（docx 要求按轮次保留）
            if run_dir.exists():
                (run_dir / "deliverables").mkdir(parents=True, exist_ok=True)
                # 用「实际修改网表的那一轮」命名，与 decision_log.file_changes.path 保持一致
                rid = netlist_change.get("applied_run") or final_run.get("run_id", "")
                rname = f"pre_scan_{rid}.v" if rid else "pre_scan_final.v"
                _link_or_copy(modified, run_dir / "deliverables" / rname)
        lec_report = (netlist_change.get("lec") or {}).get("report", "")
        if lec_report and Path(lec_report).exists():
            _link_or_copy(lec_report, reports / "lec_report.rpt")
            copied.append("reports/lec_report.rpt")
            if run_dir.exists():
                (run_dir / "reports").mkdir(parents=True, exist_ok=True)
                _link_or_copy(lec_report, run_dir / "reports" / "lec_report.rpt")
            # docx 8.2.2 还要求 diffs/pre_scan_netlist_orig_to_R{n}.diff
            _write_netlist_diff(
                out, netlist_change,
                netlist_change.get("applied_run") or final_run.get("run_id", "R1"))

    return copied



def _write_netlist_diff(output_dir, netlist_change, run_id):
    """写出原始 Pre-scan 网表与修改后网表的 diff（docx 8.2.2 的 diffs/ 要求）。"""
    import difflib

    orig = netlist_change.get("original_path", "")
    mod = netlist_change.get("modified_path", "")
    if not (orig and mod and Path(orig).exists() and Path(mod).exists()):
        return
    try:
        a = Path(orig).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        b = Path(mod).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return
    diffs_dir = Path(output_dir) / "diffs"
    diffs_dir.mkdir(parents=True, exist_ok=True)
    diff = difflib.unified_diff(a, b, fromfile="pre_scan.v (original)",
                                tofile=f"pre_scan_{run_id}.v", lineterm="\n")
    (diffs_dir / f"pre_scan_netlist_orig_to_{run_id}.diff").write_text(
        "".join(diff), encoding="utf-8")


def generate_diffs(output_dir, run_results, original_dofile_text=None):
    """
    生成 diffs/ 下的版本差异。

      · 任务二：先写 `dofile_orig_to_R1.diff`（修复起点 original.dofile -> 首轮采用
        的 dofile）—— file_changes 的第一条改动记录（Fn）就指向它；
      · 相邻轮次：`dofile_R{n}_to_R{n+1}.diff`（仅在内容确实变化时生成）。
    """
    import difflib

    out = Path(output_dir)
    diffs_dir = out / "diffs"
    diffs_dir.mkdir(parents=True, exist_ok=True)

    # 0. 任务二：修复起点 -> 首轮
    if original_dofile_text and run_results:
        first = run_results[0]
        rid = first.get("run_id", "R1")
        p = _dofile_file(first)
        if p.exists():
            a = original_dofile_text.splitlines(keepends=True)
            try:
                b = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            except OSError:
                b = []
            if b and a != b:
                diff = difflib.unified_diff(
                    a, b, fromfile="input/original.dofile",
                    tofile=f"runs/{rid}/deliverables/{rid}.dofile", lineterm="\n")
                (diffs_dir / f"dofile_orig_to_{rid}.diff").write_text(
                    "".join(diff), encoding="utf-8")

    for i in range(len(run_results) - 1):
        cur = run_results[i]
        nxt = run_results[i + 1]
        cur_path = _dofile_file(cur)
        nxt_path = _dofile_file(nxt)
        if not cur_path.exists() or not nxt_path.exists():
            continue
        cur_text = cur_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        nxt_text = nxt_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        diff = difflib.unified_diff(
            cur_text, nxt_text,
            fromfile=f"runs/{cur['run_id']}/deliverables/{cur['run_id']}.dofile",
            tofile=f"runs/{nxt['run_id']}/deliverables/{nxt['run_id']}.dofile",
            lineterm="\n",
        )
        fname = f"dofile_{cur['run_id']}_to_{nxt['run_id']}.diff"
        (diffs_dir / fname).write_text("".join(diff), encoding="utf-8")

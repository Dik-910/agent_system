#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日志与报告解析模块
解析 dftexp_scan 的运行日志，提取错误、警告、DRC 违例、扫描链统计等，
用于判断运行成败并驱动多轮修复。
"""

import re


# 显式错误标记（dftexp_scan 的报错行都带这个前缀）
_ERROR_MARK_RE = re.compile(r"\[(?:ERROR|FATAL)\]")
# 行首的 ERROR/FATAL（工具另一种输出风格）
_ERROR_LINE_RE = re.compile(r"^\s*(?:ERROR|FATAL)\b")
# 「0 errors / no error」这类统计摘要不是错误，避免误判
_ZERO_ERR_RE = re.compile(r"(?:no|0|zero|without)\s+(?:error|fatal)", re.I)
# 警告判定（与错误同样分「行首标记」与「行内裸词」两类）
_WARN_LINE_RE = re.compile(r"^\s*WARNING\b")
_WARN_BARE_RE = re.compile(r"\bWARNING\b")


# 单条错误/警告的保存上限。
#
# 大设计的日志里同一条 DRC 告警会刷几十万次（task_1/case5 实测 69,022 条
# DFTDRC-4001 "Clock input ... is uncontrolled"），全量留在内存里既无意义
# （诊断只需要"有哪些问题、各多少次"）又占几十 MB。超出后只计数。
_MAX_KEPT = 2000

# 统计类正则（逐行处理时对每行匹配；原实现是在整份日志上 re.search）
_TOTAL_VIOL_RE = re.compile(r"Total\s+violations?\s*[:=]\s*(\d+)", re.IGNORECASE)

# DRC 逐条汇总行（日志与 *.rpt 里格式相同，.rpt 会多一个 [INFO] 前缀）：
#   [INFO] [DFTDRC-7001] There were 2782 DRC rule 'DFTR1' fails. (Clock input ...)
#   [INFO] There were 1 DRC rule 'DFTR10' fails. (Clock is connected to data input of cell)
#
# 旧实现用的是 `\[(DFTDRC-?\d*)\]\s*(\w+)\s*x?(\d+)`，它要求紧跟 "[DFTDRC-xxxx]"
# 之后是「单词 + 数字」，而真实报文是 "There were 2782 DRC rule 'DFTR1' fails"，
# 于是**一条都匹配不到** —— drc_violations 恒空、total_violations 恒 0，
# 下游 run_clean 里的「DRC 违例为 0」条件形同虚设（task_1/case5 的 69022 条
# DFTR1 全程不可见）。这里改为直接解析工具自己的汇总口径。
_DRC_RULE_SUM_RE = re.compile(
    r"There\s+(?:were|was)\s+(\d+)\s+DRC\s+rule\s+'?\"?(DFTR[\w-]+)'?\"?", re.IGNORECASE)

_CHAIN_RE = re.compile(r"Number\s+of\s+chains?\s*[:=]?\s*(\d+)", re.IGNORECASE)
_CHAIN_ALT_RE = re.compile(r"(\d+)\s+(?:scan\s+)?chains\b", re.IGNORECASE)
_FLOP_RE = re.compile(r"(\d+)\s+(?:DFF|flip-?flops|FF)", re.IGNORECASE)


def _iter_log_lines(log_text=None, log_path=None):
    """按行迭代日志：优先 log_path（流式，不把整份日志读进内存），否则用 log_text。"""
    if log_path:
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    yield ln
            return
        except OSError:
            pass
    if log_text:
        for ln in log_text.splitlines():
            yield ln


def parse_log(log_text=None, log_path=None):
    """
    解析运行日志，返回结构化结果。

    Args:
        log_text: 日志全文（小日志，或只需解析片段时用）。
        log_path: 日志文件路径 —— **推荐**。单 case 日志可达 200MB
            （task_1/case5 实测 198MB，其中含 5 条 1400 万字符的 SCAN-7601 单行），
            走文件时逐行流式处理，不占内存。两者同时给时 log_path 优先。

    Returns:
        dict: {
            "errors": [错误行],
            "warnings": [警告行],
            "drc_violations": [{"code","rule","count"}],
            "total_violations": int,
            "chain_count": int 或 None,
            "flop_count": int 或 None,
            "fatal": bool,
            "warning_total": int,   # 告警总数（errors/warnings 列表各自最多 _MAX_KEPT 条）
            "error_total": int,
        }
    """
    result = {
        "errors": [],
        "warnings": [],
        "drc_violations": [],
        "drc_rules": {},          # {规则号: 条数}，工具自己的汇总口径
        "total_violations": 0,
        "chain_count": None,
        "flop_count": None,
        "fatal": False,
        "warning_total": 0,
        "error_total": 0,
    }

    if not log_text and not log_path:
        return result

    for line in _iter_log_lines(log_text, log_path):
        # 错误判定（保持原语义）：
        #   1) 带 [ERROR]/[FATAL] 标记 -> 一定是错误；
        #   2) 行首 ERROR/FATAL，或行内出现独立单词 ERROR/FATAL（且不是 "0 errors" 类摘要）。
        # 大日志下每行跑 3 个正则的代价可接受：198MB 逐行实测 ~1s，
        # 而错误行必须一条不漏（它直接决定该轮是否算失败、要不要重试）。
        marked = bool(_ERROR_MARK_RE.search(line))
        bare = bool(_ERROR_LINE_RE.search(line)) or bool(
            re.search(r"\b(?:ERROR|FATAL)\b", line))
        if marked or (bare and not _ZERO_ERR_RE.search(line)):
            result["error_total"] += 1
            if len(result["errors"]) < _MAX_KEPT:
                result["errors"].append(line.strip())
            result["fatal"] = True
            continue
        if "[WARNING]" in line or _WARN_LINE_RE.search(line) or _WARN_BARE_RE.search(line):
            result["warning_total"] += 1
            if len(result["warnings"]) < _MAX_KEPT:
                result["warnings"].append(line.strip())

        # ── 统计类：原实现在整份文本上 re.search，取首个命中，这里逐行等价 ──
        if result["total_violations"] == 0:
            m = _TOTAL_VIOL_RE.search(line)
            if m:
                result["total_violations"] = int(m.group(1))
        if result["chain_count"] is None:
            m = _CHAIN_RE.search(line) or _CHAIN_ALT_RE.search(line)
            if m:
                result["chain_count"] = int(m.group(1))
        if result["flop_count"] is None:
            m = _FLOP_RE.search(line)
            if m:
                result["flop_count"] = int(m.group(1))
        m = _DRC_RULE_SUM_RE.search(line)
        if m:
            cnt, rule = int(m.group(1)), m.group(2).upper()
            result["drc_rules"][rule] = result["drc_rules"].get(rule, 0) + cnt
            if len(result["drc_violations"]) < _MAX_KEPT:
                result["drc_violations"].append({
                    "code": "DFTDRC-7001",
                    "rule": rule,
                    "count": cnt,
                })

    # 没有显式的 "Total violations:" 行时，用逐规则汇总求和兜底
    # （`examine_scan_drc -file x.rpt` 只把汇总写进文件、日志里没有 Total 行）。
    if result["total_violations"] == 0 and result["drc_rules"]:
        result["total_violations"] = sum(result["drc_rules"].values())

    return result


# ============================================================================
# DRC 报告文件（*.rpt）
# ----------------------------------------------------------------------------
# `examine_scan_drc -file <path>` 的输出格式（实测 task_1/case2、case5）：
#
#   Design: ac97_top
#   --------------------------------------------------
#   DRC Report
#   Total violations: 1
#   --------------------------------------------------
#   [INFO] There were 1 DRC rule 'DFTR10' fails. (Clock is connected to data input of cell)
#
# 该文件是 DRC 结论的**唯一权威来源**：走 -file 时日志里只有 "Starting design
# rule checking."，没有 Total 行，因此必须把 rpt 读进来，否则「DRC 违例清零」
# 这件事在系统里没有任何机器证据。
# ============================================================================
_DRC_RPT_HINT_RE = re.compile(r"drc", re.IGNORECASE)
_MAX_RPT_BYTES = 4 << 20          # 单份 DRC 报告上限（实测几百字节~几KB）


def parse_drc_report(text=None, path=None):
    """
    解析一份 DRC 报告，返回 {"total_violations": int, "rules": {规则号: 条数}}。

    解析失败/文件不存在时返回全零结构，不抛异常（判据缺失由调用方决定如何处理）。
    """
    out = {"total_violations": 0, "rules": {}}
    lines = []
    if path:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                read = 0
                for ln in fh:
                    lines.append(ln)
                    read += len(ln)
                    if read > _MAX_RPT_BYTES:
                        break
        except OSError:
            return out
    elif text:
        lines = text.splitlines()

    for line in lines:
        m = _TOTAL_VIOL_RE.search(line)
        if m:
            out["total_violations"] = int(m.group(1))
        m = _DRC_RULE_SUM_RE.search(line)
        if m:
            cnt, rule = int(m.group(1)), m.group(2).upper()
            out["rules"][rule] = out["rules"].get(rule, 0) + cnt
    if out["total_violations"] == 0 and out["rules"]:
        out["total_violations"] = sum(out["rules"].values())
    return out


def find_drc_reports(run_dir):
    """列出某轮目录下的 DRC 报告（reports/ 与 output/ 两处，文件名含 drc）。"""
    from pathlib import Path
    run_dir = Path(run_dir or "")
    if not run_dir or not run_dir.is_dir():
        return []
    hits = []
    for sub in ("reports", "output", ""):
        base = run_dir / sub if sub else run_dir
        if not base.is_dir():
            continue
        try:
            for f in sorted(base.glob("*.rpt")):
                if f.is_file() and _DRC_RPT_HINT_RE.search(f.name):
                    hits.append(f)
        except OSError:
            continue
    # 去重（reports/ 与 output/ 可能硬链接同一 inode）
    seen, uniq = set(), []
    for f in hits:
        key = (f.name, f.stat().st_size if f.exists() else 0)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(f)
    return uniq


def collect_drc(run_dir=None, parsed=None):
    """
    把「日志里的 DRC 汇总」与「run 目录下 DRC 报告」合并成一份权威结论。

    Returns:
        {"total_violations": int, "rules": {规则号: 条数}, "sources": [文件名]}
    """
    merged = {"total_violations": 0, "rules": {}, "sources": []}
    for f in find_drc_reports(run_dir):
        one = parse_drc_report(path=str(f))
        if not one["rules"] and not one["total_violations"]:
            continue
        merged["sources"].append(f.name)
        for rule, cnt in one["rules"].items():
            merged["rules"][rule] = merged["rules"].get(rule, 0) + cnt
        merged["total_violations"] = max(merged["total_violations"],
                                         one["total_violations"])

    log_rules = dict((parsed or {}).get("drc_rules") or {})
    if log_rules:
        for rule, cnt in log_rules.items():
            # 日志与 rpt 同时有该规则时取较大值，避免同一份报告被算两遍
            merged["rules"][rule] = max(merged["rules"].get(rule, 0), cnt)
        merged["total_violations"] = max(
            merged["total_violations"],
            int((parsed or {}).get("total_violations") or 0),
            sum(merged["rules"].values()))
    elif merged["rules"]:
        merged["total_violations"] = max(merged["total_violations"],
                                         sum(merged["rules"].values()))
    return merged


def drc_effective(parsed, allowed_rules=()):
    """
    扣除「任务书明确允许残留」的规则后，还剩多少违例。

    Args:
        parsed: parse_log 的结果（需带 drc_rules / total_violations）；
        allowed_rules: 允许残留的规则号（如 task_1/case5 的 DFTR10/11/12）。

    Returns:
        {"total": int, "by_rule": {...}, "allowed": {...}}
    """
    allowed = {str(r).upper() for r in (allowed_rules or ())}
    by_rule = dict(parsed.get("drc_rules") or {})
    allowed_hit = {r: n for r, n in by_rule.items() if r in allowed}
    eff = {r: n for r, n in by_rule.items() if r not in allowed}
    if by_rule:
        total = sum(eff.values())
    else:
        # 只有总数字、没有逐规则明细时，无法判断豁免，按「全部有效」处理
        total = int(parsed.get("total_violations") or 0)
    return {"total": total, "by_rule": eff, "allowed": allowed_hit}


def parse_chain_report(report_text):
    """解析扫描链报告，提取链数与链长信息。"""
    if not report_text:
        return {"chain_count": 0, "chains": []}

    chains = []
    # 常见的链报告格式较多样，这里做尽力解析
    m = re.search(r"(?:Number of chains|chain_count|total chains)\s*[:=]\s*(\d+)", report_text, re.IGNORECASE)
    chain_count = int(m.group(1)) if m else 0

    # 提取每行链信息（尽力）
    return {"chain_count": chain_count, "chains": chains}


def summarize_run(run_result):
    """对一次运行结果做摘要，供 decision_log / 修复决策使用。

    优先读日志文件（log_file）—— run_result 里不再携带日志全文，
    大 design 的日志有 200MB 级。
    """
    parsed = parse_log(log_path=run_result.get("log_file"))
    if not parsed["errors"] and not parsed["warnings"] \
            and parsed["chain_count"] is None and not parsed.get("warning_total"):
        # 兜底：没有 log_file 时退回 output 片段（小日志场景）
        parsed = parse_log(log_text=run_result.get("output", ""))
    return parsed

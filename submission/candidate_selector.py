#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
候选方案择优模块

当 LLM 生成多个候选 dofile 时，通过静态评分 +（可选）运行结果评分，
选出最优方案进入下一轮。避免「只生成一个、可能不是最优」的问题。

评分维度（静态）：
  1. 语法完整性：必须包含关键命令（load_lib / load_netlist / present_design / exit）
  2. 命令覆盖率：task_spec 需求对应的命令是否齐全（clock / scan_enable / scan_cfg / insert）
  3. 结构合理性：路径写法正确、无残留 Markdown、命令顺序符合工作流
  4. （可选）运行结果：成功且 DRC 违例少的候选加分
"""

import re


# 关键命令清单
_REQUIRED_COMMANDS = ["load_lib", "load_netlist", "present_design", "insert_dft_logic", "exit"]

# 常见需求命令映射
_REQUIREMENT_COMMANDS = {
    "clock": r"set_scan_signal\s+.*-type\s+clock",
    "scan_enable": r"set_scan_signal\s+.*-type\s+scan_enable",
    "reset": r"set_scan_signal\s+.*-type\s+reset",
    "scan_cfg": r"set_scan_cfg",
    "mapping": r"set_scan_cell_mapping",
    "wrapper": r"set_wrapper_cfg",
    "icg": r"set_dft_clock_gating_cfg",
    "segment": r"set_scan_segment",
    "drc": r"examine_scan_drc",
    "chain": r"examine_scan_chain",
}


def _static_score(dofile_text, task):
    """
    对 dofile 做静态评分，返回 (score, details)。

    score 越大越优。
    """
    details = {}
    score = 0

    # 1. 关键命令完整性（每项 10 分，共 50）
    for cmd in _REQUIRED_COMMANDS:
        if re.search(rf"\b{cmd}\b", dofile_text):
            score += 10
            details[cmd] = True
        else:
            details[cmd] = False

    # 2. 需求命令覆盖率（每项 5 分）
    spec = task.get("task_spec", "")
    for name, pattern in _REQUIREMENT_COMMANDS.items():
        # 仅当 task_spec 提到相关需求时才要求该命令
        if _requirement_mentioned(spec, name):
            if re.search(pattern, dofile_text):
                score += 5

    # 3. 结构合理性
    # 无 Markdown 代码块残留
    if "```" not in dofile_text:
        score += 5
    # 无 Markdown 标题残留
    if not re.search(r"^#{1,6}\s", dofile_text, re.MULTILINE):
        score += 5
    # 有 exit 结尾
    if re.search(r"^\s*exit\s*$", dofile_text, re.MULTILINE):
        score += 5

    # 4. 路径健康（输入应为绝对路径或 /input/ 前缀，避免 $script_dir/lib 相对路径失效）
    if re.search(r'(/input/|"E:/|"/[A-Za-z]:/)', dofile_text):
        score += 5

    return score, details


def _requirement_mentioned(spec, name):
    """判断 task_spec 是否提到某类需求（用于按需检查命令）。"""
    keywords = {
        "clock": ["时钟", "clock", "clk"],
        "scan_enable": ["scan_enable", "扫描使能", "scan enable", "测试使能"],
        "reset": ["复位", "reset", "rst"],
        "scan_cfg": ["扫描链", "scan chain", "chain_count", "链数", "链长"],
        "mapping": ["映射", "mapping", "DFF", "SFF", "替换"],
        "wrapper": ["wrapper", "Wrapper", "边界", "包裹"],
        "icg": ["ICG", "时钟门控", "clock gating", "门控时钟"],
        "segment": ["segment", "移位寄存器", "scan segment"],
        "drc": ["DRC"],
        "chain": ["扫描链", "chain", "插链"],
    }
    kws = keywords.get(name, [name])
    return any(k.lower() in spec.lower() for k in kws)


def select_best_candidate(candidates, task, run_results=None):
    """
    从候选 dofile 中选出最优。

    Args:
        candidates: list of {"dofile": str, "source": str}
        task: 任务解析结果
        run_results: 可选，每个候选的运行结果（与 candidates 对齐）

    Returns:
        str: 最优 dofile 文本
    """
    if not candidates:
        return ""

    best_dofile = candidates[0]["dofile"]
    best_score = -1

    for i, cand in enumerate(candidates):
        dofile = cand.get("dofile", "")
        score, _ = _static_score(dofile, task)

        # 违规命令（幻觉命令）重罚：每个违规命令扣 50 分，确保不选有幻觉的候选
        violations = cand.get("violations", [])
        score -= len(violations) * 50

        # 若提供运行结果，成功 + DRC 违例少加分
        if run_results and i < len(run_results):
            r = run_results[i]
            if r.get("success"):
                score += 30
            parsed_violations = r.get("violations", 0)
            score -= parsed_violations  # 违例越少越好

        if score > best_score:
            best_score = score
            best_dofile = dofile

    return best_dofile


def score_dofile(dofile_text, task):
    """对外暴露的静态评分（供日志/决策记录使用）。"""
    score, details = _static_score(dofile_text, task)
    return score

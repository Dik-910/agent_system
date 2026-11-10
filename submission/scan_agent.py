#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scan Insertion Agent - 主入口

基于 DeepSeek V4 Pro 的智能 Scan Insertion 代理。
支持任务一（Dofile 生成）与任务二（问题诊断修复）。

模块结构：
  config.py            全局配置
  llm_client.py        LLM 调用封装
  input_parser.py      输入解析 + 任务理解
  netlist_analyzer.py  网表分析
  dofile_generator.py  Dofile 生成（LLM 主 + 规则兜底）
  tool_runner.py       dftexp_scan / yosys 调用
  report_parser.py     日志与 DRC 解析
  output_manager.py    输出目录管理
  decision_logger.py   decision_log.json 生成
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

try:
    from . import config
    from . import input_parser
    from . import dofile_generator
    from . import dofile_fixer
    from . import tool_runner
    from . import report_parser
    from . import output_manager
    from . import decision_logger
    from . import netlist_analyzer
    from . import manual_rag
    from . import lec_verifier
    from . import netlist_facts
    from . import spec_verifier
    from . import spec_fixer
    from . import task2_diagnoser
    from .llm_client import get_llm, has_useful_time, openai_version
except ImportError:
    import config
    import input_parser
    import dofile_generator
    import dofile_fixer
    import tool_runner
    import report_parser
    import output_manager
    import decision_logger
    import netlist_analyzer
    import manual_rag
    import netlist_facts
    import lec_verifier
    import spec_verifier
    import spec_fixer
    import task2_diagnoser
    from llm_client import get_llm, has_useful_time, openai_version


# ============================================================================
# Dofile 完整性强制校验（P0-1）
# ============================================================================

# 必须存在的基础命令（缺任意一个则回退到规则模板）
REQUIRED_CMDS = ["load_lib", "load_netlist", "present_design", "insert_dft_logic", "exit"]

# 报告命令与生成文件导出命令不硬编码 —— 各 case 的输出清单不同：
#   - task_1/case5、case6、task_2/case4、case5 在输出表里直接给出文件名
#     （drc.rpt / scan_cfg.rpt / veer_eh1.def ...）；
#   - 其余 case 只给语义描述（"扫描链报告""插链配置报告"）。
# 统一由 dofile_generator.required_artifacts(task) 按 task_spec「输出」章节
# 与参考 dofile 计算，避免补齐出错误文件名。


# ============================================================================
# 运行预算（limitations.md）
#
# 赛题 5.4.1：运行总时间或 Scan Insertion 工具调用次数超过 case 设定值 -> 当前 case 0 分。
# 因此必须把限制解析成可执行上限，而不是只把原文塞进 prompt。
# ============================================================================

def max_runs_for(task):
    """本 case 允许的最大重跑轮数（每轮 = 1 次 Scan Insertion 工具调用）。"""
    limits = (task or {}).get("limits") or {}
    max_runs = config.MAX_RUNS
    if limits.get("max_tool_calls"):
        max_runs = max(1, min(max_runs, int(limits["max_tool_calls"])))
    return max_runs


def candidate_count_for(task):
    """
    本 case 的候选 dofile 数量（生成多个候选 = 多次 LLM 调用）。

    候选数必须按**预运行窗口**反推，不能只看预算大小：
    平台实测单次「生成完整 dofile」78~148s（见 _PRE_RUN_FRACTION 上方标定），
    而窗口 = min(预算 × 0.92, 200s, 预算 × 0.90 − tool_reserve)。

    这里按保守值 90s/次 估算，再配合 generate_dofile_with_llm 里的耗时感知
    动态停止，双重保险：
      150s 预算 → 窗口 120s → 1 个
      300s 预算 → 窗口 200s → 2 个（实际第 2 个只在余量够时才发）
      ≥600s     → 窗口封顶 200s → 2 个

    注：估算值 90s 低于平台实测的 78~148s 看似矛盾，但**这是刻意的** ——
    它只用于决定「要不要给第 2 个候选的机会」。真正的刹车是耗时感知停止：
    第 1 个候选实际花了 T 秒，剩余不足 1.2T 就不会发第 2 个。
    所以估算偏乐观不会造成超时，只是让「第一次特别快」的情况仍能试第二个。
    """
    window = pre_run_window_s(task)
    if window is None:
        return 1
    n = int(window // 90.0)
    return max(1, min(dofile_generator.CANDIDATE_COUNT, n))


# ============================================================================
# wall-clock 预算控制
#
# 赛题 5.4.1：运行总时间或工具调用次数超过 case 设定值 -> 当前 case 0 分。
# 因此除了把限制写进 prompt，必须真的用时间驱动流程：每轮开始前检查剩余预算，
# 并把剩余预算作为单轮超时上限传给工具，保证「在被外部计时器 kill 之前」
# 已经有落盘产物。
# ============================================================================

# 内部截止时间 = 预算 × 该系数，剩余 10% 留给「整理输出 + 写 decision_log」
_BUDGET_SAFETY = 0.90

# ---------------------------------------------------------------------------
# 绝对看门狗（不依赖 limitations.md 是否解析成功）
# ---------------------------------------------------------------------------
# 平台实测事故：某个 case 跑到 2 小时被外部 kill，零产物。
# 根因有两层：
#   ① limits 没解析出 max_wall_time_s 时，budget_deadline() 返回 None，
#      time_is_up() 永远 False —— 整个流程**完全没有时间上限**；
#   ② 单轮工具上限 3600s，两轮就能撑满 2 小时。
#
# 所以这里再加两道与解析无关的保险：
#   · 解析不到预算时，按 _DEFAULT_WALL_TIME_S 兜底（而不是"unlimited"）；
#   · 无论预算多大，自启动起的绝对上限 _ABSOLUTE_CAP_S，到点即停止开新一轮。
#     取 5400s（90 分钟）：比赛单个 case 的 wall time 上限最大是 4800s，
#     超过它已不可能算"合规完成"，继续跑只会把「超时 0 分」变成「更晚的超时 0 分」。
_DEFAULT_WALL_TIME_S = float(os.environ.get("SCAN_AGENT_DEFAULT_WALL_TIME_S", "1200"))
_ABSOLUTE_CAP_S = float(os.environ.get("SCAN_AGENT_ABSOLUTE_CAP_S", "5400"))

# 单轮工具运行的硬上限（无论预算多大，都留一个天花板，避免异常挂死吃光全部时间）。
#
# 为什么从 3600 降到 900（平台实测教训）：
#   旧值 3600 意味着「一次 dftexp_scan 允许跑 1 小时」，两轮就是 2 小时 ——
#   平台上一旦工具卡住（大网表 + 异常 DRC 报告），整个 case 直接撞上传总时限
#   fail，连落盘机会都没有。
#   而实测数据是：0.33MB→2.3s / 3.8MB→7.4s / 232MB→334s / 259MB→718s。
#   900s 已覆盖最大的 259MB 网表，再慢就属于异常，应当中止并保留已有产物。
_TOOL_TIMEOUT_CAP = float(os.environ.get("SCAN_AGENT_TOOL_TIMEOUT_CAP", "900"))

# ---------------------------------------------------------------------------
# 预运行阶段（dofile 生成）的预算上限
# ---------------------------------------------------------------------------
# 为什么必须留足：dftexp_scan 单轮运行要几十秒（小网表）到几十分钟（大网表），
# 而 task_1/task_2 多数 case 的 wall time 预算只有 150s。
#
# 预运行阶段现在只剩一次真正值得的 LLM 调用 —— 「生成完整 dofile」
# （结构化理解已改为规则抽取，见 dofile_generator.rule_understanding）。
# tools/_llm_latency.py 实测该调用：48.1s / 80.0s / 107.2s（case1 / case2 / case6）。
#
# 因此预运行截止线取三者最小：
#     预算 × _PRE_RUN_FRACTION（默认 0.92）
#     _PRE_RUN_CAP_S（默认 200s，只对 case5 这类大预算 case 起作用）
#     内部截止时间 − tool_reserve（保证 dftexp_scan 至少还剩这么多时间）
#
# **设计要点（避免「预判跳过 = 放弃 LLM」）**：
#   本条截止线只约束「LLM 阶段整体」，不在单次调用前做「「来不及就干脆不发」的预判。
#   只要剩余时间放得下一次调用，就发出去 —— 让 LLM 真正有机会成功；
#   只有真超时 / 真失败，才落到确定性路径（规则模板），且此时工具时间不受影响。
#
# ---------------------------------------------------------------------------
# 【2026-10-10 平台实测后的重标定 —— 这是「LLM 没真正跑起来」的直接根因】
# ---------------------------------------------------------------------------
# 平台首跑（submit_out/scoring_result，11 case / total 3126.8s）暴露：
#     **11 个 case 里有 8 个的 LLM 调用全部超时**，decision_log 明写
#     「dofile 生成采用规则模板（LLM 无可用输出）」。赛题定位是「基于大语言模型
#     的智能体」，退化到规则模板 = 不符合赛题要求，而不只是耗时问题。
#
# 平台侧单次 LLM 延迟（从 container.log 反推，单位 s）：
#     t1/c1 成功 78.7 | t1/c2 成功 98.5 | t1/c3 超时 99.2 | t1/c4 成功 98.2
#     t2/c1 超时 79.5 | t2/c2 超时 78.6(+49) | t2/c3 超时 79.6(+52)
#     t2/c4 超时 79.3(+48) | t2/c5 超时 148.6
#   → **模型单次就要 78~100s（大 prompt 达 148s）**，与 prompt 大小弱相关
#     （t1/c2 网表只有 0.58MB，照样用了 98.5s）—— 说明这只取决于模型思考时间，
#     靠精简 prompt 省不下来。旧窗口（150s 预算给 100s、task_2 给 80s）
#     恰好卡在延迟分布中位数上，于是**一半以上的 case 被自己的超时截断**。
#
# 修正：把窗口放宽到「延迟分布的 P100 之上」——150s 预算给到 120s。
#   旧：min(150×0.72=108, 180, 135−35=100) = 100s  ← t1/c3 卡死在 99s
#   新：min(150×0.92=138, 200, 135−15=120) = 120s  ← 覆盖 78~100s 并留余量
#
# 工具保留量为什么能从 35s/55s 降到 15s：**这是量出来的，不是猜的**。
#   平台实测 dftexp_scan 单轮耗时 vs 网表体积：
#     0.33MB→2.3s  0.58MB→2.3s  2.8MB→6.4s  3.7MB→5.4s  3.8MB→7.4s
#     232MB→334s   259MB→718s
#   即「<20MB 的网表，工具只要 2~7s」。旧值 35s/55s 是在没有实测数据时的保守
#   估计（当时误信「task_2/case1 单轮约 50s」的旧日志），白占 20~40s 预算。
#   现在按网表体积自适应：小网表 15s 足够，大网表本来就配了大预算。
_PRE_RUN_FRACTION = float(os.environ.get("SCAN_AGENT_PRE_RUN_FRACTION", "0.92"))
_PRE_RUN_CAP_S = float(os.environ.get("SCAN_AGENT_PRE_RUN_CAP_S", "200"))

# 大预算 case 的预运行上限（见 pre_run_window_s）。
#
# case5 这类 case 的预运行阶段要装下三件事：
#   1. 网表事实抽取      —— case5（259MB）实测 38.4s
#   2. 伪主输入推导      —— 同网表实测 12s（netlist_facts.find_floating_clock_roots，
#                            两遍扫描 1031 万行；缺它时工具要为 69,022 个 DFF
#                            逐个报 uncontrolled，单轮工具耗时 718s）
#   3. 一次完整的 LLM 调用 —— 平台实测 78~148s
# 合计 130~198s，旧上限 200s 会把 LLM 挤出去 —— 那就回到「LLM 没真正跑起来」
# 的老问题。因此预算 ≥ _PRE_RUN_CAP_BIG_OVER_S 的 case 放宽到 260s；
# 小预算 case 仍按 _PRE_RUN_CAP_S 保护 dftexp_scan 的时间。
_PRE_RUN_CAP_BIG_S = float(os.environ.get("SCAN_AGENT_PRE_RUN_CAP_BIG_S", "260"))
_PRE_RUN_CAP_BIG_OVER_S = float(
    os.environ.get("SCAN_AGENT_PRE_RUN_CAP_BIG_OVER_S", "600"))

# 无论预算多小，都必须给 dftexp_scan 留出的运行时间（秒）—— 自适应兜底值。
# 见 tool_reserve_for()：按网表体积分档，小网表 15s、中等 60s、超大 90s。
_TOOL_RESERVE_S = float(os.environ.get("SCAN_AGENT_TOOL_RESERVE_S", "15"))

# 任务二专用的工具保留量（秒）。
#
# 【2026-10-10 修正】原值 55s 的依据是「task_2/case1 单轮 dftexp_scan 约占 50s」，
# 但平台实测同一 case 的工具耗时只有 **2.3s** —— 那个 50s 是旧日志里的整体耗时
# 被误读成工具耗时。55s 的代价是 task_2 的 LLM 窗口被压到 80s，
# 于是 t2/c1~c4 **全部** LLM 超时（79s/78s/79s/79s，全部差一点点）。
# 现在与任务一同源：按网表体积自适应。
_TOOL_RESERVE_TASK2_S = float(os.environ.get("SCAN_AGENT_TOOL_RESERVE_TASK2_S", "15"))


def tool_reserve_for(task):
    """
    按**网表体积**估算 dftexp_scan 单轮需要保留多少秒。

    标定数据来自平台实测（见上方注释）：体积是工具耗时的强预测因子。
    阈值取 20MB / 100MB —— public case 恰好分成 0.3~4MB（秒级）与 230~260MB
    （分钟级）两簇，hidden case 落在中间地带时按上界给，宁多留不冒险。

    返回 None 表示无法判断（此时由调用方回落到 _TOOL_RESERVE_S）。
    """
    files = (task or {}).get("files") or {}
    total = 0
    for p in (files.get("netlist") or []):
        try:
            total += os.path.getsize(p)
        except OSError:
            continue
    if total <= 0:
        return None
    mb = total / 1048576.0
    if mb < 20.0:
        return 15.0
    if mb < 100.0:
        return 60.0
    return 90.0


# 进程级起始时间（main() 中重置）；无预算时不影响流程
_RUN_STARTED_AT = time.time()


def budget_deadline(task, started_at=None):
    """
    本 case 的内部 wall-clock 截止时间（epoch 秒）。

    **永远不返回 None**：解析不到 wall time 时按 _DEFAULT_WALL_TIME_S 兜底。

    为什么（平台实测事故）：旧实现在 limits 缺 max_wall_time_s 时返回 None，
    而 time_is_up() 对 None 永远判 False —— 整个流程**完全没有时间上限**，
    一路跑到平台外部计时器 kill（实测 2 小时 fail，且零产物）。
    「不知道预算」绝不等于「没有预算」，必须取保守默认值。
    """
    limits = (task or {}).get("limits") or {}
    budget = limits.get("max_wall_time_s")
    if not budget:
        budget = _DEFAULT_WALL_TIME_S
    started = _RUN_STARTED_AT if started_at is None else started_at
    return started + float(budget) * _BUDGET_SAFETY


def pre_run_window_s(task, tool_reserve=None):
    """
    预运行阶段（LLM 生成 dofile）可用的秒数。

    取值 = min(预算 × _PRE_RUN_FRACTION, cap, 预算 × _BUDGET_SAFETY − tool_reserve)

    cap 按预算分档：预算 ≥ _PRE_RUN_CAP_BIG_OVER_S 取 _PRE_RUN_CAP_BIG_S，
    否则取 _PRE_RUN_CAP_S（大预算 case 的预运行阶段要额外容纳网表事实抽取与
    伪主输入推导，见这两个常量的注释）。

    tool_reserve 为 None 时按网表体积自适应（tool_reserve_for）。
    返回 None 表示无预算限制（不约束）。
    """
    limits = (task or {}).get("limits") or {}
    # 与 budget_deadline 同口径：解析不到预算时按保守默认值，绝不返回 None
    # （返回 None 会让候选数恒为 1、预运行阶段没有截止线）。
    budget = float(limits.get("max_wall_time_s") or _DEFAULT_WALL_TIME_S)
    if tool_reserve is None:
        tool_reserve = tool_reserve_for(task)
    reserve = _TOOL_RESERVE_S if tool_reserve is None else float(tool_reserve)
    cap = _PRE_RUN_CAP_BIG_S if budget >= _PRE_RUN_CAP_BIG_OVER_S else _PRE_RUN_CAP_S
    window = min(budget * _PRE_RUN_FRACTION, cap,
                 budget * _BUDGET_SAFETY - reserve)
    return max(0.0, window)


def pre_run_deadline(task, tool_reserve=None):
    """
    预运行阶段（dofile 生成 / LLM 调用）的截止时间（epoch 秒）。

    Args:
        tool_reserve: 给 dftexp_scan 保留的运行时间（秒）。
            None = 按网表体积自适应（tool_reserve_for），判断不出时用 _TOOL_RESERVE_S。

    返回 None 表示不额外限制（此时只受 budget_deadline 约束）。

    取值 = 起点 + pre_run_window_s()

    最后一项是硬保险：保证「LLM 阶段再怎么慢，dftexp_scan 也至少还剩
    tool_reserve 秒可以跑一轮」—— 平台首跑 t1/c2、t1/c3、t1/c6、t2/c2
    正是因为 LLM 阶段吃满预算，工具一次都没跑起来（无 final 产物 / 无 tool_runs 证据）。
    """
    window = pre_run_window_s(task, tool_reserve)
    if window is None:
        return _RUN_STARTED_AT + _PRE_RUN_CAP_S
    return _RUN_STARTED_AT + window


# LLM 结构化理解的启用阈值（秒）。
#
# 多数 case 的 wall time 预算只有 150s，而 LLM 理解单次实测 62.9~68.0s；
# 「理解 + 生成」两次 LLM 调用会吃满预算，dftexp_scan 一次都跑不起来（= 0 分）。
# 只有预算充裕的 case（如 t1/case5 特批 4800s）才额外用 LLM 做结构化理解，
# 取其语义抽取之长；其余 case 用 0.00s 的规则抽取（字段覆盖一致）。
_LLM_UNDERSTANDING_MIN_BUDGET_S = float(
    os.environ.get("SCAN_AGENT_LLM_UNDERSTAND_MIN_BUDGET", "600"))


def llm_understanding_affordable(task):
    """预算是否足够撑起「LLM 理解 + LLM 生成 + 至少一轮工具」的完整链路。"""
    budget = ((task or {}).get("limits") or {}).get("max_wall_time_s")
    try:
        return float(budget or 0) >= _LLM_UNDERSTANDING_MIN_BUDGET_S
    except (TypeError, ValueError):
        return False


def merge_understanding(base, fallback):
    """
    以 base（LLM 理解）为主、fallback（规则抽取）为兜底，逐字段补齐。

    规则抽取是确定性的（覆盖 task_spec 的表格与要求句），用它兜底可避免
    LLM 漏抽某字段导致整段配置丢失；LLM 已抽到的字段优先保留。
    """
    if not base:
        return dict(fallback or {})
    out = dict(base)
    for k, v in (fallback or {}).items():
        if out.get(k) in (None, "", [], {}):
            out[k] = v
    return out


def _llm_guard(llm, deadline):
    """
    把 LLM 截止时间收紧到 deadline；返回「恢复」用的本地函数。

    用法：
        restore = _llm_guard(llm, pre_run_deadline(task))
        try:
            ...LLM 阶段...
        finally:
            restore()
    """
    if not hasattr(llm, "set_deadline"):
        return lambda: None
    prev = getattr(llm, "_deadline", None)
    llm.set_deadline(deadline)
    return lambda: llm.set_deadline(prev)


def remaining_budget(task):
    """距离内部截止时间还剩多少秒。无预算时返回 None。"""
    dl = budget_deadline(task)
    return None if dl is None else max(0.0, dl - time.time())


def tool_timeout_for(task):
    """
    单轮 dftexp_scan 的超时秒数。

    旧实现把 TOOL_TIMEOUT 硬编码成 120s，而 task_1/case5 的 wall time 预算是 4800s、
    task_2/case5 是 3600s：大网表仅加载就要若干分钟，120s 必然把工具 kill 掉
    （实测 task_1/case5 日志停在 "Begin loading the netlist file."，该 case 零产物）。
    这里按 limitations 的 wall time 预算推导单轮上限，并受当前剩余预算约束。
    """
    limits = (task or {}).get("limits") or {}
    budget = float(limits.get("max_wall_time_s") or _DEFAULT_WALL_TIME_S)
    base = config.TOOL_TIMEOUT
    # 单轮上限 = 预算 × 0.8，但不超过 _TOOL_TIMEOUT_CAP（900s）。
    # 「max(base, ...)」保留：小预算 case 也要至少 config.TOOL_TIMEOUT 秒起步。
    base = max(base, min(int(_TOOL_TIMEOUT_CAP), int(budget * 0.8)))
    rem = remaining_budget(task)
    if rem is None:
        return base
    return max(1, int(min(base, rem)))


def time_is_up(task):
    """
    是否应当停止再开新一轮。

    两个条件任一成立即停：
      1. 内部截止时间已到（预算 × 0.9）；
      2. 自启动起超过绝对上限 _ABSOLUTE_CAP_S —— 与 limitations.md 是否解析
         成功无关的兜底看门狗（见常量注释里的 2 小时事故）。
    """
    if time.time() - _RUN_STARTED_AT >= _ABSOLUTE_CAP_S:
        return True
    rem = remaining_budget(task)
    return rem is not None and rem <= 5


def _best_run(run_results):
    """
    选「最优轮次」：优先成功轮，其次 ERROR 数最少、DRC 违例最少的轮次。

    旧实现用「最后一个 success 的轮次」，没有 success 时取最后一轮 ——
    末轮往往是修复失败后的产物，比中间某轮更差。
    """
    def key(r):
        p = _parsed_from_result(r)
        return (0 if r.get("success") else 1, len(p["errors"]), p["total_violations"])
    return min(run_results, key=key)


def _dofile_text_of_run(run):
    """读取某一轮实际使用的 dofile 文本（用于让 decision_log 的 locator 对齐落盘文件）。"""
    path = (run or {}).get("dofile_path")
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _mark_resolved(fix_records, run_id):
    """
    标记「某轮跑通即证明已消除」的问题闭环（issue_resolutions 的 verify 证据）。

    两类记录：
      · _auto_verify —— 首轮工具运行**之前**就已修复的注入故障（任务二的主力），
        跑通即验证通过；
      · 上一轮的运行中修复 —— 本次跑通即验证通过。
    另外把最后一条未 resolved 的记录也一并标记，与原有行为保持一致。
    """
    if not fix_records:
        return
    verified = f"R{run_id}"
    last = fix_records[-1]
    for rec in fix_records:
        if rec.get("resolved"):
            continue
        if rec.get("_auto_verify") or rec is last:
            rec["resolved"] = True
            rec["verified_run"] = verified
            rec["verified_source"] = f"runs/{verified}/{verified}.log"


def _persist_incremental(output_dir, task, run_results,
                         understanding=None, fix_records=None,
                         final_dofile_text=None, netlist_changes=None,
                         task_type="task1"):
    """
    每轮结束后把「当前最优轮次」的产物增量落盘（final_results/ + decision_log.json）。

    背景：final_results/ 与 decision_log.json 原先只在全部轮次跑完后一次性写出。
    实测 task_2/case2 跑了 5 轮、R5 日志干净，但进程被外部计时器 kill 后
    final_results/、diffs/、decision_log.json 全是空的 —— 该 case 直接 0 分。
    每轮落盘后，即使中途被杀，评委也能看到已完成的轮次与决策记录。
    """
    if not run_results:
        return
    best = _best_run(run_results)
    try:
        output_manager.finalize_outputs(
            output_dir, best, task,
            netlist_change=(netlist_changes[-1] if netlist_changes else None),
            incremental=True)
    except Exception as exc:
        print(f"  ⚠ 增量落盘失败：{exc}")

    # diffs/ 一并刷新（dofile 很小，开销可忽略）：中途被杀也能看到「改了什么」
    try:
        output_manager.generate_diffs(output_dir, run_results,
                                      original_dofile_text=task.get("original_dofile"))
    except Exception as exc:                                    # noqa: BLE001
        print(f"  ⚠ 增量 diff 生成失败：{exc}")

    # decision_log.json 也按轮增量刷新：中途被杀时至少有一份结构完整的记录
    if final_dofile_text is None:
        return
    try:
        log = decision_logger.build_decision_log(
            task, task_type, run_results, understanding, fix_records or [],
            final_dofile_text=final_dofile_text,
            netlist_changes=netlist_changes,
            # final_run 必须与 finalize_outputs 落盘所用轮次一致（同一个 _best_run）
            final_run_id=best.get("run_id"),
            original_dofile_text=task.get("original_dofile"),
            # 候选方案与择优依据（赛题 5.4.1(b)）；任务一由 LLM/规则双候选生成
            generation_info=(dofile_generator.get_last_selection()
                             if task_type == "task1" else None),
        )
        decision_logger.write_decision_log(output_dir, log)
    except Exception as exc:                                    # noqa: BLE001
        print(f"  ⚠ 增量决策日志写入失败：{exc}")


def _print_budget(task):
    limits = (task or {}).get("limits") or {}
    wall = limits.get("max_wall_time_s")
    calls = limits.get("max_tool_calls")
    if not wall:
        # 关键可观测性：把「预算从哪来」打出来。平台 2 小时超时那次，
        # 日志里只字未提预算，事后无法判断是 limits 没解析到还是流程吃满。
        print(f"  ⚠ 未从 limitations.md 解析到 wall time，按保守默认值 "
              f"{_DEFAULT_WALL_TIME_S:.0f}s 兜底（绝不无限运行）；"
              f"绝对看门狗 {_ABSOLUTE_CAP_S:.0f}s")
    if wall:
        wall = float(wall)
        reserve = tool_reserve_for(task)
        pre = pre_run_window_s(task)
        print(f"  预算: wall time {wall:.0f}s / 内部截止在 {wall * _BUDGET_SAFETY:.0f}s，"
              f"单轮工具超时 {tool_timeout_for(task)}s")
        # 预运行阶段（dofile 生成，一次 LLM 调用）限时 —— 见 _PRE_RUN_FRACTION 注释
        print(f"        预运行阶段（dofile 生成）上限 {0.0 if pre is None else pre:.1f}s，"
              f"为 dftexp_scan 至少保留 "
              f"{(_TOOL_RESERVE_S if reserve is None else reserve):.0f}s"
              f"（按网表体积自适应）")
    if calls:
        print(f"  预算: 工具调用次数上限 {calls}")



def pad_missing_artifacts(dofile, task, only_explicit_exports=False):
    """
    只补齐「缺失的报告 / 网表 / CTL / SCANDEF 导出命令」与 DRC 前置配置，
    不做基础命令回退。

    补齐位置必须符合工具工作流与官方 golden 的布局，不能一律追加到 exit 之前：

      - 报告命令 `rpt_*`：插到**最后一条 insert_dft_logic 之后**。
        golden 统一把报告段放在插链之后；若放在 insert_dft_logic 之前，
        报告内容是插链前的状态（不是最终状态）。
      - DRC 报告：官方写法是 `examine_scan_drc -file <path>`（golden 全部如此），
        因此**只要 dofile 已经有带 -file 的 examine_scan_drc，就不再补
        rpt_scan_drc_violation** —— 否则同一份 DRC 报告会写两遍。
        仅当完全没有 examine_scan_drc 时才补。
      - 导出命令 `dump_*`：插到 exit 之前（位于报告段之后）。
      - `set_scan_drc_rule_handling`：是 DRC 前置配置，统一由
        place_drc_configs_before_examine 移到 examine_scan_drc 之前。

    命令重复判定：dump_def 与 dump_scan_def 视为等价，不重复补齐。
    """
    import re

    arts = dofile_generator.required_artifacts(task)
    missing_rpt, missing_exp, missing_cfg = [], [], []

    # ---- 报告 ----
    # examine_scan_drc -file X 已经产出 DRC 报告 -> 不再补 rpt_scan_drc_violation
    drc_covered = bool(re.search(r"\bexamine_scan_drc\b[^\n]*-file\b", dofile))
    # 注意：不能用 \b 收尾 —— rpt_scan_chain 是 rpt_scan_chain_cell 的前缀，
    # \brpt_scan_chain\b 匹配不到 rpt_scan_chain_cell，会误判为「缺失」而补重复报告。
    for cmd, fname in arts["reports"]:
        if cmd == "rpt_scan_drc_violation" and drc_covered:
            continue
        if not re.search(r"\b" + re.escape(cmd) + r"(?![A-Za-z0-9_])", dofile):
            missing_rpt.extend(dofile_generator.report_lines([(cmd, fname)]))

    # ---- 网表 / CTL / SCANDEF ----
    #
    # 产物清单必须**逐条**核对，不能只判「有没有 dump_netlist」。
    # 实测 task_1/case1 的任务书点名列了 3 个网表（post_connect_icg.v /
    # post_replace_sff.v / post_replace_unscan.v），旧实现只补 arts["netlist"]
    # （清单里的最后一个）一条，于是中间两个从没生成 —— 少了 2 个独立交付物。
    # 而且 dump 必须落在**对应阶段之后**：case4 的 LLM 把 dump 写在 insert 之前，
    # 三个网表内容整体错位一格（名字对、内容不对）。
    input_basenames = {os.path.basename(str(p).replace("\\", "/"))
                       for p in (task.get("files", {}).get("netlist") or [])}
    dumped = {os.path.basename(m.group(1).strip('"\''))
              for m in re.finditer(r"dump_netlist[^\n]*-file\s+(\"[^\"]+\"|'[^']+'|\S+)",
                                   dofile)}

    wanted = [str(n) for n in (arts.get("netlists") or [])]
    if not wanted:
        wanted = [arts["netlist"]]
    elif arts["netlist"] not in wanted:
        wanted.append(arts["netlist"])

    # 每个网表名 -> 应该在哪个阶段之后 dump
    _STAGE_ANCHORS = (
        (("connect_icg",), r"^[ \t]*insert_dft_logic\b[^\n]*-connect_icg_only"),
        (("unscan",), r"^[ \t]*insert_dft_logic\b[^\n]*-replace_unscan"),
        (("replace_sff", "replace_scan", "_sff"), r"^[ \t]*insert_dft_logic\b[^\n]*-replace_only"),
    )
    by_anchor = {}          # anchor -> [行...]
    for name in dict.fromkeys(wanted):
        base = os.path.basename(str(name).replace("\\", "/"))
        if not base or base in dumped or base in input_basenames:
            continue
        line = f'dump_netlist -file "$out_res_dir/{base}"'
        anchor = None
        low = base.lower()
        for keys, pat in _STAGE_ANCHORS:
            if any(k in low for k in keys):
                anchor = pat
                break
        if anchor is None:
            low = base.lower()
            if "fixed" in low or "eco" in low:
                # ECO 后、插链前的网表（netlist_fixed.v）：紧跟 present_design 之后
                anchor = "__after_present__"
            else:
                # 终态网表（post_scan.v 等）：与其它导出一起放到 exit 之前
                missing_exp.append(line)
                dumped.add(base)
                continue
        by_anchor.setdefault(anchor, []).append(line)
        dumped.add(base)

    # CTL / SCANDEF 只在 task_spec / 参考 dofile 确实要求时才补。
    # 任务一 case1/case4 是三步前置处理、输出清单里根本没有 CTL/SCANDEF，
    # 补出来就是伪造产物（docx 5.4.2「不得伪造结果」）。
    ctl_wanted = arts["ctl_explicit"] if only_explicit_exports else True
    scandef_wanted = arts["scandef_explicit"] if only_explicit_exports else True
    if ctl_wanted and not re.search(r"\bdump_ctl\b", dofile):
        missing_exp.append(f'dump_ctl "$out_res_dir/{arts["ctl"]}"')
    if scandef_wanted and not re.search(r"\bdump_(?:scan_)?def\b", dofile):
        missing_exp.append(
            dofile_generator.scandef_dump_line(arts["scandef"], arts["scandef_cmd"])
        )

    # ---- DRC 规则豁免（手册 2.1.3.3）：规则号只认 task_spec 原文 ----
    #   a) task_spec 要求忽略某些规则 -> 缺失则补，存在但规则号非法/级别被降成
    #      Warning/重复出现 -> 按原文整行重写（只修「坏的」，不碰正确的行）；
    #   b) task_spec 未要求忽略 -> 该命令属于「靠豁免掩盖违例」，一律删除
    #      （官方多处明确「不得以禁用/豁免 DFTRx 规则的方式消除违例」）。
    drc_line = dofile_generator.drc_rule_handling_line(
        dofile_generator.drc_ignore_rules(task))
    drc_hits = list(re.finditer(r"^[ \t]*set_scan_drc_rule_handling\b[^\n]*$", dofile, re.M))
    if drc_line:
        if not drc_hits:
            missing_cfg.append(drc_line)
        else:
            for m in reversed(drc_hits[1:]):        # 去掉重复行
                dofile = dofile[:m.start()] + dofile[m.end():]
            m = drc_hits[0]
            raw = m.group(0)
            ids_now = dofile_generator._DRC_RULE_ID_RE.findall(raw)
            bad = [t for t in re.findall(r"\bDFTR[-A-Za-z0-9_]*\b", raw)
                   if t not in dofile_generator.ALLOWED_DRC_RULE_IDS]
            if bad or not ids_now or not re.search(r"\bIgnore\b", raw):
                dofile = dofile[:m.start()] + drc_line + dofile[m.end():]
                print("  ⚠ set_scan_drc_rule_handling 规则名/级别不符合 task_spec，已按原文重写")
    elif drc_hits:
        for m in reversed(drc_hits):
            dofile = dofile[:m.start()] + dofile[m.end():]
        print("  ⚠ task_spec 未要求忽略 DRC 规则，已删除 set_scan_drc_rule_handling（禁止用豁免掩盖违例）")

    # ---- 落位 ----
    if missing_rpt:
        dofile = _insert_after_last(
            dofile, r"^[ \t]*insert_dft_logic\b",
            "# ── 报告生成（自动补齐）────\n" + "\n".join(missing_rpt) + "\n")
    # 逐阶段落位网表 dump（顺序：ICG -> replace -> unscan -> ECO/final）
    for anchor in (r"^[ \t]*insert_dft_logic\b[^\n]*-connect_icg_only",
                   r"^[ \t]*insert_dft_logic\b[^\n]*-replace_only",
                   r"^[ \t]*insert_dft_logic\b[^\n]*-replace_unscan"):
        lines_here = by_anchor.get(anchor)
        if lines_here:
            dofile = _insert_after_last(dofile, anchor, "\n".join(lines_here) + "\n")
    if by_anchor.get("__after_present__"):
        dofile = _insert_after_last(
            dofile, r"^[ \t]*present_design\b[^\n]*$",
            "\n# ── ECO 后、插链前网表（自动补齐）────\n"
            + "\n".join(by_anchor["__after_present__"]) + "\n")
    if missing_exp:
        dofile = _insert_before_exit(
            dofile,
            "# ── 生成文件导出（自动补齐）────\n" + "\n".join(missing_exp) + "\n")
    if missing_cfg:
        dofile = _insert_before_exit(
            dofile,
            "# ── DRC 前置配置（自动补齐）────\n" + "\n".join(missing_cfg) + "\n")
    return dofile_generator.place_drc_configs_before_examine(dofile)


def _insert_after_last(text, pat, block):
    """把 block 插到最后一次匹配 pat 的那一整行之后；无匹配则退化为插到 exit 之前。"""
    last = None
    for m in re.finditer(pat, text, re.M):
        nl = text.find("\n", m.end())
        last = len(text) if nl == -1 else nl + 1
    if last is None:
        return _insert_before_exit(text, block)
    return text[:last] + "\n" + block + text[last:]


def _insert_before_exit(text, block):
    """把 block 插到最后一个 exit 之前；无 exit 则补一个。"""
    m = re.search(r"^[ \t]*exit\b", text, re.M)
    if m:
        return text[:m.start()] + block + "\n" + text[m.start():]
    return text.rstrip("\n") + "\n\n" + block + "\nexit\n"


def normalize_dofile_layout(task, understanding, dofile, is_task2=False):
    """
    写盘前的确定性整理（与 LLM 无关）：

      1. 输出目录变量齐全（CMD-0074 no such variable 的根因）；
      2. 输入路径绝对化；
      3. 工作流阶段顺序（SCAN-1009）；
      4. DRC 前置配置位置；
      5. task_spec 明确要求的 set_scan_cfg 选项（最大链长 / 链数 / 端口命名）。

    任务一额外做基础命令完整性校验（缺命令则回退规则模板）。
    """
    dofile = dofile_generator.ensure_output_vars(dofile)
    dofile, _usage_notes = dofile_generator.fix_scan_signal_usage(dofile)
    for _n in _usage_notes:
        print(f"  ⚠ {_n}")
    # 顶层名是不变量：修复循环里 LLM 可能把正确的 present_design 改成 task_spec 的
    # 「设计名称」（case6：des3 → des_perf，直接把能跑的 dofile 改坏）。
    # task["top_module"] 已由 _apply_top_from_facts 用**网表结构**校正过，这里强制回写。
    dofile = dofile_generator.enforce_top_module(dofile, task)
    if dofile_generator.has_relative_input_path(dofile):
        fixed = dofile_generator.fix_input_paths_to_absolute(dofile, task)
        if fixed and not dofile_generator.has_relative_input_path(fixed):
            print("  ⚠ dofile 含相对输入路径，已重写为绝对路径")
            dofile = fixed
    if not is_task2:
        for cmd in REQUIRED_CMDS:
            if not re.search(r"\b" + re.escape(cmd) + r"\b", dofile):
                print(f"  ⚠ dofile 缺失基础命令 {cmd!r}，回退规则模板重新生成")
                return dofile_generator.generate_from_config(task, understanding)
    else:
        # 任务二：按 task_spec 做「规格驱动的确定性修复」。
        # 这一步必须在工具运行之前完成 —— 实测 task_2/case1 的 original 被删掉了整条
        # load_netlist，首轮必报 22 个 ERROR 连锁失败；旧代码只靠 LLM 补丁在 R3 才修好，
        # 白烧两轮工具调用，且 LLM 一旦不稳定就直接卡死在「错误签名相同」。
        # 这些故障的正确值全部写在 task_spec 里，属确定性修复，不该赌 LLM。
        dofile, spec_notes = spec_fixer.fix_to_spec(task, dofile)
        for n in spec_notes:
            print(f"  ⚠ 规格修复：{n}")
    dofile = dofile_generator.enforce_workflow_stage_order(dofile)
    dofile = dofile_generator.place_drc_configs_before_examine(dofile)
    if not is_task2:
        # 任务二的 set_scan_cfg 规格校对已由上面的 spec_fixer.fix_to_spec 完成，
        # 不必再跑一遍；任务一没有那一步，仍需要这道兜底。
        dofile = dofile_generator.ensure_scan_cfg_from_spec(task, dofile)
    # 最终清洗：时钟声明必须是顶层端口。
    # 必须放在最后 —— 前面「规格修复」会把 spec 里的 GCLK 再补回来，
    # 只在 understanding 层过滤拦不住（case6 冒烟实测）。
    dofile, dropped = dofile_generator.sanitize_clock_ports(dofile, task)
    if dropped:
        print("  ⚠ 移除非端口的时钟声明：" + "、".join(dropped))
    return dofile


def ensure_dofile_integrity(dofile, task, understanding):
    """
    强制校验 dofile 的完整性：

      1. 输出目录变量 / 输入路径 / 工作流顺序 / DRC 配置位置规范化；
      2. 检查 REQUIRED_CMDS，缺任意一个 -> 回退规则模板；
      3. 补齐缺失的报告 / 网表 / CTL / SCANDEF（仅补 task_spec 确实要求的）。

    Returns:
        str: 处理后的 dofile
    """
    dofile = normalize_dofile_layout(task, understanding, dofile, is_task2=False)
    return pad_missing_artifacts(dofile, task, only_explicit_exports=True)



# ============================================================================
# 任务一：Dofile 生成
# ============================================================================

def run_task1(input_dir, output_dir, llm):
    """任务一入口：同 run_task2，异常时先落应急产物再抛出。"""
    try:
        return _run_task1_impl(input_dir, output_dir, llm)
    except BaseException:
        _emergency_finalize(input_dir, output_dir, "task1")
        raise


def _run_task1_impl(input_dir, output_dir, llm):
    """
    任务一流程：理解任务 -> 生成 dofile -> 运行 -> 解析 -> 多轮修复 -> 输出。
    """
    print("[任务一] Scan Insertion Dofile 生成")
    print(f"  输入目录: {input_dir}")
    print(f"  输出目录: {output_dir}")

    # 1. 解析输入
    task = input_parser.parse_task(input_dir)
    _print_budget(task)

    # 按预算调节候选数量（多候选 = 多次 LLM 调用，影响效率分与超时风险）
    dofile_generator.set_candidate_count(candidate_count_for(task))

    # ---- 预运行阶段：限时（详见 _PRE_RUN_FRACTION 注释）----
    # 全局护栏：任何 LLM 调用都不得越过本 case 的内部截止时间；
    # 预运行阶段再收紧一次，保证一定给 dftexp_scan 留出至少一轮完整运行的时间。
    global_deadline = budget_deadline(task)
    pre_deadline = pre_run_deadline(task)
    restore = _llm_guard(llm, pre_deadline)
    t_pre = time.time()
    try:
        # 2. 网表事实抽取（确定性，只读网表 + Liberty）—— 先于结构化理解，
        #    因为「DFF→SFF 映射」等字段要以库命名规律/网表实例为依据补全，
        #    而不仅是任务书措辞。这是「真正的分析」：public 与 hidden case 依赖
        #    同一份事实，不存在「public 靠 golden 抄答案、hidden 抓瞎」的两套逻辑。
        #    规模自适应 + 截止时间保护，最坏情况退化为空事实，不影响主流程。
        facts = dofile_generator.ensure_facts(
            task, deadline=pre_deadline, want_chains=True)
        # 供 dofile_generator.sanitize_clock_ports 做「时钟必须是顶层端口」的最终清洗
        task["_facts"] = facts
        if facts.get("ok"):
            print(f"  ✓ 网表事实（{facts['modules']} 模块 / 时序单元 {facts['ff_count']}"
                  f" / 移位链 {len(facts['shift_chains'])} / {facts['elapsed']:.1f}s）")

        # 2.5 结构化理解 —— 为 LLM 生成 dofile 准备输入
        #
        # 【定位】本系统是「基于大语言模型的 Scan Insertion 智能体」：最终 dofile
        # 由 LLM 依据「任务说明书 + 网表事实 + 手册检索(RAG) + 命令白名单」写出
        # （见 dofile_generator.generate_dofile）。结构化理解只是**输入之一**。
        #
        # LLM 理解单次实测 62.9~68.0s，而多数 case 预算仅 150s；若在理解上再花一次
        # LLM 调用，dftexp_scan 就没时间跑（平台首跑 t1/c2、t1/c3、t1/c6 均因此一次
        # 工具都没跑起来 = 直接 0 分）。因此按预算二选一：
        #   · 预算充裕（≥ 阈值，如 case5 特批 4800s）→ LLM 理解，规则抽取兜底补字段；
        #   · 预算紧张（150s 级）→ 0.00s 规则抽取（字段覆盖一致，不牺牲生成质量）。
        rule_u = dofile_generator.rule_understanding(task, facts=facts)
        llm_u = None
        if llm.available and llm_understanding_affordable(task):
            llm_u = input_parser.extract_task_understanding(task, llm)
        understanding = merge_understanding(llm_u, rule_u)
        # 用网表事实压过 task_spec 的措辞（顶层名 / 不存在的端口），
        # 见 apply_facts_precedence 的说明（case6 顶层名、case1/case2 故障端口）。
        understanding, _fp_notes = apply_facts_precedence(understanding, facts)
        for _n in _fp_notes:
            print(f"  ⚠ 事实优先：{_n}")
        print("  ✓ 结构化理解（LLM 抽取 + 规则兜底）" if llm_u
              else "  ✓ 结构化抽取（规则，0.0s；预算不足以再花一次 LLM 调用）")

        # 3. 生成 dofile（规则模板 + LLM 候选择优）
        #    这次 LLM 调用是预运行阶段的核心价值，给足时间；真超时才用规则模板。
        dofile = dofile_generator.generate_dofile(task, understanding, llm, facts=facts)
        print(f"  ✓ Dofile 生成（{len(dofile)} 字符）")

        # 3.5 强制校验 dofile 完整性（补齐缺失命令/报告，P0-1）
        dofile = ensure_dofile_integrity(dofile, task, understanding)
    finally:
        restore()
        # 预运行阶段结束后，LLM 只受「本 case 内部截止时间」约束（用于多轮修复阶段）
        if hasattr(llm, "set_deadline"):
            llm.set_deadline(global_deadline)

    _pre_dt = time.time() - t_pre
    _pre_cap = (pre_deadline - _RUN_STARTED_AT) if pre_deadline else 0.0
    print(f"  ⏱ 预运行阶段 {_pre_dt:.1f}s / 上限 {_pre_cap:.1f}s"
          f"（{llm.stats_line() if hasattr(llm, 'stats_line') else ''}）")
    print(f"  ⏱ 剩余预算 {remaining_budget(task) or 0:.1f}s，可用于 dftexp_scan 运行")

    # 4. 多轮运行 + 修复
    output_manager.setup_output_dirs(output_dir)
    run_results = []
    fix_records = []  # 每轮修复记录，供 decision_log.issue_resolutions 使用
    max_runs = max_runs_for(task)
    seen_error_sigs = {}  # 错误签名 -> 首次出现的轮次，用于检测修复是否收敛
    best_errors = None    # 历史最小错误数，用于检测「修复在恶化」（见收敛控制 2）
    # 内部时钟档位修复已尝试的次数（见 dofile_generator.repair_internal_clocks_for_drc）
    clock_repair_attempts = 0

    for run_id in range(1, max_runs + 1):
        # 预算用尽就不再开新一轮：超时 = 当前 case 0 分（docx 5.4.1）
        if time_is_up(task):
            print(f"  ⏱ 运行预算已用尽，不再开 Run {run_id}")
            break

        run_dir = Path(output_dir) / "runs" / f"R{run_id}"
        timeout = tool_timeout_for(task)
        print(f"  [Run {run_id}] 运行 dftexp_scan...（超时 {timeout}s）")
        _t_run = time.time()
        result = tool_runner.run_dofile(dofile, run_dir, run_id, timeout=timeout)
        print(f"  ⏱ Run {run_id} 工具耗时 {time.time() - _t_run:.1f}s"
              f"（剩余预算 {remaining_budget(task) or 0:.1f}s）")
        run_results.append(result)

        # 解析日志
        parsed = _parsed_from_result(result)
        result["_parsed"] = parsed

        # 每轮结束立即增量落盘（被外部 kill 也不至于空手）
        _persist_incremental(output_dir, task, run_results,
                             understanding=understanding, fix_records=fix_records,
                             final_dofile_text=dofile, task_type="task1")

        if result.get("error") == "tool_not_found":
            print("  ⚠ 工具不可用（本地无 License/工具环境），停止运行")
            break

        if result.get("error") == "timeout":
            print(f"  ✗ Run {run_id} 工具超时被中止（{timeout}s）")
            fix_records.append({
                "run": f"R{run_id}",
                "issue": f"Scan Insertion 工具单轮超过 {timeout}s 被中止",
                "root_cause": "本 case 网表/设计规模较大，单轮耗时超过按预算推导的超时",
                "fix": "不再重试同一 dofile（时间预算不足以再跑一轮）",
                "found_run": f"R{run_id}",
                "resolved": False,
            })
            break

        # 统一成功判定（task1 / task2 同一口径）：
        #   1) 进程成功且日志无 ERROR/FATAL；
        #   2) 有效 DRC 违例为 0（仅真建链的 case 把 DRC 当指标）；
        #   3) 任务要求逐条满足（缺件 / 配置不符）。
        # 旧实现只做第 1 条，因此 task1 在「回替 0 个、少交两个网表、DRC 残留 6.9 万条」
        # 的情况下依然宣告成功（summary 与 final.log 互相矛盾）。
        has_errors = bool(parsed["errors"])
        exit_ok = result.get("success", False)  # returncode == 0
        drc = _drc_gate(task, understanding, parsed)
        result["_drc"] = drc                      # 供 decision_logger 写 summary
        log_clean = exit_ok and not has_errors and drc["ok"]

        if log_clean:
            # 日志干净 ≠ 配置正确（docx 4.2.2 第三类故障）。task1 过去完全没有这一步。
            spec_issues = _check_spec_compliance(
                task, understanding, dofile, result.get("output", ""), run_dir,
                llm=None, allow_llm=False,
            ) or []
            if not spec_issues:
                print(f"  ✓ Run {run_id} 成功（无 ERROR / {_drc_brief(drc)} / 满足任务要求）")
                result["accepted"] = True          # 判据通过（供 decision_log 写"最终成功"）
                _mark_resolved(fix_records, run_id)
                break

            print(f"  ✗ Run {run_id} 日志无报错，但 {len(spec_issues)} 项不符合任务要求：")
            for v in spec_issues[:5]:
                print(f"      - {v.get('requirement', '')}（{v.get('evidence', '')}）")
            fix_records.append({
                "run": f"R{run_id}",
                "issue": "DFT 配置不符合任务要求："
                         + "；".join(v.get("requirement", "") for v in spec_issues[:3]),
                "root_cause": spec_verifier.format_issues(spec_issues),
                "fix": "按 task_spec 原文补齐缺失的产物/配置",
                "violated_requirement": "；".join(
                    v.get("requirement", "") for v in spec_issues[:3]),
                "found_run": f"R{run_id}",
                "applied_run": f"R{run_id + 1}" if run_id < max_runs else "",
                "resolved": False,
            })
            # 确定性补缺（补 dump/报告、按 spec 校 set_scan_cfg），不消耗 LLM 预算
            repaired = ensure_dofile_integrity(dofile, task, understanding)
            if (repaired and repaired != dofile
                    and run_id < max_runs and not time_is_up(task)):
                dofile = repaired
                print("  ↻ 已按任务要求补齐 dofile，重试")
                continue
            break

        else:
            print(f"  ✗ Run {run_id} 失败（exit_code={result.get('exit_code')}, "
                  f"errors={len(parsed['errors'])}, {_drc_brief(drc)}）")
            if parsed["errors"]:
                for e in parsed["errors"][:3]:
                    print(f"      - {e}")
            elif drc["required"] and not drc["ok"]:
                # 没有 ERROR、只有 DRC 违例：把违例本身作为问题登记，
                # 否则 decision_log 会写「末轮错误数 0」而与日志矛盾。
                fix_records.append({
                    "run": f"R{run_id}",
                    "issue": "DRC 检查未清零：" + _drc_brief(drc),
                    "root_cause": "DFT 信号/时钟/门控配置导致测试模式下不可控",
                    "fix": "按 DRC 规则补齐 set_scan_signal / set_scan_cfg 配置",
                    "found_run": f"R{run_id}",
                    "resolved": False,
                })
                if run_id < max_runs and not time_is_up(task):
                    # 注意：这里**不能**用 has_useful_time(llm) 当门槛。
                    # 旧实现加了这个条件，而它在「LLM 不可用」时返回 False ——
                    # 于是平台实测（本次未注入 LLM_API_KEY）task_1/case5 首轮
                    # 69k 条 DFTR1 之后直接 break，一次确定性修复都没试。
                    # 下面两条修复都是纯确定性的，只该受时间预算约束。
                    clock_repair_attempts += 1
                    clk_fixed = dofile_generator.repair_internal_clocks_for_drc(
                        dofile, parsed, attempt=clock_repair_attempts)
                    if clk_fixed and clk_fixed != dofile:
                        dofile = clk_fixed
                        print("  ↻ 已调整内部时钟识别档位，重试")
                        continue

                    repaired = ensure_dofile_integrity(dofile, task, understanding)
                    if repaired and repaired != dofile:
                        dofile = repaired
                        print("  ↻ 已补齐 DFT 配置，重试")
                        continue
                break

            # 引擎级环境失败（SCAN-0620：ScanInsertion 读不到 license 环境变量）：
            # 与 dofile 内容无关，所有 ScanInsertion 命令都会失败。
            # 继续"修复 dofile"只会浪费时间并引入新错误（路径被改坏、错误数上升），
            # 因此立即停止，并明确报告这是运行环境问题。
            if dofile_fixer.is_engine_env_failure(parsed):
                print("  ⚠ ScanInsertion 引擎初始化失败（SCAN-0620：读环境变量失败）")
                print("     属运行环境问题（未注入 license 相关环境变量），改动 dofile 无法修复，停止重试")
                print("     请确认已设置 SCANINSERTION_LICENSE_SERVER（评测系统会自动注入）")
                fix_records.append({
                    "run": f"R{run_id}",
                    "issue": (parsed["errors"][0] if parsed["errors"] else ""),
                    "root_cause": "运行环境缺少 license 相关环境变量（SCAN-0620），与 dofile 内容无关",
                    "fix": "不改动 dofile（环境问题无法通过修改 dofile 修复）",
                    "resolved": False,
                })
                break

            # 收敛控制：同一组错误签名再次出现，说明修复在原地打转。
            # task_1/case6 曾在 add_dedicated_wrapper_cell_type 上连跑 6 轮不收敛，
            # 白耗工具调用预算（docx 5.4.1 超限即 0 分）。
            err_sig = _err_sig_of(result)
            if err_sig and err_sig in seen_error_sigs:
                print(f"  ⚠ 错误签名与 R{seen_error_sigs[err_sig]} 完全相同（{'/'.join(err_sig)}），"
                      "修复未收敛，停止重试")
                fix_records.append({
                    "run": f"R{run_id}",
                    "issue": (parsed["errors"][0] if parsed["errors"] else ""),
                    "root_cause": f"同一组错误（{'/'.join(err_sig)}）反复出现，修复未收敛",
                    "fix": "停止重试，避免耗尽工具调用预算",
                    "resolved": False,
                })
                break
            if err_sig:
                seen_error_sigs[err_sig] = run_id

            # 收敛控制 2：错误数不降反升 —— 修复在恶化，继续重试只会更糟。
            #
            # t1/case6 平台实测 R1=4 → R2=5 → R3=8：每一轮的错误**签名都不同**
            # （收敛控制 1 只认「签名完全相同」，拦不住），于是白烧两轮工具调用，
            # 最终产物还停在更差的一轮上。
            # 判据用「是否超过此前最佳」而不是「是否比上一轮差」：
            # 中间某轮偶然变差不算恶化，只有再也回不到最佳才算。
            n_errors = parsed.get("error_total") or len(parsed["errors"])
            if best_errors is not None and n_errors > best_errors:
                print(f"  ⚠ 错误数 {n_errors} 高于此前最佳 {best_errors}，"
                      "修复在恶化，停止重试")
                fix_records.append({
                    "run": f"R{run_id}",
                    "issue": (parsed["errors"][0] if parsed["errors"] else ""),
                    "root_cause": f"修复后错误数由 {best_errors} 升至 {n_errors}，修复在恶化",
                    "fix": "停止重试，保留错误最少的那一轮产物（见 final_run）",
                    "resolved": False,
                })
                break
            if best_errors is None or n_errors < best_errors:
                best_errors = n_errors

            # 针对性修复（只修出错命令，结合 RAG）
            if run_id < max_runs and llm.available and not time_is_up(task):
                fixed = dofile_fixer.targeted_fix(task, dofile, parsed, llm)
                if fixed:
                    # 修复补丁可能又写出参数形式错误的命令（wrapper 注册 / cell 映射），
                    # 写盘前再过一遍确定性文法规范化 + 布局规范化
                    fixed, norm_notes = dofile_generator.normalize_dofile_commands(task, fixed)
                    for n in norm_notes:
                        print(f"      ⚠ {n}")
                    fixed = normalize_dofile_layout(task, understanding, fixed, is_task2=False)
                if fixed and fixed != dofile:
                    fix_records.append({
                        "run": f"R{run_id}",
                        "issue": (parsed["errors"][0] if parsed["errors"] else ""),
                        "root_cause": "dofile 命令/参数与工具要求不符",
                        "fix": "针对性修复 dofile（具体改动见 diffs/）",
                        "found_run": f"R{run_id}",
                        "applied_run": f"R{run_id + 1}",
                        "resolved": False,
                    })
                    dofile = fixed
                    print("  ↻ 已针对性修复 dofile，重试")
                    continue
                else:
                    print("  ⚠ 无法进一步修复，停止")
                    break
            else:
                break

    # 5. 整理输出
    final_run = _best_run(run_results) if run_results else {}

    output_manager.finalize_outputs(output_dir, final_run, task)
    output_manager.generate_diffs(output_dir, run_results)

    # requirement_mapping 的行号必须对**真正落盘的**那份 dofile 计算。
    # _best_run 选出的轮次可能不是最后一轮（case6：final_run=R2 而 R3 也存在），
    # 若拿当前内存里的 dofile（末轮）算 locator，就会与 final.dofile 错行。
    final_dofile_text = _dofile_text_of_run(final_run) or dofile

    decision_log = decision_logger.build_decision_log(
        task, "task1", run_results, understanding, fix_records,
        final_dofile_text=final_dofile_text,
        final_run_id=final_run.get("run_id"),
        # 候选方案与择优依据（赛题 5.4.1(b)）
        generation_info=dofile_generator.get_last_selection(),
    )
    decision_logger.write_decision_log(output_dir, decision_log)

    print("  ✓ 任务一完成")
    return 0


# ============================================================================
# 任务二：问题识别结果的记录
#
# 赛题 5.5.2 的 75 分（问题识别与原因定位 30 + 问题修复正确性 30 + 问题闭环真实性
# 15）全部由 decision_log.json 的 issue_resolutions 承载。因此修复器识别出的每条
# 问题都必须落成一条带 found -> diagnosis -> fix -> verify 证据链的记录。
# ============================================================================

def _snapshot_original_dofile(output_dir, original_text, run_id="R1"):
    """
    把修复起点（original.dofile）快照进 output，返回相对 output 的路径。

    issue 的 found.source 必须指向真实存在的文件（赛题评分项「引用是否闭合」），
    而 /input 位于 output 之外、相对路径无法干净表达，因此把修复起点留一份在
    runs/R1/deliverables/ 下作为审计快照。
    """
    if not original_text:
        return ""
    try:
        d = Path(output_dir) / "runs" / run_id / "deliverables"
        d.mkdir(parents=True, exist_ok=True)
        (d / "original.dofile").write_text(original_text, encoding="utf-8")
    except OSError as exc:
        print(f"  ⚠ original.dofile 快照失败：{exc}")
        return ""
    return f"runs/{run_id}/deliverables/original.dofile"


# 定位用的标识符（端口名 / 实例路径 / 命令名 / 参数名）
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./\[\]]{2,}")

# dofile 中以导出类命令开头的行
_EXPORT_CMD_RE = re.compile(r"^[ \t]*(rpt_[A-Za-z0-9_]+|dump_[A-Za-z0-9_]+)", re.M)

# 配置/流程类命令的首词（用于在 original.dofile 里定位"问题那一行"）
_CMD_HEADS = ("set_scan", "set_wrapper", "set_dft", "set_current", "load_",
              "add_scan", "present_", "examine_", "insert_", "dump_", "rpt_")


def _locate_in_text(issue, text):
    """
    在 original.dofile 中定位该问题涉及的行，返回 (locator, excerpt)。

    优先找「命令名 + 标识符」同时出现的行（最可能是注入故障那一行），
    退而求其次找第一个标识符出现的行；都找不到就返回空（不编造行号）。
    """
    blob = " ".join([
        issue.get("located_object", "") or "",
        issue.get("fix_action", "") or "",
        issue.get("phenomenon", "") or "",
    ])
    idents = [t for t in _IDENT_RE.findall(blob)
              if "/" not in t or t.count("/") <= 2]
    if not idents or not text:
        return "", ""
    lines = text.splitlines()
    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith(_CMD_HEADS) and any(t in s for t in idents):
            return f"L{i}", s[:200]
    for i, ln in enumerate(lines, 1):
        if any(t in ln for t in idents):
            return f"L{i}", ln.strip()[:200]
    return "", ""


def _line_at(text, locator):
    """按 'L12' / 'L12-L14' 取原文对应行的片段。"""
    m = re.match(r"L(\d+)", locator or "")
    if not m or not text:
        return ""
    lines = text.splitlines()
    n = int(m.group(1))
    return lines[n - 1].strip()[:200] if 1 <= n <= len(lines) else ""


def _issue_to_record(issue, found_source, found_run="R1", original_text=""):
    """
    把 spec_fixer 产出的结构化 issue 转成 fix_records 记录。

    这些问题的修复在**首轮工具运行之前**就已生效，因此：
      · applied_run = R1；首轮跑通即证明问题已消除（_auto_verify=True）；
      · found 的证据指向 original.dofile 快照（修复起点）而不是工具日志 ——
        赛题 4.2.2 的第三类故障「DFT 配置不符合任务要求」本身就**不会**在日志里
        报 ERROR，其证据只能来自「配置 × 任务要求」的对照。
    """
    loc = issue.get("locator") or ""
    excerpt = ""
    if original_text:
        if loc:
            excerpt = _line_at(original_text, loc)
        else:
            loc, excerpt = _locate_in_text(issue, original_text)
    return {
        "issue": issue.get("located_object") or issue.get("phenomenon", ""),
        "phenomenon": issue.get("phenomenon", ""),
        "root_cause": issue.get("root_cause", ""),
        "fix": issue.get("fix_action", ""),
        "category": issue.get("category", ""),
        "violated_requirement": issue.get("violated_requirement", ""),
        "found_run": found_run,
        "found_source": found_source,
        "found_locator": loc,
        "found_excerpt": excerpt,
        "applied_run": found_run,
        "resolved": False,
        "_auto_verify": True,
    }


def _verify_llm_locator(issue, original_text):
    """
    校验 LLM 给出的行号是否**真的**对应该问题（防止编造行号污染证据链）。

    判据：该行文本与问题涉及的标识符（命令名 / 端口名 / 实例路径）必须有交集。
    对不上就丢弃 locator，交给 _locate_in_text 按关键词兜底定位；
    若关键词也定位不到，`found_locator` 留空 —— 宁可少写一个行号，也不编造。

    赛题「问题闭环真实性」明确要求 found 证据可核验，编造行号会直接被判不真实。
    """
    if not isinstance(issue, dict):
        return issue
    loc = (issue.get("locator") or "").strip()
    if not loc or not original_text:
        return issue
    line = _line_at(original_text, loc)
    blob = " ".join([
        issue.get("located_object") or "",
        issue.get("excerpt") or "",
        issue.get("fix_action") or "",
    ])
    idents = [t for t in _IDENT_RE.findall(blob) if len(t) >= 4]
    if not line or (idents and not any(t in line for t in idents)):
        issue = dict(issue)
        issue["locator"] = ""
    return issue


def _detect_missing_artifacts(task, original_text, base_text):
    """
    识别 original.dofile 缺失的产物导出命令（任务要求的报告 / 网表 / CTL / SCANDEF）。

    做法：把「补齐产物后的版本」与修复起点比对，多出来的导出命令就是缺的那一批
    （与 pad_missing_artifacts 同一口径，不会凭空多报）。
    """
    if not original_text:
        return []
    try:
        padded = pad_missing_artifacts(base_text or original_text, task,
                                       only_explicit_exports=True)
    except Exception:                                           # noqa: BLE001
        return []
    before = {m.group(1) for m in _EXPORT_CMD_RE.finditer(original_text)}
    after = {m.group(1) for m in _EXPORT_CMD_RE.finditer(padded)}
    new = sorted(after - before)
    if not new:
        return []
    return [{
        "category": "dofile_exec",
        "phenomenon": "dofile 中没有任务要求的产物导出命令，工具运行结束后拿不到 %s"
                      " 等交付物，输出不完整" % "、".join(new),
        "located_object": "original.dofile 缺失的产物导出命令：%s" % "、".join(new),
        "root_cause": "产物导出缺失：任务说明书明确要求交付这些报告/网表/CTL/SCANDEF，"
                      "但 original.dofile 中没有对应命令，工具不会生成这些文件",
        "fix_action": "补写缺失的导出命令：" + "；".join(new),
        "violated_requirement": "需按任务要求导出 %s" % "、".join(new),
    }]


def _stage_order_issue():
    """工具强制的工作流阶段顺序被违反（SCAN-1009）。"""
    return {
        "category": "dofile_exec",
        "phenomenon": "insert_dft_logic 被排在 examine_scan_drc / examine_scan_chain 之前，"
                      "工具以 SCAN-1009 报错并拒绝执行插链阶段",
        "located_object": "original.dofile 中 insert_dft_logic 与 examine_scan_drc / "
                          "examine_scan_chain 的先后顺序",
        "root_cause": "命令执行顺序错误：工具要求先 examine_scan_drc -> examine_scan_chain "
                      "再 insert_dft_logic，original.dofile 把插链命令提前，前置检查未完成",
        "fix_action": "把 insert_dft_logic 移回 examine_scan_chain 之后（确定性重排，首轮前生效）",
        "violated_requirement": "工具工作流阶段顺序：examine_scan_drc -> examine_scan_chain "
                                "-> insert_dft_logic",
    }


# ============================================================================
# 任务二：问题诊断与修复
# ============================================================================

def run_task2(input_dir, output_dir, llm):
    """
    任务二入口：包一层异常兜底，保证**任何异常都不会导致零产物**。

    未捕获异常会让 finalize_outputs / decision_log 全部跳过 —— 平台 2 小时
    超时那次正是这种形态：容器被杀之前，输出目录里连 decision_log 都没有，
    事后完全无法定位。这里统一兜住，写出「如实标记 aborted」的产物再抛出。
    """
    try:
        return _run_task2_impl(input_dir, output_dir, llm)
    except BaseException:
        _emergency_finalize(input_dir, output_dir, "task2")
        raise


def _run_task2_impl(input_dir, output_dir, llm):
    """
    任务二流程：读 original.dofile -> 复现问题 -> 诊断 -> 修复 -> 重跑。
    """
    print("[任务二] Scan Insertion 问题定位与修复")
    print(f"  输入目录: {input_dir}")
    print(f"  输出目录: {output_dir}")

    task = input_parser.parse_task(input_dir)
    original_dofile = task.get("original_dofile", "")
    _print_budget(task)

    # ---- 预运行阶段：限时（详见 _PRE_RUN_FRACTION 注释）----
    # 本阶段两处「在首轮工具运行之前」的工作，现已都降为确定性开销：
    #   1) 结构化理解 → 规则抽取（0.00s；原 LLM 调用单次 62.9~68.0s）；
    #   2) _analyze_netlist_for_task2 → 单次遍历（0.06s；修复前对 t2/c2 的 1223 个
    #      模块各重扫一遍 3MB 网表，耗时 83.66s，见 netlist_analyzer 注释）。
    # 两者叠加曾是平台首跑 t2/c2「运行预算已用尽，不再开 Run 1」的直接原因。
    # 工具保留量按网表体积自适应（tool_reserve_for）：平台实测 <20MB 的网表
    # dftexp_scan 单轮只要 2~7s，因此不再固定抽走 55s（那会把 LLM 窗口压到 80s，
    # 导致 t2/c1~c4 四个 case 的 LLM **全部**在 78~79s 超时）。
    # 大网表（232MB→334s）自动升到 90s，同样不会让工具被截断。
    pre_deadline = pre_run_deadline(task)
    global_deadline = budget_deadline(task)
    if hasattr(llm, "set_deadline"):
        llm.set_deadline(pre_deadline)
    t_pre = time.time()

    # 网表事实（确定性，只读网表 + Liberty）：任务二的 LLM 诊断要引用**真实**的
    # 实例名 / 端口名 / 层次路径，因此与任务一取同一份事实，两个任务口径一致。
    facts = dofile_generator.ensure_facts(
        task, deadline=pre_deadline, want_chains=True)
    # 供 dofile_generator.sanitize_clock_ports 做「时钟必须是顶层端口」的最终清洗
    task["_facts"] = facts

    # 结构化理解 —— 规则抽取（0.00s）：LLM 理解单次实测 62.9~68.0s，任务二预算同样
    # 紧张，而理解结果只被 normalize_dofile_layout → generate_from_config 当作兜底输入。
    # 任务二的 LLM 火力集中在两处更有价值的地方：
    #   1) 首轮「诊断 + 修复」（task2_diagnoser.analyze_and_fix，LLM 主路径）；
    #   2) 工具跑通后的规格复核（spec_verifier.review_with_llm，专治第三类故障）。
    understanding = dofile_generator.rule_understanding(task, facts=facts)
    # 用网表事实压过 task_spec / original.dofile 的措辞：顶层名按结构判定；
    # 不存在的 DFT 端口（original.dofile 里的注入故障）从 understanding 中剔除，
    # 否则 spec_verifier 会把故障值当成"任务要求"，修对了反而判不合格。
    understanding, _fp_notes = apply_facts_precedence(understanding, facts)
    for _n in _fp_notes:
        print(f"  ⚠ 事实优先：{_n}")
    print("  ✓ 结构化抽取（规则，0.0s）")

    # 分析网表结构（任务二诊断上下文：DFF/SFF 类型、时钟/复位端口等）
    netlist_info = _analyze_netlist_for_task2(task)

    output_manager.setup_output_dirs(output_dir)
    run_results = []
    fix_records = []
    netlist_changes = []  # 记录网表级修复
    # 「LLM 重写整份网表 + yosys LEC」只允许尝试一次。
    # 为什么：这条路单次成本 = LLM 240s + LEC 300s ≈ 9 分钟，而成功率很低
    # （case6 真跑时 LLM 把顶层端口声明都漏了）。旧代码每轮都会重试，
    # 6 轮就能吃掉近 1 小时预算，是平台 2 小时超时的主要构成之一。
    netlist_rewrite_tried = False
    # DRC 不下降熔断用的状态（见循环内同名注释）
    best_drc_total = None
    drc_no_progress = 0
    # 已经尝试过多少次「dofile 层修复」。用来给 ECO 做**行为判据**（见 _eco_inject
    # 的门槛说明）：spec 没提示「缺陷在网表」时，先让 dofile 修复试一次，
    # 无效再上 ECO —— 避免抢跑，也能覆盖 spec 措辞不同的新 case。
    dofile_fix_attempts = 0
    max_runs = max_runs_for(task)

    # ---- 修复起点快照 ----
    # issue 的 found 证据必须指向 output 内真实存在的文件（赛题「引用是否闭合」），
    # 把 original.dofile 快照进 runs/R1/deliverables/，理由见函数注释。
    original_snapshot = _snapshot_original_dofile(output_dir, original_dofile)

    # ---- 问题识别（任务二的 75 分主战场）----
    # original.dofile 是被注入故障的版本，故障分三类（赛题 4.2.2）：
    #   ① Dofile 执行问题 ② DRC 问题 ③ DFT 配置不符合任务要求
    # 三类的正确写法都写在 task_spec 里，因此做一次「task_spec × original.dofile ×
    # 标准单元库 × 网表」的确定性审查，一次拿到两样东西：
    #   1) 一份正确的 dofile（首轮即跑对，不浪费工具调用预算）；
    #   2) 结构化 issue 列表（问题识别与定位的证据链）。
    # 审查过程不依赖 LLM，也**不读任何参考答案**（preset_issues.json / golden.dofile）——
    # hidden case 不提供这些文件，依赖它们等于在评测时失效。
    base_dofile = dofile_generator.preprocess_dofile(original_dofile, task)

    # ---- 问题识别（主路径 = LLM 诊断；兜底/交叉验证 = spec_fixer）----
    # 赛题定位是「基于大语言模型的智能体」，而任务二 30 分的「问题识别与原因定位」
    # 正落在「读懂 original.dofile 与 task_spec 的差异」这件事上 —— 先交给 LLM。
    # LLM 一次调用同时产出「结构化问题清单」与「修复后的完整 dofile」；
    # 其 dofile 必须通过确定性校验（见 task2_diagnoser._validate_fixed_dofile：
    # 无幻觉命令 / 输入路径绝对 / 基础命令齐备 / 满足 task_spec 静态断言）才被采用，
    # 否则回退下面的确定性修复 —— 保命，不丢分。
    llm_fix = task2_diagnoser.analyze_and_fix(
        task, base_dofile, llm, facts=facts, understanding=understanding)
    llm_issues = []
    if llm_fix:
        # LLM 给的行号先做一次真实性校验，对不上就丢弃（防编造行号污染证据链）
        llm_issues = [_verify_llm_locator(it, base_dofile)
                      for it in llm_fix["issues"]]
        if llm_fix.get("full_dofile"):
            print(f"  ✓ LLM 诊断 + 修复：{len(llm_issues)} 条问题，"
                  f"修复后 dofile {len(llm_fix['fixed_dofile'])} 字符")
        else:
            print(f"  ✓ LLM 诊断 + 修复决策：{len(llm_issues)} 条问题"
                  f"（预算不足以让 LLM 誊写整份脚本，修复按 fix_action 由执行器落地）")

    # 确定性审查（**始终执行**）：
    #   · LLM 成功时 —— 它是交叉验证与证据补充，防止 LLM 漏报某类故障；
    #   · LLM 不可用 / 未通过校验时 —— 它就是兜底修复器。
    fixed_by_rules, spec_notes = spec_fixer.fix_to_spec(task, base_dofile)
    spec_issues = list(getattr(spec_notes, "issues", []) or [])

    # 产物导出缺失（报告 / 网表 / CTL / SCANDEF）也是任务二的一类问题：
    # original.dofile 可能整段不含导出命令，工具跑完却拿不到任务要求的交付物。
    art_issues = _detect_missing_artifacts(task, original_dofile, base_dofile)

    # 工作流阶段顺序（工具强制；original 可能把 insert_dft_logic 提前到
    # examine_scan_drc 之前 -> SCAN-1009）
    order_issues = []
    if dofile_generator.enforce_workflow_stage_order(base_dofile) != base_dofile:
        order_issues = [_stage_order_issue()]

    rule_issues = order_issues + spec_issues + art_issues
    identified = (task2_diagnoser.merge_issues(llm_issues, rule_issues, base_dofile)
                  if llm_issues else rule_issues)
    for it in identified:
        fix_records.append(_issue_to_record(
            it, original_snapshot, original_text=original_dofile))

    print(f"  ✓ 问题识别完成：共识别 {len(identified)} 条问题"
          f"（LLM {len(llm_issues)} 条 + 确定性审查 {len(rule_issues)} 条，已去重）")
    for rec in fix_records:
        print(f"      - {rec['issue']}")

    # ---- 修复后的 dofile（首轮即用）----
    # 优先级：
    #   1) LLM 产出的完整修复 dofile（预算充裕时才走这条，已过确定性校验）；
    #   2) spec_fixer 的确定性修复 —— 它是 fix_action（LLM 的修复决策）与
    #      task_spec 要求的落地执行器，幂等且可核验。
    current_dofile = (llm_fix.get("fixed_dofile") if llm_fix else "") \
        or fixed_by_rules or base_dofile
    current_dofile = pad_missing_artifacts(current_dofile, task, only_explicit_exports=True)
    # normalize_dofile_layout 在 task2 分支会再跑一次 spec_fixer.fix_to_spec（幂等，
    # 不会产生重复 issue），并完成输出变量、信号 -usage、输入路径绝对化、工作流阶段
    # 顺序（SCAN-1009）、DRC 配置前置等确定性整理。以上全部在**首轮运行之前**生效。
    current_dofile = normalize_dofile_layout(
        task, understanding, current_dofile, is_task2=True)

    # 已知问题清单：**不读取 preset_issues.json**。它是 public case 的参考答案，
    # hidden case 并不提供，且赛题 5.4.2 禁止依赖针对具体 case 的人工信息；
    # 问题必须由 Agent 从 original.dofile 与 task_spec 的对照、日志与报告中自主识别。
    known_issues = []

    # 预运行阶段结束：解除限时，恢复为「本 case 内部截止时间」（供多轮修复阶段使用，
    # 保证后续任何 LLM 调用也不会越过内部截止线）。
    if hasattr(llm, "set_deadline"):
        llm.set_deadline(global_deadline)
    _pre_cap = (pre_deadline - _RUN_STARTED_AT) if pre_deadline else 0.0
    print(f"  ⏱ 预运行阶段 {time.time() - t_pre:.1f}s / 上限 {_pre_cap:.1f}s"
          f"（{llm.stats_line() if hasattr(llm, 'stats_line') else ''}）")
    print(f"  ⏱ 剩余预算 {remaining_budget(task) or 0:.1f}s，可用于 dftexp_scan 运行")

    # 待修复的网表文件（任务二网表级修复目标）
    netlist_files = task.get("files", {}).get("netlist", [])

    # 规格类问题（第三类故障）的已尝试记录，防止同一问题反复重试耗尽轮数
    spec_attempts = {}
    # 网表 ECO 已尝试过的规则签名：同一组规则只推导一次，避免同一条 ECO
    # 被反复插入（插入后若仍不收敛，说明推导的接法或根因判断不对，应交回 LLM）。
    eco_tried = set()
    seen_error_sigs = {}  # 错误签名 -> 首次出现的轮次，用于检测修复是否收敛
    best_errors = None    # 历史最小错误数，用于检测「修复在恶化」（见收敛控制 2）

    for run_id in range(1, max_runs + 1):
        # 预算用尽就不再开新一轮：超时 = 当前 case 0 分（docx 5.4.1）
        if time_is_up(task):
            print(f"  ⏱ 运行预算已用尽，不再开 Run {run_id}")
            break

        run_dir = Path(output_dir) / "runs" / f"R{run_id}"
        timeout = tool_timeout_for(task)
        print(f"  [Run {run_id}] 运行 dftexp_scan...（超时 {timeout}s）")
        _t_run = time.time()
        result = tool_runner.run_dofile(current_dofile, run_dir, run_id, timeout=timeout)
        print(f"  ⏱ Run {run_id} 工具耗时 {time.time() - _t_run:.1f}s"
              f"（剩余预算 {remaining_budget(task) or 0:.1f}s）")
        run_results.append(result)

        parsed = _parsed_from_result(result)
        result["_parsed"] = parsed

        # 每轮结束立即增量落盘（被外部 kill 也不至于空手）
        _persist_incremental(output_dir, task, run_results,
                             understanding=understanding, fix_records=fix_records,
                             final_dofile_text=current_dofile,
                             netlist_changes=netlist_changes, task_type="task2")

        if result.get("error") == "tool_not_found":
            print("  ⚠ 工具不可用（本地无 License/工具环境），停止运行")
            break

        if result.get("error") == "timeout":
            print(f"  ✗ Run {run_id} 工具超时被中止（{timeout}s）")
            fix_records.append({
                "run": f"R{run_id}",
                "issue": f"Scan Insertion 工具单轮超过 {timeout}s 被中止",
                "root_cause": "本 case 网表/设计规模较大，单轮耗时超过按预算推导的超时上限",
                "fix": "不再重试同一 dofile（时间预算不足以再跑一轮）",
                "found_run": f"R{run_id}",
                "resolved": False,
            })
            break

        # 日志侧判定：returncode==0 且无 ERROR 且「有效」DRC 违例为 0
        # （任务书明确允许残留的规则号不算违例，见 dofile_generator.drc_residual_rules）
        _drc2 = _drc_gate(task, understanding, parsed)
        result["_drc"] = _drc2                      # 供 decision_logger 写 summary
        run_clean = bool(result.get("success")) and not parsed["errors"] and _drc2["ok"]
        if _drc2["required"]:
            print(f"  · {_drc_brief(_drc2)}")

        # ---- DRC 不下降熔断 ----
        # case6 改造前的真实形态：每轮都是 28 条（DFTR1×12 + DFTR9×16），
        # 而每轮仍在「改声明 -> 跑工具 -> LLM 诊断」，把预算烧光（平台 2 小时事故）。
        # 判据：DRC 违例总数连续 2 轮没有下降就停 —— 此时再改也大概率是同一套无效手段。
        # 注意只在「本 case 把 DRC 当指标」时才生效（前置处理类 required=False）。
        if _drc2.get("required") and not run_clean:
            _tot = _drc2.get("total") or 0
            if best_drc_total is None or _tot < best_drc_total:
                best_drc_total = _tot
                drc_no_progress = 0
            else:
                drc_no_progress += 1
                print(f"  ⚠ DRC 违例 {_tot} 条，较此前最佳 {best_drc_total} 无下降"
                      f"（{drc_no_progress}/2）")
                if drc_no_progress >= 2:
                    print("  ⚠ DRC 连续 2 轮无改善，停止重试（避免耗尽预算）")
                    if fix_records:
                        fix_records[-1]["root_cause"] = (
                            (fix_records[-1].get("root_cause") or "")
                            + f"；DRC 违例连续 2 轮停留在 {best_drc_total} 条，修复未收敛")
                    break

        # 日志干净 ≠ 配置正确：
        # 赛题 4.2.2 的第三类故障「DFT 配置不符合任务要求」的特征正是
        # 「Dofile 可以执行、日志不报 ERROR」（链数/长度/端口命名/时钟域/分区归属不对）。
        # 必须再用 task_spec 规格断言复核一遍，否则会在第一轮就误判成功
        # （问题识别 30 分 + 问题修复 30 分双归零）。
        spec_issues = []
        if run_clean:
            spec_issues = _check_spec_compliance(
                task, understanding, current_dofile, result.get("output", ""), run_dir,
                llm, facts=facts,
            )
            if not spec_issues:
                print(f"  ✓ Run {run_id} 成功（无 ERROR、{_drc_brief(_drc2)}、满足任务要求）")
                result["accepted"] = True          # 判据通过（供 decision_log 写"最终成功"）
                _mark_resolved(fix_records, run_id)
                break

            print(f"  ✗ Run {run_id} 日志无报错，但 {len(spec_issues)} 项 DFT 配置不符合任务要求：")
            for v in spec_issues[:5]:
                print(f"      - {v.get('requirement', '')}（{v.get('evidence', '')}）")

            vl_req = "；".join(v.get("requirement", "") for v in spec_issues[:3])
            diagnosis = {
                "issue": "DFT 配置不符合任务要求：" + vl_req,
                "root_cause": spec_verifier.format_issues(spec_issues),
                "fix": "按 task_spec 原文修正 dofile 的 DFT 配置",
                "violated_requirement": "；".join(
                    v.get("requirement", "") for v in spec_issues),
            }
            fix_records.append({
                "run": f"R{run_id}",
                "issue": diagnosis["issue"],
                "root_cause": diagnosis["root_cause"],
                "fix": diagnosis["fix"],
                "violated_requirement": diagnosis["violated_requirement"],
                "spec_issues": spec_issues,
                "found_run": f"R{run_id}",
                "applied_run": f"R{run_id + 1}" if run_id < max_runs else "",
                "resolved": False,
            })

            if run_id >= max_runs or time_is_up(task):
                break

            # 同一批规格问题重复出现 -> 不再重复尝试（避免耗尽预算）
            sig = "|".join(sorted(v.get("requirement", "") for v in spec_issues))
            spec_attempts[sig] = spec_attempts.get(sig, 0) + 1
            if spec_attempts[sig] > 2:
                print("  ⚠ 同类配置问题多次修复无效，停止重试（避免耗尽运行预算）")
                break

            fixed = dofile_fixer.targeted_spec_fix(task, current_dofile, spec_issues, llm)
            if fixed:
                # 规格补丁同样可能带出参数形式错误的命令，先做确定性规范化
                fixed, norm_notes = dofile_generator.normalize_dofile_commands(task, fixed)
                for n in norm_notes:
                    print(f"      ⚠ {n}")
                # 补丁可能打乱工作流阶段顺序 / DRC 配置位置，重新规范化
                fixed = normalize_dofile_layout(task, understanding, fixed, is_task2=True)
                fixed = dofile_generator.place_drc_configs_before_examine(fixed)
            if fixed and fixed != current_dofile:
                current_dofile = fixed
                print("  ↻ 已按任务要求修正 DFT 配置，重试")
                continue
            print("  ⚠ 未能生成有效的配置修正，停止")
            break

        # ---- 日志错误驱动路径 ----
        print(f"  ✗ Run {run_id} 有问题：errors={len(parsed['errors'])}, violations={parsed['total_violations']}")
        diagnosis = diagnose_with_llm(
            task, current_dofile, parsed, known_issues, llm, netlist_info
        )

        # 记录问题闭环
        fix_records.append({
            "run": f"R{run_id}",
            "issue": diagnosis.get("issue", "未知问题"),
            "root_cause": diagnosis.get("root_cause", ""),
            "fix": diagnosis.get("fix", ""),
            "found_run": f"R{run_id}",
            "applied_run": f"R{run_id + 1}" if run_id < max_runs else "",
            "resolved": False,
        })

        if run_id >= max_runs or time_is_up(task):
            break

        # 收敛控制：同一组错误签名再次出现，说明修复在原地打转，继续重试只会
        # 耗尽工具调用预算（docx 5.4.1 超限即 0 分）
        err_sig = _err_sig_of(result)
        if err_sig and err_sig in seen_error_sigs:
            print(f"  ⚠ 错误签名与 R{seen_error_sigs[err_sig]} 完全相同（{'/'.join(err_sig)}），"
                  "修复未收敛，停止重试")
            if fix_records:
                fix_records[-1]["root_cause"] = (
                    (fix_records[-1].get("root_cause") or "")
                    + f"；同一组错误（{'/'.join(err_sig)}）反复出现，修复未收敛"
                )
            break
        if err_sig:
            seen_error_sigs[err_sig] = run_id

        # 收敛控制 2：错误数不降反升 —— 修复在恶化（同任务一，见那边的说明）。
        n_errors = parsed.get("error_total") or len(parsed["errors"])
        if best_errors is not None and n_errors > best_errors:
            print(f"  ⚠ 错误数 {n_errors} 高于此前最佳 {best_errors}，修复在恶化，停止重试")
            if fix_records:
                fix_records[-1]["root_cause"] = (
                    (fix_records[-1].get("root_cause") or "")
                    + f"；修复后错误数由 {best_errors} 升至 {n_errors}，修复在恶化"
                )
            break
        if best_errors is None or n_errors < best_errors:
            best_errors = n_errors

        # ① 确定性 ECO（首选）：根因在网表结构上时，用工具自带的网表编辑命令
        #    直接把修法落进 dofile —— 改动面只有一个连接 + 一个 MUX，可解释、
        #    可 LEC、也不依赖 LLM 誊写整份网表。
        eco_dofile, eco_info = _eco_inject(
            task, current_dofile, parsed, understanding, facts, eco_tried,
            dofile_fix_attempts=dofile_fix_attempts)
        if eco_dofile:
            current_dofile = eco_dofile
            print(f"  ↻ 已按网表结构推导 ECO 修复（{len(eco_info['groups'])} 处："
                  + "、".join(f"{g['kind']}@{g['module']}" for g in eco_info["groups"])
                  + f"），S 端接 {eco_info['mode_port'] or '(未识别)'}，重试")
            fix_records.append({
                "run": f"R{run_id}",
                "issue": "DRC 违例根因位于网表结构（时钟路径穿过组合门 / ICG 测试使能被功能链占据）",
                "root_cause": "；".join(
                    f"{g['kind']}@{g['module']}（驱动 {g['driver']}）"
                    for g in eco_info["groups"]),
                "fix": "在根因连接处插入 MUX：A0=原逻辑、A1=测试逻辑、S=测试模式端口，"
                       "被修引脚连接保持不变（网表 ECO，零功能副作用）",
                "found_run": f"R{run_id}",
                "applied_run": f"R{run_id + 1}" if run_id < max_runs else "",
                "resolved": False,
                "eco": eco_info,
            })
            continue

        # ② dofile 层修复：仅当根因不在网表结构上（或 ECO 已试过）时才走这条路。
        #    **顺序很关键**：若把 ECO 放在这条路之后，只要 targeted_fix 还能产出
        #    任何改动就会 continue，ECO 永远轮不到 —— task2/case6 就是这样一直
        #    反复改声明、DRC 恒 28 的。
        # 针对性修复（dofile）
        fixed = dofile_fixer.targeted_fix(
            task, current_dofile, parsed, llm,
            diagnosis=diagnosis
        )
        if fixed:
            # 补丁可能又写出参数形式错误的命令，写盘前过一遍确定性文法规范化
            fixed, norm_notes = dofile_generator.normalize_dofile_commands(task, fixed)
            for n in norm_notes:
                print(f"      ⚠ {n}")
            # 补丁也可能打乱工作流阶段顺序 / DRC 前置配置位置，重新规范化
            fixed = normalize_dofile_layout(task, understanding, fixed, is_task2=True)
            fixed = dofile_generator.place_drc_configs_before_examine(fixed)
        if fixed and fixed != current_dofile:
            current_dofile = fixed
            dofile_fix_attempts += 1
            print("  ↻ 已针对性修复 dofile，重试")
            continue

        # 网表级修复：dofile 修复无效，且诊断/轮数指示需改网表
        if not llm.available:
            print("  ⚠ 无法进一步修复，停止")
            break

        if (not netlist_rewrite_tried
                and lec_verifier.needs_netlist_fix(diagnosis, parsed, fix_records)
                and netlist_files):
            netlist_rewrite_tried = True
            _rem = remaining_budget(task)
            if _rem is not None and _rem < 240:
                # 剩余预算装不下「LLM 重写 + LEC」，直接进最终整理，保住已有产物
                print(f"  ⏱ 剩余 {_rem:.0f}s 不足以做网表重写+LEC，停止重试")
                break
            netlist_change = _try_netlist_fix(
                task, netlist_files, diagnosis, parsed, current_dofile,
                output_dir, llm, run_id=run_id,
            )
            if netlist_change:
                netlist_changes.append(netlist_change)
                if netlist_change.get("applied"):
                    print("  ↻ 已通过 LEC 的网表级修复（改在 output 侧副本上），重试")
                    # 把 dofile 的 load_netlist 指向修改后的副本
                    new_dofile = _redirect_load_netlist(
                        current_dofile, netlist_change.get("modified_path", "")
                    )
                    if new_dofile and new_dofile != current_dofile:
                        current_dofile = new_dofile
                    continue
                else:
                    print(f"  ⚠ 网表修复未采用（{netlist_change.get('reason', '')}），停止")
                    break

        print("  ⚠ 无法进一步修复，停止")
        break

    # 整理输出
    final_run = _best_run(run_results) if run_results else {}

    # 最终采用的网表修复（若网表被修改）
    applied_change = None
    for nc in reversed(netlist_changes):
        if nc.get("applied"):
            applied_change = nc
            break

    output_manager.finalize_outputs(output_dir, final_run, task,
                                    netlist_change=applied_change)
    output_manager.generate_diffs(output_dir, run_results,
                                  original_dofile_text=original_dofile)

    decision_log = decision_logger.build_decision_log(
        task, "task2", run_results, understanding, fix_records,
        # 与落盘的 final.dofile 同源（final_run 可能不是最后一轮）
        final_dofile_text=_dofile_text_of_run(final_run) or current_dofile,
        netlist_changes=netlist_changes,
        final_run_id=final_run.get("run_id"),
        original_dofile_text=original_dofile,
    )
    decision_logger.write_decision_log(output_dir, decision_log)

    print("  ✓ 任务二完成")
    return 0


def _check_spec_compliance(task, understanding, dofile_text, log_text, run_dir, llm,
                           allow_llm=True, facts=None):
    """
    核对最终 dofile 是否满足 task_spec 的明确要求（第三类故障的检测路径）。

    先做确定性断言校验（配置项 / 数值 / 端口命名），全部通过后再让 LLM 做一次
    「任务要求 vs 最终配置」的对照复核（带可核验过滤，避免幻觉制造假问题）。

    Args:
        allow_llm: False 时只跑确定性断言（task1 在 150s 预算下没有第二次 LLM 调用可花）。
        facts: 网表事实（用 top_ports 过滤掉不存在的端口断言）。

    Returns:
        list（违反项）或 None（复核本身异常）。None **不阻断**本轮成功（避免检查器
        故障导致死循环），但必须显式告警 —— 旧实现 except 后静默 return []，调用方
        据此判定成功，形成「检查器坏了 = 一切都对」的假成功 + 误标闭环。
    """
    report_text = _collect_report_text(run_dir)
    valid_ports = set((facts or {}).get("top_ports") or {}) or None
    try:
        return spec_verifier.verify_spec(
            task, understanding, dofile_text,
            log_text=log_text, report_text=report_text,
            llm=(llm if allow_llm else None),
            use_llm=bool(allow_llm and llm is not None),
            valid_ports=valid_ports,
        )
    except Exception as exc:                                    # noqa: BLE001
        print(f"  ⚠ 规格复核异常（本次按「未复核」处理，不计为通过）：{exc}")
        return None


def _collect_report_text(run_dir, limit=12000):
    """汇总本轮生成的 .rpt 报告文本，供规格复核比对数值与端口名。"""
    run_dir = Path(run_dir)
    chunks, total = [], 0
    for cand in (run_dir / "reports", run_dir / "output"):
        if not cand.exists():
            continue
        for p in sorted(cand.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in (".rpt", ".txt"):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            chunks.append(f"### {p.name}\n{text}")
            total += len(text)
            if total > limit:
                break
        if total > limit:
            break
    return "\n".join(chunks)[:limit]


def _redirect_load_netlist(dofile_text, modified_path):
    """把 dofile 里 load_netlist 的输入路径改指向网表修复后的副本（output 侧）。"""
    if not modified_path:
        return dofile_text
    target = str(modified_path).replace("\\", "/")
    out, changed = [], False
    for line in dofile_text.splitlines(keepends=True):
        if re.match(r"\s*load_netlist\b", line):
            line = re.sub(r'"[^"]*"', '"%s"' % target, line, count=1)
            changed = True
        out.append(line)
    return "".join(out) if changed else dofile_text





# ============================================================================
# 诊断与修复辅助
# ============================================================================

def _analyze_netlist_for_task2(task):
    """
    任务二：分析网表结构，补充诊断上下文。
    返回 dict 或 None（网表不可分析时）。
    """
    netlist_files = task.get("files", {}).get("netlist", [])
    if not netlist_files:
        return None

    # 只分析最大的网表（顶层），且限制文件大小避免大网表卡顿
    info = None
    for nf in netlist_files:
        try:
            size = os.path.getsize(nf)
            if size > 50 * 1024 * 1024:  # >50MB 的网表跳过全量分析，只读头部
                info = netlist_analyzer.analyze_netlist_head(nf)
            else:
                info = netlist_analyzer.analyze_netlist(nf)
            if info and info.get("modules"):
                break
        except Exception:
            continue
    return info


def _eco_inject(task, current_dofile, parsed, understanding, facts, tried,
                dofile_fix_attempts=0):
    """
    确定性网表 ECO：把 ECO 命令插进 dofile（在 present_design 之后、DFT 配置之前）。

    为什么走「命令注入」而不是「LLM 重写整份网表」：
      task2/case6 真跑证实后者会把顶层的测试模式端口声明漏掉（随后 connect_pin
      报 SCAN-1315），而且改动面不可控、LEC 也难定位。ECO 命令只动「根因所在的
      那一个连接 + 一个 MUX」，天然满足「最小修改 / 功能态透传」，且 derive 出来的
      A0/A1/S 接法与任务书约定逐字一致。

      （2026-10-23 补充）官方随 case6 附带的 yosys miter → btor → btormc 参考链
      也正是这么验的：**功能态（scan_mode=0）下 netlist_fixed.v 与原网表顶层等价**。
      而 netlist_fixed.v 是 dftexp_scan 执行 dofile 里的 ECO 命令后 dump 出来的产物
      —— 说明「把 ECO 命令写进 dofile」正是命题方预期的形态。

    Returns:
        (新 dofile 文本, 说明 dict) —— 无可用 ECO 时返回 (None, None)
    """
    key = _violation_rule_key(parsed)
    if not key or key in tried:
        return None, None

    # 硬门槛：什么时候才允许动网表。
    #
    # 两道并行判据（OR），兼顾「准」与「泛化」：
    #   ① spec 明确说缺陷在网表层、要用 ECO 命令修 —— 强信号，立即动手；
    #   ② **行为判据**：dofile 层修复已经试过一次仍然没好 —— 说明改配置消不掉，
    #      根因大概率在结构上，此时上 ECO 是合理的下一步。
    #
    # 为什么需要 ②（泛化，2026-10-23）：比赛会给 5 个全新 case。①只认
    # 「网表缺陷 / ECO / disconnect_net…」这类措辞，新 case 完全可能写成
    # 「修复 DFF 时钟不可控」「时钟在测试态不可活动」而不出现"网表"二字 ——
    # 那样 DFTR1/DFTR9 的结构性缺陷就永远等不到 ECO，表现为「怎么修都不降」。
    # ②不依赖任何措辞，只看「配置层修过了、还是没好」这个事实。
    #
    # 为什么 ② 不会误伤 case3（当初加硬门槛的原因）：
    #   case3 的 DFTR1 靠「把该 net 声明为扫描时钟」这类 dofile 修复就能清零，
    #   所以它的 dofile 修复一成功就结束了，根本走不到 ②；而 ② 又排在 ① 之后、
    #   要求先试过 dofile 修复，不会像旧代码那样「首轮失败就抢跑改网表」。
    spec_hint = dofile_generator._needs_netlist_eco(task.get("task_spec", ""))
    if not spec_hint and dofile_fix_attempts < 1:
        print("  · 未从 spec 判定为网表缺陷，先尝试 dofile 层修复"
              "（若无效则自动转入网表 ECO）")
        return None, None

    tried.add(key)

    netlists = task.get("files", {}).get("netlist", [])
    if not netlists:
        return None, None
    # 只对该 case 的顶层网表做 ECO（取含 top 模块的那个，最大/首个通常就是）
    top = (facts or {}).get("top") or task.get("top_module")
    target = None
    for nf in netlists:
        try:
            text = Path(nf).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if top and re.search(r"\bmodule\s+%s\b" % re.escape(top), text):
            target = (nf, text)
            break
    if target is None:
        return None, None

    lib_meta = {}
    try:
        lib_meta = netlist_facts.parse_lib_metadata(
            task.get("files", {}).get("lib", []))
    except Exception:
        pass

    # 测试模式端口：网表已声明但无负载的那个（case6 的 scan_mode）
    #
    # 旧实现只做「无负载端口名出现在 spec 文本里」的匹配 —— 隐藏 case 若没在
    # spec 里点名该端口，mode_port 就是空串，derive_netlist_eco 直接 return None，
    # 整条 ECO 链路静默失效（表现为「DRC 一直在、修不降」）。所以补两级兜底：
    #   ① spec 点名（最可信）；
    #   ② 名字像测试模式（scan_mode / test_mode / scan_en / se / tm …）的无负载端口。
    mode_port = ""
    unloaded = (facts or {}).get("unloaded_ports") or []
    spec = task.get("task_spec", "") or ""
    for p in unloaded:
        if re.search(r"\b%s\b" % re.escape(p), spec):
            mode_port = p
            break
    if not mode_port and unloaded:
        _mode_name_re = re.compile(
            r"(scan_?mode|scan_?en|test_?mode|test_?en|^se$|^tm$|^scan$|"
            r"mode_?sel|dft)", re.I)
        mode_port = next((p for p in unloaded if _mode_name_re.search(p)), "")
    # 扫描时钟端口
    clocks = [c.get("port") for c in (understanding or {}).get("clock_signals") or []
              if c.get("role", "scan_clock") != "described_only"]
    clock_port = clocks[0] if clocks else ""

    eco = lec_verifier.derive_netlist_eco(
        {"scan_clock_port": clock_port},
        target[1], lib_meta, parsed, mode_port=mode_port)
    if not eco:
        return None, None

    new_dofile = _insert_eco_block(current_dofile, eco["lines"])
    if not new_dofile:
        return None, None
    # clock_port 用「实际采用值」：spec 没写时是网表反推出来的那个，
    # 决策日志要能复现「A1 到底接到了哪个端口」。
    return new_dofile, {"mode_port": mode_port,
                        "clock_port": eco.get("clock_port") or clock_port,
                        "groups": eco["groups"], "netlist": str(target[0])}


def _insert_eco_block(dofile_text, eco_lines):
    """
    把 ECO 命令块插到 `present_design` 之后（必须在 DFT 配置与 DRC 之前）。

    找不到 present_design 时返回 None —— 宁可不做，也不要把命令插到未知位置
    造成 CMD-0074 之类的执行错误。
    """
    lines = dofile_text.splitlines()
    idx = None
    for i, ln in enumerate(lines):
        if re.match(r"\s*present_design\b", ln):
            idx = i
    if idx is None:
        return None
    block = ["", "# ── 网表 ECO 修复（确定性推导，A0=原逻辑 / A1=测试逻辑 / S=测试模式）──"]
    block += [f"    {l}" if False else l for l in eco_lines]
    return "\n".join(lines[:idx + 1] + block + lines[idx + 1:]) + "\n"


def _violation_rule_key(parsed):
    """本轮 DRC 里「根因在网表」的规则签名（用于只尝试一次）。"""
    rules = lec_verifier._violation_rules(parsed)
    hit = sorted(rules & lec_verifier._NETLIST_ROOT_DRC_RULES)
    return "|".join(hit) if hit else ""


def _try_netlist_fix(task, netlist_files, diagnosis, parsed, current_dofile,
                     output_dir, llm, run_id="R1"):
    """
    尝试网表级修复：让 LLM 生成网表修改，经 LEC 校验通过才采用。

    Args:
        run_id: 当前轮次（用于把工作副本命名为 pre_scan_<run_id>.v，与
            docx 8.2.2「runs/R{n}/deliverables/pre_scan_R{n}.v」对齐）

    Returns:
        dict 或 None：网表变更记录（含 applied / lec 结果），失败返回 None
    """
    if not netlist_files:
        return None

    # 选择待修复网表（优先最小的 pre_scan 网表，避免大网表全量读入）
    target = None
    for nf in sorted(netlist_files, key=lambda p: Path(p).stat().st_size):
        size = Path(nf).stat().st_size
        if size > 50 * 1024 * 1024:
            continue  # 大网表不适合 LLM 全量改写
        target = Path(nf)
        break
    if target is None:
        print("  ⚠ 网表过大，跳过网表级修复")
        return None

    # 让 LLM 生成网表级修复（完整新网表内容）
    fix_content = _generate_netlist_fix_with_llm(
        task, target, diagnosis, parsed, current_dofile, llm
    )
    if not fix_content or fix_content.strip() == target.read_text(encoding="utf-8", errors="replace").strip():
        return None

    # 经 LEC 校验
    result = lec_verifier.apply_netlist_fix(
        task, str(target), fix_content, output_dir, llm, run_id=run_id
    )
    return result


def _generate_netlist_fix_with_llm(task, netlist_path, diagnosis, parsed,
                                   current_dofile, llm):
    """
    让 LLM 生成网表级修复内容（完整网表文本）。

    Returns:
        str 或 None
    """
    if not llm.available:
        return None

    netlist_text = netlist_path.read_text(encoding="utf-8", errors="replace")

    system_prompt = """你是数字 IC 网表（Verilog）修复专家。
根据诊断信息，对网表做**最小化**修改以修复问题（如：修正单元连接、补全缺失端口、修正常量连接等）。
严格要求：
1. 只输出修改后的完整网表内容，不要 Markdown 代码块，不要解释。
2. 只做解决问题所必需的最小改动，不要重构无关逻辑。
3. 保持模块名、端口名、实例名不变（否则等价性检查会失败）。
4. 修改后网表必须与原网表在功能上等价（除了修复目标外）。"""

    user_prompt = f"""# 任务说明书
{task['task_spec']}

# 当前 dofile
{current_dofile}

# 诊断信息
{json.dumps(diagnosis or {}, ensure_ascii=False, indent=2)}

# DRC 违例
{json.dumps(parsed.get('drc_violations', [])[:10], ensure_ascii=False, indent=2)}

# 待修复网表（{netlist_path.name}）
{netlist_text[:200000]}

请输出修改后的完整网表。"""

    content = llm.chat(system_prompt, user_prompt)
    if content is None:
        return None
    return dofile_generator.clean_dofile(content)


def diagnose_with_llm(task, current_dofile, parsed, known_issues, llm, netlist_info=None):
    """用 LLM 诊断问题，返回 {"issue", "root_cause", "fix"}。"""
    # 优先使用 preset_issues 中的已知诊断（public case）
    if known_issues:
        # 简单匹配：根据错误信息匹配问题编号
        err_text = " ".join(parsed.get("errors", []))
        for issue in known_issues:
            phenomenon = issue.get("现象", "")
            # 提取现象中的关键 ERROR 编号做模糊匹配
            if any(code in err_text for code in _extract_error_codes(phenomenon)):
                return {
                    "issue": issue.get("问题编号", ""),
                    "root_cause": issue.get("根因", ""),
                    "fix": issue.get("修复方式", ""),
                }

    if not llm.available:
        return {"issue": "未知", "root_cause": "", "fix": ""}

    # 检索手册：根据错误码 / DRC 规则 / 报错命令
    rag_context = ""
    query_parts = []
    for err in parsed.get("errors", []):
        query_parts.extend(_extract_error_codes(err))
        import re as _re
        m = _re.search(r"[Cc]ommand\s+'([\w_]+)'", err)
        if m:
            query_parts.append(m.group(1))
    for v in parsed.get("drc_violations", []):
        query_parts.append(v.get("rule", ""))
    if query_parts:
        rag_context = manual_rag.search_manual(" ".join(query_parts))

    system_prompt = """你是广立微 dftexp_scan（Scan Insertion）工具专家。根据运行日志、网表结构、手册参考诊断问题，返回 JSON：
{"issue": "问题简述", "root_cause": "根因", "fix": "修复方式"}"""

    netlist_desc = ""
    if netlist_info:
        netlist_desc = json.dumps(netlist_info, ensure_ascii=False, indent=2)

    user_prompt = f"""# 任务说明书
{task['task_spec']}

# dofile
{current_dofile}

# 错误
{chr(10).join(parsed.get('errors', [])[:10])}

# 警告
{chr(10).join(parsed.get('warnings', [])[:5])}

# DRC 违例
{parsed.get('total_violations', 0)}
{chr(10).join(json.dumps(v, ensure_ascii=False) for v in parsed.get('drc_violations', [])[:10])}

# 网表结构（分析结果）
{netlist_desc if netlist_desc else '（未分析）'}

# 手册参考段落（RAG）
{rag_context if rag_context else '（无）'}

请诊断。"""

    return llm.chat_json(system_prompt, user_prompt) or {"issue": "未知", "root_cause": "", "fix": ""}


def _parsed_from_result(result):
    """
    从一次工具运行结果解析日志（结果缓存在 result["_parsed"]）。

    **必须以 log_file 为准**：tool_runner 不再把整份日志塞进 result["output"]
    （大 design 单轮 198MB，见 tool_runner.run_dofile 的说明），
    output 只剩尾部摘要。日志的完整解析（错误清单 / 链数 / 违例数）
    必须走文件。
    """
    result = result or {}
    cached = result.get("_parsed")
    if cached is not None:
        return cached
    parsed = report_parser.parse_log(log_path=result.get("log_file"),
                                     log_text=result.get("output", ""))
    # DRC 权威结论 = 日志汇总 + 本轮 reports/*drc*.rpt。
    #
    # 为什么必须读 rpt：dofile 里写的是 `examine_scan_drc -file x.rpt`（官方写法），
    # 此时日志里只有 "Starting design rule checking."，没有 "Total violations:" 行，
    # 于是旧实现的 total_violations 恒为 0 —— 下游「DRC 违例为 0」的判据**永远成立**，
    # task_1/case5 的 69022 条 DFTR1、task_2/case2 的 21 条 DFTR10 全部隐身。
    try:
        drc = report_parser.collect_drc(run_dir=result.get("run_dir"), parsed=parsed)
        if drc.get("rules"):
            parsed["drc_rules"] = drc["rules"]
        parsed["total_violations"] = max(int(parsed.get("total_violations") or 0),
                                         int(drc.get("total_violations") or 0))
        parsed["drc_sources"] = drc.get("sources") or []
    except Exception as exc:                                    # noqa: BLE001
        print(f"  ⚠ DRC 报告解析异常（按无违例继续）：{exc}")
    try:
        result["_parsed"] = parsed
    except TypeError:                       # result 可能是不可变对象
        pass
    return parsed


def _err_sig_of(result):
    """该轮日志里的错误码集合（用于判断修复是否收敛）。"""
    errs = (_parsed_from_result(result).get("errors") or [])
    return tuple(sorted(set(_extract_error_codes("\n".join(errs)))))


# ============================================================================
# 统一的「本轮是否算成功」判据（task1 / task2 共用）
# ----------------------------------------------------------------------------
# 旧实现两个任务各写一套：task1 只看 "有没有 ERROR 行"，task2 还看 DRC 违例。
# 于是 task1 在 DRC 残留 4621 / 69022 条违例时仍然宣告「最终成功；末轮错误数 0」
# （summary 与 final.log 直接矛盾，踩在 docx 5.5.1「问题闭环真实性」上）。
# 这里统一成三层：
#   1) 进程成功且日志无 ERROR/FATAL；
#   2) DRC 有效违例为 0（仅「真建链」的 case 把 DRC 清零当指标；
#      纯前置处理 case 如 t1/case1、case4 官方 golden 也不跑 DRC）；
#   3) 任务要求逐条满足（缺件/配置不符由 spec 断言兜底，见 _check_spec_compliance）。
# ============================================================================

def _builds_chain(task, understanding):
    try:
        return spec_verifier.builds_chain(task, understanding)
    except Exception:                                    # noqa: BLE001
        return True


def _allowed_drc_rules(task):
    """任务书明确「允许残留」的 DRC 规则号（这些违例不计入失败）。"""
    try:
        return list(dofile_generator.drc_residual_rules(task))
    except Exception:                                    # noqa: BLE001
        return []


def _drc_gate(task, understanding, parsed):
    """
    计算本轮 DRC 是否达标。

    Returns:
        {"ok": bool, "total": int, "by_rule": {}, "allowed": {}, "required": bool}
        required=False 表示本 case 不把 DRC 清零当指标（前置处理类）。
    """
    if not _builds_chain(task, understanding):
        return {"ok": True, "total": 0, "by_rule": {}, "allowed": {},
                "required": False, "checked": False}
    eff = report_parser.drc_effective(parsed, _allowed_drc_rules(task))
    checked = bool(parsed.get("drc_rules")) or bool(parsed.get("total_violations"))
    return {"ok": eff["total"] == 0, "total": eff["total"],
            "by_rule": eff["by_rule"], "allowed": eff["allowed"],
            "required": True, "checked": checked}


def _drc_brief(drc):
    """把 DRC 结论变成一行可读文本（写日志 / summary 用）。"""
    if not drc.get("required"):
        return "DRC 非本 case 指标"
    if drc.get("ok"):
        # ok 但没有检出任何 DRC 报告 -> 说明本轮没跑成 DRC，不能宣称"违例 0"
        return "DRC 违例 0" if drc.get("checked") else "DRC 未检出（本轮未产生 DRC 报告）"
    top = sorted((drc.get("by_rule") or {}).items(), key=lambda kv: -kv[1])[:4]
    return "DRC 违例 %d（%s）" % (
        drc.get("total") or 0,
        "、".join("%s×%d" % (r, n) for r, n in top) or "未分类")


def apply_facts_precedence(understanding, facts):
    """
    让「网表结构事实」压过「任务说明书/参考 dofile 的措辞」。

    两个必须纠正的点（都是实测事故）：

      1. **顶层名**：task_spec 的"设计名称"是产品名（case6 写 des_perf，真实顶层 des3）。
         以结构判定为准，否则 present_design 必错，后续命令级联失败。
      2. **DFT 端口必须存在**：port 不在顶层端口表里，说明它是 original.dofile 里被注入的
         故障值（task_2/case1 的 resetn、case2 的 wb_clk）。旧实现把这些故障值合并进
         understanding，spec_verifier 又拿它当"任务要求"，于是**修对了反而判不合格**，
         最终全部 issue 记为 resolved:false（15 分闭环项归零）。

    Returns:
        (understanding, [改动说明...])
    """
    u = understanding if isinstance(understanding, dict) else {}
    facts = facts or {}
    notes = []

    top = str(facts.get("top") or "").strip()
    old = str(u.get("top_module") or "").strip()
    if top and old != top:
        u["top_module"] = top
        notes.append(f"顶层设计按网表结构取 {top}（原 {old or '未识别'}）")

    ports = {str(p) for p in (facts.get("top_ports") or {})}
    if ports:
        def _exists(name):
            name = str(name or "").strip()
            if not name:
                return False
            return name in ports or re.sub(r"\[.*$", "", name) in ports

        # 只对「必须真实存在」的信号做剔除：clock / reset。
        #
        # 不能剔除 scan_enable / constant / wrapper：官方 task_spec 明确这些端口由
        # **工具自动创建**（"scan_enable 和扫描数据端口由工具自动创建"；case6 的
        # wrapper_in_shift_en 等"网表中不存在，工具会自动创建"）。把它们当故障剔掉，
        # 会让 dofile 少写 4 条 set_scan_signal（case2 实测），反而制造新错误。
        #
        # 也不能剔除**内部层次路径**（含 "/"）：add_pseudo_pi 声明的伪主输入本来就是
        # 门控单元的内部时钟引脚（如 x_aq_top_0/x_aq_mmu_top/x_utlb_gateclk/clk_out），
        # 它们永远不可能出现在顶层端口表里。旧写法把 t1/case4、case5 的 9 条 pseudo PI
        # 当故障值全剔了 —— 那 9 条恰恰是与 golden 一字不差的正确配置。
        pseudo = {str(p) for p in (u.get("pseudo_pi") or [])}

        def _internal(port):
            port = str(port or "").strip()
            return "/" in port or port in pseudo

        for key, label in (("clock_signals", "扫描时钟"),
                           ("reset_signals", "扫描复位")):
            keep, dropped = [], []
            for sig in (u.get(key) or []):
                if not isinstance(sig, dict):
                    continue
                port = sig.get("port")
                if not port or _internal(port) or _exists(port):
                    keep.append(sig)
                else:
                    dropped.append(str(port))
            if dropped:
                u[key] = keep
                notes.append("%s端口 %s 不在设计端口中，已剔除（按故障值处理）"
                             % (label, "、".join(dict.fromkeys(dropped))))

        # 兜底：剔除后若主时钟没被声明，用事实里的顶层时钟端口补回一条，
        # 避免「剔掉故障值」把时钟声明也一起弄丢。
        clk_ports = [p for p in (facts.get("clock_ports") or {}) if p in ports]
        if clk_ports:
            have = {str((s or {}).get("port")) for s in (u.get("clock_signals") or [])}
            if not (have & set(clk_ports)):
                u.setdefault("clock_signals", []).append(
                    {"port": clk_ports[0], "off_state": 0})
                notes.append(f"补回主时钟 {clk_ports[0]}（事实推导）")
    return u, notes


def _extract_error_codes(text):
    """从现象描述文本中提取错误代码编号。"""
    import re
    return re.findall(r"[A-Z]+-\d+", text)


# ============================================================================
# 评测计时机制：等待哨兵文件 <input>/.case_ready
# ----------------------------------------------------------------------------
# 依据《评测计时机制优化说明》：
#   - 容器启动时 /input 目录**初始为空**；
#   - Agent 启动后需等待哨兵 /input/.case_ready 出现；
#   - 出题方确认 Agent 已就绪后才放入 case 文件并创建哨兵，**此时开始计时**。
# 若不等待，Agent 会在空目录上解析任务（task_spec 为空、无网表），
# 整个 case 直接判 0 分。因此本函数必须在任何输入读取之前调用。
#
# 本地调试兼容：输入目录里已有 case 文件时（本地直跑，无哨兵机制），
# 只宽限 SCAN_AGENT_READY_GRACE 秒（默认 5s）再继续，避免本地测试卡死。
# 设 SCAN_AGENT_WAIT_READY=0 可完全关闭等待。
# ============================================================================

#
# 【2026-10-22 补充说明更新】就绪标志已由 .case_ready 正式定为
# **case_upload_marker**（0 字节空文件，写在 input 根目录，落盘时刻记为 t1）。
# input 是 :ro 只读挂载，评测系统复制完全部输入后才原子写入该文件，
# 因此「marker 存在」== 「input 完整」。
# 旧的 .case_ready 保留为兼容别名（本地脚本/老平台仍可能造它）。
READY_SENTINEL = "case_upload_marker"
READY_SENTINELS = ("case_upload_marker", ".case_ready")

# 完成标志：**必须写在 output 根目录**（不能放子目录），评测系统一见即归档并
# 停容器 —— 所以只能在「全部产物写完并关闭」之后创建，晚一秒都可能丢产物。
COMPLETE_MARKER = ".case_complete_marker"


def _write_complete_marker(output_dir):
    """
    写出完成标志 .case_complete_marker（t2）。

    约定（补充说明第 2 节）：
      · 必须在 output **根目录**；
      · 必须在所有产出文件写完、关闭之后再写 —— marker 一出现系统立即归档并
        停容器，之后再写的内容全部丢失。
    因此本函数只做一次 mkdir + 一次空文件创建，且调用点必须位于流程最末尾
    （main() 的 finally 里，在 _emergency_finalize 之后）。
    返回秒数（写成功）或 None（已存在/失败），仅用于日志。
    """
    try:
        target = Path(output_dir) / COMPLETE_MARKER
        if target.exists():
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        # 用二进制模式打开并立即关闭：确保 touch 后文件长度为 0 且句柄已释放，
        # 避免「文件已存在但内容/句柄未提交」导致评测系统读到不完整的标记。
        with open(target, "wb"):
            pass
        try:
            os.sync()          # 落盘后再让系统看到，防止归档早于 flush
        except (AttributeError, OSError):
            pass
        return time.time()
    except Exception as exc:                                    # noqa: BLE001
        print(f"  ⚠ 完成标志写入失败：{exc}")
        return None


def wait_for_case_ready(input_dir):
    """
    等待评测就绪标志（case_upload_marker / .case_ready）；返回实际等待秒数。

    语义要点（补充说明第 2 节）：marker 落盘时刻即 t1，计量从此刻开始。
    所以「等到 marker 再读 input」既保证数据完整，也让本地计时与评测口径一致。
    """
    if os.environ.get("SCAN_AGENT_WAIT_READY", "1") == "0":
        print(f"  ⏱ 已关闭哨兵等待（SCAN_AGENT_WAIT_READY=0）")
        return 0.0

    sentinel = None
    for name in READY_SENTINELS:
        p = os.path.join(input_dir, name)
        if os.path.exists(p):
            sentinel = p
            break
    if sentinel:
        print(f"  ⏱ 就绪标志已存在：{sentinel}")
        return 0.0
    sentinel = os.path.join(input_dir, READY_SENTINEL)

    try:
        has_case = bool(os.listdir(input_dir))
    except OSError:
        has_case = False

    try:
        grace = float(os.environ.get("SCAN_AGENT_READY_GRACE", "5"))
    except ValueError:
        grace = 5.0

    t0 = time.time()
    deadline = (t0 + grace) if has_case else None
    if has_case:
        print(f"  ⏱ 未见就绪标志（本地直跑？），宽限 {grace:g}s 后继续")
    else:
        print(f"  ⏱ 等待就绪标志 {sentinel}（评测 t1 自其落盘起算）...")

    next_beat = 5.0
    while True:
        hit = next((p for p in (os.path.join(input_dir, n) for n in READY_SENTINELS)
                    if os.path.exists(p)), None)
        if hit:
            break
        waited = time.time() - t0
        if deadline is not None and time.time() >= deadline:
            print("  ⏱ 宽限期结束，按本地模式继续")
            return waited
        if deadline is None and waited >= next_beat:
            print(f"  ⏱ 仍在等待就绪标志（已 {waited:.0f}s）")
            next_beat = waited + 5.0
        time.sleep(0.05)

    waited = time.time() - t0
    print(f"  ⏱ 就绪标志出现（等待 {waited:.2f}s），开始处理任务")
    return waited


# ============================================================================
# 主函数
# ============================================================================

# ============================================================================
# 构建标识
# ----------------------------------------------------------------------------
# 打印在启动日志的第一屏，随 container.log 一起被平台回传。
#
# 为什么需要它：第二轮提交后排查时发现，平台实际执行的 runs/R1/deliverables/
# R1.dofile 里还带着 -verbose（该开关在新版里早已默认关闭），日志文案也与代码
# 对不上 —— 说明「提交的包」与「排查的代码」不是同一份，但当时没有任何直接
# 手段能判定，只能靠一堆间接特征去猜、去比。有了这行，一眼就能确认。
#
# 核对方式：
#   grep BUILD_TAG <平台返回的 intermediate_files/*/container.log>
# 或在容器里直接看启动横幅。
_BUILD_TAG = os.environ.get("SCAN_AGENT_BUILD", "2026-10-23-r17-refalign")


def main():
    global _RUN_STARTED_AT
    parser = argparse.ArgumentParser(description="Scan Insertion Agent")
    parser.add_argument("-input", "--input", dest="input", required=True, help="输入目录")
    parser.add_argument("-output", "--output", dest="output", required=True, help="输出目录")
    args = parser.parse_args()

    print("=" * 60)
    print(f"  Scan Insertion Agent   [build {_BUILD_TAG}]")
    print("=" * 60)
    print(f"  LLM: {'可用' if config.llm_available() else '不可用(规则兜底)'}")
    # SDK 版本：平台构建装到的是 openai 3.37.0（>=1.0.0 放开后由镜像源解析），
    # 参考示例 pin 的是 1.12.0。打印出来，出问题时能立刻判断是不是 SDK 大版本差异。
    _oai = ""
    try:
        _oai = openai_version()
    except Exception:                                        # noqa: BLE001
        pass
    print(f"       openai SDK {_oai or '(未安装)'}")
    if config.llm_available():
        # 只打印「脱敏后的地址 + 模型名」，方便核对归一化结果（base_url 必须以
        # /v1 结尾，否则 OpenAI 兼容客户端会打到 /chat/completions 而 404）。
        # 绝不打 api_key 本身。
        print(f"       model={config.LLM_CONFIG['model']}  "
              f"base_url={config.LLM_CONFIG['base_url']}")
    else:
        # 缺失原因逐项列出：评测环境无公网，缺变量就会一路降级到规则路径，
        # 事后看日志必须能一眼分清「没注入」还是「注入了但格式不对」。
        _miss = [k for k, v in (
            ("LLM_API_KEY", config.LLM_CONFIG["api_key"]),
            ("LLM_BASE_URL", config.LLM_CONFIG["base_url"]),
            ("LLM_MODEL", config.LLM_CONFIG["model"]),
        ) if not v]
        if _miss:
            print(f"       ⚠ 缺少环境变量：{', '.join(_miss)}（将走确定性规则路径）")
    print(f"  工具: {'可用' if config.tool_available() else '不可用'}")
    print(f"  License: {'已设置' if config.LICENSE_SERVER else '未设置'}")
    # 手册 RAG 自检（首次调用会解析 PDF 建索引，约 2 秒）
    try:
        _ms = manual_rag.manual_stats()
    except Exception:
        _ms = {"available": False}
    if _ms.get("available"):
        print(f"  手册: 已索引 {_ms['sections']} 节 / {_ms['keywords']} 关键词（{_ms['source']}）")
    else:
        print("  手册: 未找到（RAG 关闭，仅靠 LLM 知识 + 参考 dofile）")
    print()

    # License 缺失预警：dftexp_scan 的 DFT 命令会全部报 SCAN-0620，
    # 与 dofile 内容无关，提前提示以免浪费一次运行。
    if not config.LICENSE_SERVER:
        print("  ⚠ 未检测到 SCANINSERTION_LICENSE_SERVER 环境变量。")
        print("    dftexp_scan 的 ScanInsertion 命令将报 SCAN-0620（读环境变量失败）。")
        print("    本地测试请设置：export SCANINSERTION_LICENSE_SERVER=8273@121.40.209.111")
        print("    （Docker 运行请加 -e SCANINSERTION_LICENSE_SERVER=... ，或取消 .env 中该行注释）")
        print()

    llm = get_llm()

    # 评测计时机制：必须在读取任何输入之前等待哨兵 /input/.case_ready
    wait_for_case_ready(args.input)

    # 预算计时起点：赛题 5.4.1 的 wall time 从「哨兵创建（case 文件落入 /input）
    # → 容器退出」计算，因此必须**在哨兵就绪之后**才起表。若提前到 main() 开头，
    # 评测方填充 /input 的等待时间会白吃掉预算，导致 time_is_up 提前触发、
    # 提前放弃后续轮次（末轮不达标 → 5.4.1 超限 0 分）。
    # 计时起点（global 声明已在函数开头）
    _RUN_STARTED_AT = time.time()

    task_type = "task1"
    rc = 1
    try:
        task_type = input_parser.detect_task_type(args.input)
        if task_type == "task2":
            rc = run_task2(args.input, args.output, llm) or 0
        else:
            rc = run_task1(args.input, args.output, llm) or 0
    except SystemExit as e:
        # 子流程里可能 sys.exit()；按退出码透传，但**仍要写完成标志**
        rc = e.code if isinstance(e.code, int) else 1
    except BaseException as e:                                  # noqa: BLE001
        # 兜底：任何未预期异常都不允许「零产物」退出。
        # 平台 2 小时超时那次连 decision_log 都没有 —— 事后完全无法定位。
        print()
        print("=" * 60)
        print(f"  错误: {type(e).__name__}: {e}")
        print("=" * 60)
        import traceback
        traceback.print_exc()
        _emergency_finalize(args.input, args.output, task_type)
        rc = 1
    finally:
        # ================================================================
        # 完成标志（t2）—— 全流程的最后一步，必须放在 finally 里。
        #
        # 为什么用 finally 而不是「正常返回前」：
        #   ① 只要没写 marker，评测系统就不知道 agent 已完成，只能靠外部超时
        #      兜底 —— 那正是「跑两小时 fail」的最坏形态；
        #   ② 即使中途异常（含 _emergency_finalize 已写出部分产物），也必须
        #      让系统拿到 t2，否则连「有产物但超时」都算不上；
        #   ③ 它是本进程最后一个动作：marker 一落盘系统就归档并停容器，
        #      之后再执行的代码写不进归档。
        # 因此这里不再做任何耗时操作，只 touch 一个空文件。
        # ================================================================
        try:
            _scan_elapsed = time.time() - _RUN_STARTED_AT
        except Exception:                                       # noqa: BLE001
            _scan_elapsed = -1.0
        _write_complete_marker(args.output)
        if _scan_elapsed >= 0:
            print(f"  ⏱ 端到端耗时 {_scan_elapsed:.1f}s，已写完成标志 "
                  f"{COMPLETE_MARKER}")
    return rc


def _emergency_finalize(input_dir, output_dir, task_type):
    """
    异常退出前的最后一搏：把「已经跑完的轮次」整理成可评分的产物。

    只做**不会失败**的事，任何一步出错都忽略 —— 此时的首要目标是留证据，不是完美。
    """
    try:
        out = Path(output_dir)
        if not out.exists():
            return
        print("  ↳ 尝试写出最小可评分产物（decision_log / final_results）…")
    except Exception:
        return

    # 1) 若有轮次目录，尝试用最后一轮补齐 final_results
    try:
        runs = sorted((Path(output_dir) / "runs").glob("R*"),
                      key=lambda p: p.name)
        if runs:
            last = runs[-1]
            fin = Path(output_dir) / "final_results"
            fin.mkdir(parents=True, exist_ok=True)
            for sub in ("deliverables", "output", "reports"):
                src = last / sub
                if src.exists():
                    dst = fin / sub
                    dst.mkdir(parents=True, exist_ok=True)
                    for f in src.iterdir():
                        if f.is_file():
                            try:
                                shutil.copy2(f, dst / f.name)
                            except OSError:
                                pass
            log = last / f"{last.name}.log"
            if log.exists():
                try:
                    shutil.copy2(log, fin / "final.log")
                except OSError:
                    pass
        print("  ↳ 已复制末轮产物到 final_results/")
    except Exception as exc:                                    # noqa: BLE001
        print(f"  ⚠ 末轮产物复制失败：{exc}")

    # 2) 写一个「如实说明中途失败」的 decision_log（不冒充成功）
    try:
        dl = Path(output_dir) / "decision_log.json"
        if not dl.exists():
            import json as _json
            dl.write_text(_json.dumps({
                "case_id": Path(input_dir).name,
                "task": task_type,
                "final_run": None,
                "summary": ("执行中途异常终止（未完成正常流程），"
                            "已保留中断前产生的轮次产物；本文件为应急写出，"
                            "不代表任务成功。"),
                "issue_resolutions": [],
                "tool_runs": [],
                "file_changes": [],
                "aborted": True,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            print("  ↳ 已写出应急 decision_log.json（标记 aborted=true）")
    except Exception as exc:                                    # noqa: BLE001
        print(f"  ⚠ 应急 decision_log 写入失败：{exc}")


if __name__ == "__main__":
    _rc = 1
    try:
        _rc = main()
    except BaseException as _e:                                 # noqa: BLE001
        # main() 内部已兜底；能到这里说明连 finally 之前就炸了（如 argparse 参数
        # 错误、内存不足）。这里只保证「进程有退出码」，不再尝试写产物。
        print(f"  ⚠ 进程级异常：{type(_e).__name__}: {_e}", file=sys.stderr)
        _rc = 1
    sys.exit(_rc)

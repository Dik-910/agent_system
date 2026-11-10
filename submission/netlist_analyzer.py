#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网表分析模块
分析 Verilog 网表，提取模块结构、DFF 类型统计、时钟/复位连接等信息，
用于任务二的问题诊断与规则兜底。
"""

import re
from pathlib import Path


def analyze_netlist_head(netlist_path, read_bytes=64 * 1024):
    """
    只读网表头部（用于超大网表，如 >50MB），提取 module 声明和顶层模块名。
    避免全量读入导致内存/耗时问题。

    Returns:
        dict: 精简信息 {"modules": [...], "top_module": ...}
    """
    try:
        with open(netlist_path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(read_bytes)
    except Exception:
        return None

    modules = re.findall(r"^\s*module\s+([A-Za-z_][\w]*)", head, re.MULTILINE)
    top_module = modules[0] if modules else ""
    return {"modules": modules, "top_module": top_module, "partial": True}


def analyze_netlist(netlist_path):
    """
    分析单个网表文件，返回统计信息。

    Returns:
        dict: {
            "modules": [模块名],
            "top_module": 顶层模块名（启发式）,
            "dff_types": {dff类型: 数量},
            "cell_types": {单元类型: 数量},
            "port_count": input/output 声明数,
        }

    性能约束（重要，勿回退）：
      本函数在 task_2 中于**首轮工具运行之前**调用，而 task_1/task_2 多数 case 的
      wall time 预算只有 150s（赛题 5.4.1：超时 = 当前 case 0 分）。

      旧实现推断顶层模块时写成：

          for mod in modules:
              refs = len(re.findall(r"\\b" + mod + ..., content))   # 全文重扫

      复杂度 O(模块数 × 文件大小)。实测 task_2/case2（网表仅 2.98MB、约 200 个模块）
      单此一处耗时 **83.7s**（同代码在 0.61MB 的 case1 上只要 0.53s），配合启动开销
      与 LLM 理解，直接把 150s 预算吃光 —— 平台首跑该 case 的日志停在
      "⏱ 运行预算已用尽，不再开 Run 1"，一次 dftexp_scan 都没跑起来。

      现改为复用上面**单次扫描**得到的 cell_types 计数（同一语义：该模块名被实例化的
      次数），整体降到 O(文件大小)。
    """
    content = Path(netlist_path).read_text(encoding="utf-8", errors="replace")

    modules = re.findall(r"^\s*module\s+([A-Za-z_][\w]*)", content, re.MULTILINE)

    # 统计单元实例：一次正则扫描（同时充当「模块被实例化次数」的计数表）
    cell_types = {}
    # 匹配 cell_type inst_name ( ... ) 的形式
    for m in re.finditer(r"^\s*([A-Za-z_][\w]*)\s+([A-Za-z_][\w]*)\s*\(", content, re.MULTILINE):
        cell = m.group(1)
        cell_types[cell] = cell_types.get(cell, 0) + 1

    # DFF / SFF / latch 类型（sky130 命名特征）
    dff_types = {}
    for cell, cnt in cell_types.items():
        if re.search(r"(dfr|dfx|edf|sdfr|sdfx|sedf|dlx|dlr|sdl)", cell, re.IGNORECASE):
            dff_types[cell] = cnt

    # 顶层模块：被实例化次数最少的模块，通常就是最外层（无上层引用 -> 0 次）
    top_module = ""
    min_ref = None
    for mod in modules:
        refs = cell_types.get(mod, 0)
        if min_ref is None or refs < min_ref:
            min_ref = refs
            top_module = mod

    return {
        "modules": modules,
        "top_module": top_module,
        "dff_types": dff_types,
        "cell_types": cell_types,
        "port_count": content.count("input ") + content.count("output "),
    }


def find_scan_flops(dff_types):
    """从 DFF 类型中区分可扫描(SFF)与不可扫描(DFF)单元。"""
    sff = {}
    dff = {}
    for cell, cnt in dff_types.items():
        # sky130 扫描 FF 以 s 开头（sdfrtp/sdfxtp/sedf 等）
        base = cell.split("__")[-1] if "__" in cell else cell
        if base.startswith("s"):
            sff[cell] = cnt
        else:
            dff[cell] = cnt
    return dff, sff

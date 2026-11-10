# -*- coding: utf-8 -*-
"""
网表 ECO 的 LLM 兜底：规则推不出修复动作时，让 LLM 在**结构化候选点**里选。

## 为什么需要这一层

确定性 ECO（`lec_verifier.derive_netlist_eco`）是**形态库**：
  · 时钟穿组合门（DFTR1）
  · ICG 的 TE/SCE 被功能组合链占据（DFTR9）

比赛给的是新 case，缺陷形态可以不重样 —— 时钟穿两级门后接 LATCH、ICG 的 CP 端
被常量钳住、门是 OR 而不是 AND、使能来自常量而不是组合链……形态库漏一个，
表现就是「DRC 一直在、怎么补配置都不降」，而且**不报错**（静默失败）。

## 边界：LLM 只做「选择」，不做「生成」

LLM 能看到的动作空间是候选点清单（由代码从网表扫出），能输出的只有：
  · 选哪个候选点（必须是清单里的 net/pin，凭空写的直接丢弃）
  · A1 接什么（顶层端口 或 TIE1）
  · S 接什么（强制等于测试模式端口）
新增单元只用库里确认存在的 mux2 / conb_1。这样「没见过的现象」能覆盖，
而「LLM 编出网表里不存在的引脚」这类事故被结构挡掉。
"""
from __future__ import annotations

import json
import re

import lec_verifier as lv

_MAX_POINTS = 6          # 交给 LLM 的候选点上限（控 token）
_MAX_FIXES = 4


def _brief(fix):
    """LLM 的 why 可能写一整段推理，只取前 40 字进注释。"""
    txt = str((fix or {}).get("why") or "").replace("\n", " ").strip()
    return (txt[:40] + "…") if len(txt) > 40 else txt


def _paths(net, prefix):
    return lv._fp(prefix, net) if prefix else net


def _join(pfx, name):
    """层次前缀 + 局部名；pfx 已去尾斜杠，这里补齐分隔符。"""
    return f"{pfx}/{name}" if pfx else name


def build_candidates(netlist_text, lib_meta, top=None, limit=_MAX_POINTS):
    """扫出「可能是缺陷根因」的结构化候选点。

    与 derive_* 的区别：这里**不做判定**，只把「FF 时钟/ICG 使能的驱动链」
    摊平成清单，判定交给规则或 LLM。返回的每个点都带完整的层次路径，
    生成命令时不需要再推前缀。
    """
    if not netlist_text:
        return {}
    seq = lv._seq_cells(lib_meta)
    insts = lv._parse_insts(netlist_text, lib_meta)
    prefixes = lv._hierarchy_prefixes(insts, lib_meta, top)

    drivers = {}
    for mod, inst, cell, pins in insts:
        meta = (lib_meta or {}).get(cell) or {}
        for pin in (meta.get("out_pins") or set()):
            net = pins.get(pin)
            if net:
                drivers.setdefault((mod, net), []).append((inst, pin, cell))

    clock_pts, icg_pts = [], []
    seen_clock = set()
    for mod, inst, cell, pins in insts:
        meta = (lib_meta or {}).get(cell) or {}
        if mod not in prefixes:
            continue
        pfx = prefixes[mod]

        # ── 时钟组：时序单元 CLK 所接 net 的驱动源
        if meta.get("seq"):
            for pin in (meta.get("clock_pins") or set()):
                net = pins.get(pin)
                if not net or (mod, net) in seen_clock:
                    continue
                for dinst, dpin, dcell in drivers.get((mod, net), []):
                    if dcell in seq:
                        continue                      # 上一级 FF：正常时钟树
                    if lv._is_icg_cell(dcell, (lib_meta or {}).get(dcell) or {}):
                        continue                      # ICG：走 ICG 候选
                    seen_clock.add((mod, net))
                    clock_pts.append({
                        "point": "clock",
                        "target": _paths(net, pfx),
                        "driver_pin": _paths(f"{dinst}/{dpin}", pfx),
                        "driver_cell": dcell,
                        "sample_ff_pin": _paths(f"{inst}/{pin}", pfx),
                    })
                    break

        # ── ICG 的测试使能端：TE/SCE 被「非端口、非常量」驱动即候选
        if lv._is_icg_cell(cell, meta):
            ins = [p for p in (meta.get("in_pins") or set())
                   if p not in (meta.get("out_pins") or set())]
            te_pin = next((p for p in ins
                           if re.match(r"(?i)^(TE|SCE|SE|TEST_EN|TM)$", p)), None)
            if not te_pin:
                continue
            te_net = pins.get(te_pin)
            if not te_net:
                continue
            drv = drivers.get((mod, te_net), [])
            if drv:
                dinst, dpin, dcell = drv[0]
                icg_pts.append({
                    "point": "icg",
                    "target": _paths(f"{inst}/{te_pin}", pfx),
                    "te_net": _paths(te_net, pfx),
                    "driver_pin": _paths(f"{dinst}/{dpin}", pfx),
                    "driver_cell": dcell,
                })
            else:
                icg_pts.append({
                    "point": "icg",
                    "target": _paths(f"{inst}/{te_pin}", pfx),
                    "te_net": _paths(te_net, pfx),
                    "driver_pin": "",
                    "driver_cell": "port-or-const",
                })

    return {"clock": clock_pts[:limit], "icg": icg_pts[:limit],
            "seq_cells": sorted(seq)[:8]}


_SYSTEM = """你在做 IC 可测性设计（DFT）的网表缺陷修复。
给定 DRC 违例统计、设计的顶层端口、以及从网表扫出的「可疑候选点」，你要判断违例
根因落在哪个候选点上，并选择修复动作。

修复约定（与题目一致，必须遵守）：
- 在出问题的路径上插入一个 2 选 1 MUX：A0 = 原逻辑（测试模式=0 时透传，保证功能
  等价），A1 = 测试逻辑，S = 测试模式端口。
- 时钟类（DFTR1，时钟不可控）：A1 接扫描时钟端口，或 TIE1。
- ICG 使能类（DFTR9，时钟不活跃）：A1 接 TIE1，让测试态 TE 恒开。
- 绝对不能改 FF 的 CLK/复位引脚连接，不能删除原功能逻辑，不能用规则豁免绕过。

只能输出 JSON：
{"fixes":[{"point":"clock|icg","target":"候选点清单里的 target 原文",
           "a1":"<顶层端口名 或 TIE1>","s":"<测试模式端口名>","why":"一句话"}]}
target 必须逐字来自候选清单；不确定就输出 {"fixes":[]}。"""


def _user_prompt(parsed, cand, ports, mode_port, clock_port):
    drc = {k: v for k, v in (parsed.get("drc_rules") or {}).items()
           if str(k).startswith("DFTR")}
    payload = {
        "drc": drc or parsed.get("drc_rules") or {},
        "total_violations": parsed.get("total_violations"),
        "test_mode_port": mode_port,
        "scan_clock_port": clock_port or None,
        "top_ports": ports[:40],
        "candidates": {k: cand[k] for k in ("clock", "icg") if cand.get(k)},
    }
    return ("下面是一次扫描 DRC 的结果与候选点，请判断根因并给出修复动作：\n"
            + json.dumps(payload, ensure_ascii=False, indent=1))


def plan_eco(task, netlist_text, lib_meta, parsed, mode_port, clock_port,
             ports, llm, top=None, log=print):
    """让 LLM 选修复动作并落成 ECO 命令；不可信时返回 None。

    Returns: {"lines": [...], "groups": [...], "reason": str} 或 None
    """
    if not llm or not getattr(llm, "available", False) or not mode_port:
        return None
    cand = build_candidates(netlist_text, lib_meta, top)
    if not (cand.get("clock") or cand.get("icg")):
        return None

    mux = lv._pick_lib_cell(list(lib_meta.keys()), lv._MUX_NAME_HINTS)
    if not mux:
        return None
    conb = lv._pick_lib_cell(list(lib_meta.keys()), ("conb", "tie", "constant"))
    port_set = set(ports or [])

    try:
        data = llm.chat_json(_SYSTEM,
                             _user_prompt(parsed, cand, ports, mode_port, clock_port),
                             max_tokens=700)
    except Exception as e:                                   # noqa: BLE001
        log(f"  ⚠ ECO 候选判定失败（{type(e).__name__}），跳过网表修复")
        return None
    fixes = (data or {}).get("fixes") if isinstance(data, dict) else None
    if not isinstance(fixes, list) or not fixes:
        return None

    # target -> 候选点（严格白名单：LLM 写的 target 必须在这里面）
    by_target = {}
    for p in (cand.get("clock") or []) + (cand.get("icg") or []):
        by_target[p["target"]] = p

    existing = {i for _m, i, _c, _p in lv._parse_insts(netlist_text, lib_meta)}
    lines, groups = [], []
    for f in fixes[:_MAX_FIXES]:
        if not isinstance(f, dict):
            continue
        pt = by_target.get(str(f.get("target") or "").strip())
        if pt is None:
            log(f"  · 丢弃 ECO 建议：target 不在候选清单 {f.get('target')!r}")
            continue
        s = str(f.get("s") or "").strip()
        if s != mode_port:
            log(f"  · 丢弃 ECO 建议：S 端 {s!r} 不是测试模式端口 {mode_port!r}")
            continue
        a1_raw = str(f.get("a1") or "").strip()
        tie1 = a1_raw.upper() in ("TIE1", "TIE_1", "CONST1", "1")
        if not tie1 and a1_raw not in port_set:
            log(f"  · 丢弃 ECO 建议：A1 {a1_raw!r} 既不是顶层端口也不是 TIE1")
            continue
        if pt["point"] == "icg" and not tie1:
            # ICG 的 TE 必须恒开；接扫描时钟只会让 TE 跟着时钟跳
            log("  · 丢弃 ECO 建议：ICG 使能只能接 TIE1")
            continue

        if pt["point"] == "clock":
            detach = pt["driver_pin"]
            if not detach:
                continue
            base = pt["target"].rsplit("/", 1)[-1]
            pfx = pt["target"].rsplit("/", 1)[0] if "/" in pt["target"] else ""
            src = _join(pfx, f"{base}_src")
            mux_i = lv._fresh_inst(pfx, "eco_llmmux", existing)
            existing.add(mux_i)
            lines += [f"# ECO(LLM)：{pt['driver_cell']} 驱动时钟 {pt['target']}，"
                      f"A1={a1_raw}（{_brief(f)}）",
                      f"disconnect_net  {pt['target']}  {detach}",
                      f"add_net         {{{src}}}",
                      f"add_inst        {mux_i}  {mux}",
                      f"connect_net     {src}  {{{detach}  {mux_i}/A0}}",
                      f"connect_net     {pt['target']}  {{{mux_i}/X}}",
                      f"connect_pin     -from {a1_raw}  -to {mux_i}/A1",
                      f"connect_pin     -from {s}  -to {mux_i}/S"]
            groups.append({"kind": "clock_path",
                           "module": pt["target"].rsplit("/", 1)[0],
                           "driver": detach, "target": pt["target"],
                           "ffs": None, "source": "llm"})
        else:
            if not conb:
                continue
            te_net = pt["te_net"]
            base = te_net.rsplit("/", 1)[-1]
            pfx = te_net.rsplit("/", 1)[0] if "/" in te_net else ""
            tsel, hi = _join(pfx, f"{base}_tsel"), _join(pfx, f"{base}_hi")
            mux_i = lv._fresh_inst(pfx, "eco_llmmux", existing)
            existing.add(mux_i)
            tie_i = lv._fresh_inst(pfx, "eco_tie1", existing)
            existing.add(tie_i)
            # A0 接「原 net」而不是它的驱动 pin：net 名才是原连接的语义身份，
            # 且断开只发生在 SCE 一侧，net 仍由原功能逻辑驱动（功能态逐拍等价）。
            a0 = te_net
            lines += [f"# ECO(LLM)：ICG 使能 {pt['target']} 被 {pt['driver_cell']} 占据，"
                      f"测试态接 TIE1（{_brief(f)}）",
                      f"disconnect_net  {te_net}  {pt['target']}",
                      f"add_net         {tsel}",
                      f"add_net         {hi}",
                      f"add_inst        {mux_i}  {mux}",
                      f"add_inst        {tie_i}  {conb}",
                      f"connect_net     {tsel}  {{{mux_i}/X  {pt['target']}}}",
                      f"connect_net     {a0}  {mux_i}/A0",
                      f"connect_net     {hi}  {{{tie_i}/HI  {mux_i}/A1}}",
                      f"connect_pin     -from {s}  -to {mux_i}/S"]
            groups.append({"kind": "icg_te",
                           "module": pt["target"].rsplit("/", 1)[0],
                           "driver": pt["driver_pin"] or pt["te_net"],
                           "target": pt["target"], "ffs": None, "source": "llm"})

    if not groups:
        return None
    seen, uniq = set(), []
    for ln in lines:                       # 去重：同一 target 被建议两次时只留一份
        if ln.startswith("#"):
            uniq.append(ln)
            continue
        key = ln
        if key in seen:
            continue
        seen.add(key)
        uniq.append(ln)
    return {"groups": groups, "lines": uniq,
            "reason": f"LLM 依据 DRC 与候选点判定 {len(groups)} 处网表缺陷"}

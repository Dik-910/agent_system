#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM 客户端封装
统一封装 OpenAI 兼容接口调用，支持 JSON 抽取与重试。
"""

import json
import os
import re
import sys
import time

try:
    from openai import OpenAI
    _HAS_OPENAI = True
except ImportError:
    OpenAI = None
    _HAS_OPENAI = False


def openai_version():
    """
    返回已安装的 openai SDK 版本号（取不到返回 ""）。

    为什么需要暴露它：平台构建日志显示实际装到的是 **openai 3.37.0**（`>=1.0.0`
    放开后由镜像源解析），而组委会的参考示例 pin 的是 `openai==1.12.0` —— 
    大版本差异。测试环境又没有注入 key，LLM 路径从没被真正跑过，
    所以必须是「能看见版本」的，出问题时才能立刻判断是不是 SDK 变了。
    """
    try:
        import openai
        return getattr(openai, "__version__", "") or ""
    except Exception:                                        # noqa: BLE001
        return ""

try:
    from . import config
except ImportError:
    import config


# 剩余时间低于该值就不再发起新的 LLM 调用：发出去也来不及返回，纯属浪费预算。
#
# 语义澄清（重要）：这是「客观来不及」的下限，**不是**「预判放弃」的开关。
#   · 预运行阶段现在只有一次有价值的 LLM 调用（生成 dofile），
#     实测单次 48.1~107.2s（tools/_llm_latency.py，真实 deepseek-v4-pro）；
#   · 预运行截止线允许它用到预算的 60%（150s case → 90s），
#     即第一次调用发起时剩余时间 ≈90s，远高于本阈值 —— 调用一定会发出去；
#   · 本阈值只在「前面已经耗掉绝大部分时间、剩下的连一次调用都放不下」时生效。
#
# 取 20s 而非 5s 的理由：单次调用最短也要 48s，剩余 8s 还发出去等于必然超时 + 白等。
_MIN_CALL_S = 20.0

# 「reasoning 吃光 max_tokens」时的自动重试上限（token）。
# 实测 deepseek-v4-pro 单次思考已用 2000+ tokens，正文另需 1000~2000，
# 给到 30000 足够任何本项目的任务；有天花板是为了避免病态输入把调用拖爆。
_REASONING_RETRY_CAP = 30000

# 流式调用的「进度守护」比例：用掉这次调用预算的 80% 还一个字正文都没收到，
# 就主动收手。依据（2026-10-26 实测，deepseek-v4-pro-0813，prompt 5574 tokens）：
#   · 同一 prompt，max_tokens=4000 -> 61.1s 全花在 reasoning，正文 0 字符
#   · 同一 prompt，max_tokens=30000 -> 260.2s，reasoning 16451，正文 1437 字符
# 也就是说「慢」是模型在思考，且我们无法从外部知道它要想多久。非流式只能
# 干等到超时；流式至少能看见 reasoning 在涨、正文还没开始，从而提前放弃，
# 把剩下的时间还给工具轮。
_STREAM_GUARD_RATIO = 0.8

# 思考开关是否被当前网关/模型拒绝过（进程内记住，避免每次都白撞一次 400）。
_THINKING_UNSUPPORTED = False


def _thinking_options(thinking=None):
    """要随请求下发的「思考模式」参数。

    依据百炼文档：deepseek-v4-pro-0813 等模型**默认开启思考**，通过
    `enable_thinking: false` 关闭；`reasoning_effort` 只调强度（low/high/max）。
    这类参数不是 OpenAI 标准字段，SDK 里必须走 extra_body。

    Args:
        thinking: True/False 按**本次调用**强制开/关；None 用全局配置
            `config.LLM_ENABLE_THINKING`。

    为什么要按调用区分（2026-10-28）：本系统的两类 LLM 调用对「想清楚」的
    需求差得很远 ——
      · **理解**（读任务书语义、判断哪段是描述哪段是配置要求）：需要推理，
        开思考质量明显更稳，代价 ~60s；
      · **计划**（在已知字段清单里做结构化决策）：是填空题不是解答题，
        关思考 6~7s 就够，开思考纯属浪费。
    全局开关只能一刀切，于是要么全慢要么全糙。
    """
    if _THINKING_UNSUPPORTED:
        return {}
    want = config.LLM_ENABLE_THINKING if thinking is None else bool(thinking)
    opts = {}
    if not want:
        opts["enable_thinking"] = False
    if config.LLM_REASONING_EFFORT:
        opts["reasoning_effort"] = config.LLM_REASONING_EFFORT
    return opts


def _is_param_rejection(exc):
    """判断异常是不是「不认识这个参数」（换取法重试）。"""
    msg = str(exc).lower()
    if not any(k in msg for k in ("enable_thinking", "reasoning_effort",
                                  "unknown parameter", "unrecognized", "invalid parameter")):
        return False
    return any(k in msg for k in ("400", "invalid", "unknown", "unrecognized",
                                  "unsupported", "bad request"))


_TRACE_CAP = 20000


def _trace_reasoning(reasoning, note=""):
    """
    把模型这次「想了什么」落到文件里（`SCAN_AGENT_LLM_TRACE=/path/to/log`）。

    为什么要这么做：我们之前只能从日志看到「思考约 19179 字符」，
    完全不知道它在想什么，于是只能猜「是不是 prompt 写坏了」。
    落盘后可以直接读原文 —— 是反复自我纠错、还是在重算网表、还是在纠结规则语义，
    一眼就能看出来，也才有依据继续压缩 prompt。
    """
    path = os.environ.get("SCAN_AGENT_LLM_TRACE")
    if not path or not reasoning:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== {time.strftime('%H:%M:%S')} {note} "
                     f"({len(reasoning)} 字符) =====\n")
            fh.write(reasoning[:_TRACE_CAP])
            if len(reasoning) > _TRACE_CAP:
                fh.write(f"\n…（截断，原始 {len(reasoning)} 字符）\n")
            fh.write("\n")
    except Exception:                                        # noqa: BLE001
        pass


class _StreamUnsupported(Exception):
    """网关/SDK 不支持 stream 或 stream_options（例如自建兼容层）。"""


class LLMClient:
    """基于 OpenAI 兼容协议的 LLM 客户端。"""

    def __init__(self):
        self.client = None
        # 调用统计（用于在容器日志里报告 LLM 阶段耗时，便于事后归因）
        self.calls = 0
        self.skipped = 0
        self.failures = 0
        self.guarded = 0          # 流式守护提前收手的次数
        self.partial = 0          # 返回「部分正文」的次数（被截断但有内容）
        self.retries = 0          # 瞬时错误重试次数
        self.fail_kinds = {}      # 失败按异常类型计数，便于事后归因
        self.last_cost = 0.0      # 最近一次**成功**调用的耗时（用于判断「来不来得及」）
        self.last_usage = {}      # 最近一次的 token 用量（诊断用）
        self.time_spent = 0.0
        # 截止时间（epoch 秒）；None 表示不限制。由调用方按「剩余预算」设置，
        # 保证任何一次 LLM 调用都不会把 case 的 wall time 预算吃光。
        self._deadline = None
        if config.llm_available() and _HAS_OPENAI:
            self.client = OpenAI(
                api_key=config.LLM_CONFIG["api_key"],
                base_url=config.LLM_CONFIG["base_url"],
                # 硬超时 + 不自动重试：SDK 默认 600s/重试 2 次，最坏能阻塞 30 分钟，
                # 对 150s 预算的 case 是致命的（详见 config.LLM_TIMEOUT 注释）。
                timeout=config.LLM_TIMEOUT,
                max_retries=config.LLM_MAX_RETRIES,
            )

    @property
    def available(self) -> bool:
        return self.client is not None

    # ------------------------------------------------------------------
    # 截止时间控制
    # ------------------------------------------------------------------
    def set_deadline(self, deadline):
        """设置/清除 LLM 调用的截止时间（epoch 秒，None = 不限制）。"""
        self._deadline = deadline

    def time_left(self):
        """距截止时间还剩多少秒；未设置截止时间时返回 None。"""
        if self._deadline is None:
            return None
        return self._deadline - time.time()

    def stats_line(self) -> str:
        """一行调用统计，用于写进容器日志。"""
        extra = ""
        if self.guarded:
            extra += f" / 提前收手 {self.guarded} 次"
        if self.partial:
            extra += f" / 部分输出 {self.partial} 次"
        if self.retries:
            extra += f" / 重试 {self.retries} 次"
        if self.fail_kinds:
            kinds = ",".join(f"{k}×{v}" for k, v in
                             sorted(self.fail_kinds.items(), key=lambda kv: -kv[1])[:3])
            extra += f" / 失败类型 {kinds}"
        return (f"LLM 调用 {self.calls} 次 / 因超时跳过 {self.skipped} 次 / "
                f"失败 {self.failures} 次{extra} / 累计 {self.time_spent:.1f}s")

    def chat(self, system_prompt, user_prompt, temperature=None, max_tokens=None,
             thinking=None):
        """
        发起一次对话补全，返回文本内容。

        与 SDK 裸调用的区别（必须保留）：
          1. 每次请求显式传 timeout，且受 set_deadline() 设置的剩余时间约束，
             保证「单次调用 + 重试」不会越过本 case 的内部截止时间；
          2. 剩余时间不足时**直接返回 None**，不再发起调用 —— 调用方据此走
             确定性兜底（规则模板 / spec_fixer），而不是白等。

        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            temperature: 温度（可选，覆盖默认）
            max_tokens: 最大 token 数（可选）
            thinking: True/False 本次调用强制开/关思考；None 用全局配置。
                见 `_thinking_options` 的说明 —— 理解类调用开、填空类调用关。

        Returns:
            str 或 None（失败/超时/时间不足时）
        """
        if not self.available:
            return None

        # 时间护栏：来不及返回就不要发。
        # 阈值取「上次成功调用的实测耗时 ×1.05」与 _MIN_CALL_S 的较大值 ——
        # 固定 20s 太乐观：计划调用实测 ~30s，剩余 25s 时发出去必然被流式守护砍掉。
        left = self.time_left()
        need = max(_MIN_CALL_S, self.last_cost * 1.05 if self.last_cost else 0.0)
        if left is not None and left < need:
            self.skipped += 1
            hint = f"（上次实测 {self.last_cost:.0f}s）" if self.last_cost else ""
            print(f"  [LLM] 剩余时间 {left:.1f}s 不足一次调用{hint}，"
                  f"跳过本次调用（改用确定性路径）", file=sys.stderr)
            return None

        temperature = config.LLM_CONFIG["temperature"] if temperature is None else temperature
        max_tokens = config.LLM_CONFIG["max_tokens"] if max_tokens is None else max_tokens

        req_timeout = config.LLM_TIMEOUT
        if left is not None:
            req_timeout = max(_MIN_CALL_S, min(req_timeout, left))

        t0 = time.time()
        req = dict(
            model=config.LLM_CONFIG["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=req_timeout,
        )
        _opts = _thinking_options(thinking)
        if _opts:
            req["extra_body"] = dict(_opts)
        content, meta = "", {}
        failure = None
        try:
            # 最多两次：第一次失败若属瞬时错误（连接重置 / 502 / 429 …）且剩余
            # 时间还够，再试一次。以前是 0 重试 —— 一次瞬时抖动就丢掉整轮 LLM，
            # 对 12 个 case 的评测 = 白丢一个 case 的 LLM 决策。
            for attempt in (1, 2):
                try:
                    content, meta = self._dispatch(req, req_timeout)
                    failure = None
                    break
                except Exception as e:                          # noqa: BLE001
                    failure = e
                    if not self._should_retry(e, attempt, t0):
                        break
                    self.retries += 1
                    print(f"  [LLM] {type(e).__name__}（{str(e)[:60]}），"
                          f"剩余时间够，重试一次", file=sys.stderr)
        finally:
            self.time_spent += time.time() - t0

        if failure is not None:
            self.failures += 1
            kind = type(failure).__name__
            self.fail_kinds[kind] = self.fail_kinds.get(kind, 0) + 1
            print(f"  [LLM] 调用失败（超时 {req_timeout:.0f}s，{kind}）：{failure}",
                  file=sys.stderr)
            return None

        # 记下这次真实耗时与 token 用量：下一次调用前据此判断「来不来得及」，
        # 出问题时也能从日志看出慢在 reasoning 还是 prompt。
        self.last_cost = time.time() - t0
        self.last_usage = meta.get("usage") or {}

        content = (content or "").strip()
        if not content:
            # 【已删除「同值重试」】旧实现在正文为空且 finish=length 时用
            # max_tokens*2 再发一次，而 _REASONING_RETRY_CAP 恰好等于默认上限
            # 30000 —— bigger == max_tokens，等于拿一模一样的参数又跑一遍，
            # 白烧一倍时间（2026-10-26 实测单次 260s）。现在直接放弃，
            # 把时间留给下一轮或工具。
            if meta.get("finish") == "length":
                print(f"  [LLM] reasoning 占满 {max_tokens} tokens（正文为空），"
                      f"放弃本次调用", file=sys.stderr)
            return None

        self.calls += 1
        if meta.get("partial"):
            self.partial += 1
            print(f"  [LLM] 时间到，采用已生成的部分输出（{len(content)} 字符）",
                  file=sys.stderr)

        if os.environ.get("SCAN_AGENT_DEBUG_LLM") == "1":
            print(f"  [LLM-debug] finish={meta.get('finish')} "
                  f"reasoning_chars={meta.get('reasoning_chars')} "
                  f"content_len={len(content)} partial={meta.get('partial')} "
                  f"cost={self.last_cost:.0f}s usage={self.last_usage}",
                  file=sys.stderr)
        return content

    # ------------------------------------------------------------------
    # 两种调用形态：流式（默认）与一次性（兜底）
    # ------------------------------------------------------------------
    @staticmethod
    def _delta_text(delta):
        """从流式 delta 里取出 (正文片段, reasoning 片段)。

        不同网关字段名不一：DashScope/DeepSeek 用 reasoning_content，
        有些兼容层塞在 model_extra 里，这里一并兜住。
        """
        if delta is None:
            return "", ""
        piece = getattr(delta, "content", None) or ""
        reasoning = getattr(delta, "reasoning_content", None) or ""
        if not reasoning:
            extra = getattr(delta, "model_extra", None) or {}
            reasoning = extra.get("reasoning_content") or ""
        return piece, reasoning

    def _call_streaming(self, req, req_timeout):
        """流式调用：边收边看进度，注定跑不完就提前收手。

        返回 (content, meta)。content 可能为空或为「部分正文」（被截断），
        调用方据此决定是否走补齐逻辑。
        """
        t0 = time.time()
        guard_at = t0 + req_timeout * _STREAM_GUARD_RATIO
        hard_at = t0 + req_timeout * 0.98
        parts, reasoning_chars, finish, partial = [], 0, None, False
        usage = {}
        trace_on = bool(os.environ.get("SCAN_AGENT_LLM_TRACE"))
        reasoning_parts = []

        try:
            stream = self.client.chat.completions.create(
                **req, stream=True, stream_options={"include_usage": True})
        except Exception as exc:                                # noqa: BLE001
            msg = str(exc).lower()
            if any(k in msg for k in ("stream_options", "unsupported", "not support",
                                      "invalid_request", "unknown parameter")):
                raise _StreamUnsupported(str(exc))
            raise

        try:
            chunk_iter = iter(stream)
        except TypeError:
            # 有的兼容层/自建网关无视 stream=True，直接返回完整响应对象。
            # 这时不能当失败处理 —— 按非流式结果解析即可。
            return self._parse_blocking_response(stream)

        try:
            for chunk in chunk_iter:
                # 有的网关把 usage 放在最后一个 chunk（stream_options.include_usage）
                u = getattr(chunk, "usage", None)
                if u is not None:
                    usage = {
                        "prompt": getattr(u, "prompt_tokens", None),
                        "completion": getattr(u, "completion_tokens", None),
                        "reasoning": ((getattr(u, "completion_tokens_details", None)
                                       and getattr(u.completion_tokens_details,
                                                   "reasoning_tokens", None))),
                    }
                for ch in (getattr(chunk, "choices", None) or []):
                    piece, rc = self._delta_text(getattr(ch, "delta", None))
                    if rc:
                        reasoning_chars += len(rc)
                        if trace_on and reasoning_chars <= _TRACE_CAP * 2:
                            reasoning_parts.append(rc)
                    if piece:
                        parts.append(piece)
                    if getattr(ch, "finish_reason", None):
                        finish = ch.finish_reason
                now = time.time()
                if now >= guard_at and not parts:
                    # 一个字正文都没有：继续等只是把整段时间丢光
                    self.guarded += 1
                    print(f"  [LLM] 已用 {now - t0:.0f}s 仍无正文"
                          f"（思考约 {reasoning_chars} 字符），提前收手", file=sys.stderr)
                    break
                if now >= hard_at:
                    if parts:
                        partial = True
                        print(f"  [LLM] 到点截断，保留已收到的 {len(''.join(parts))} 字符",
                              file=sys.stderr)
                    break
        finally:
            close = getattr(stream, "close", None)
            if close:                                            # 提前 break 时必须关
                try:
                    close()
                except Exception:                                # noqa: BLE001
                    pass

        if reasoning_parts:
            _trace_reasoning("".join(reasoning_parts), note="stream")

        content = "".join(parts)
        meta = {"finish": finish, "reasoning_chars": reasoning_chars,
                "partial": partial or (bool(content) and finish is None),
                "usage": usage}
        return content, meta

    @staticmethod
    def _rename_max_tokens(req):
        """SDK 大版本差异：新版把 max_tokens 改名 max_completion_tokens。"""
        if "max_tokens" in req:
            req["max_completion_tokens"] = req.pop("max_tokens")

    def _dispatch(self, req, req_timeout):
        """带上「思考开关被拒 → 去掉重试」的外壳。"""
        global _THINKING_UNSUPPORTED
        try:
            return self._dispatch_once(req, req_timeout)
        except Exception as e:                                  # noqa: BLE001
            if req.get("extra_body") and _is_param_rejection(e):
                _THINKING_UNSUPPORTED = True
                req.pop("extra_body", None)
                print("  [LLM] 该模型不接受 enable_thinking/reasoning_effort，"
                      "去掉后重试（后续调用不再发送）", file=sys.stderr)
                return self._dispatch_once(req, req_timeout)
            raise

    def _dispatch_once(self, req, req_timeout):
        """
        发一次请求：流式优先，网关不支持流式时退回一次性调用。

        为什么单独抽出来（旧实现的坑）：`_StreamUnsupported` 与 `TypeError`
        两个 except 分支是并列的 —— 如果「不支持流式 → 走一次性调用」这一步又
        抛 TypeError，外层的 TypeError 分支会把请求**再次发给流式接口**，
        于是又撞 _StreamUnsupported，最终落到最外层 except 判失败。
        这里把顺序理清楚：流式（含改名重试）→ 失败则一次性（含改名重试）。
        """
        try:
            try:
                return self._call_streaming(req, req_timeout)
            except TypeError as te:
                if "max_tokens" not in str(te):
                    raise
                self._rename_max_tokens(req)
                return self._call_streaming(req, req_timeout)
        except _StreamUnsupported:
            print("  [LLM] 网关不支持流式，退回一次性调用", file=sys.stderr)

        try:
            return self._call_blocking(req)
        except TypeError as te:
            if "max_tokens" not in str(te):
                raise
            self._rename_max_tokens(req)
            return self._call_blocking(req)

    def _should_retry(self, exc, attempt, t0):
        """
        是否值得重试一次。

        只重试**瞬时性**错误：连接/超时/限流/5xx。
        鉴权(401)、欠费(402/403)、参数(400) 重试没有意义，只会白烧时间 ——
        这也是「是不是欠费」能在日志里一眼看出来的原因（失败类型会分类打印）。
        """
        if attempt >= 2:
            return False
        name = type(exc).__name__.lower()
        msg = str(exc).lower()
        transient = ("timeout" in name or "connection" in name or "ratelimit" in name
                     or "internalserver" in name or "serviceunavailable" in name
                     or any(k in msg for k in ("502", "503", "504", "connection reset",
                                               "connection aborted", "temporarily",
                                               "overloaded", "try again")))
        if not transient:
            return False
        left = self.time_left()
        if left is None:
            return True
        spent = time.time() - t0
        # 重试必须留够「上一次实际耗时」的 1.2 倍，否则重试也会被守护砍掉
        need = max(spent * 1.2, (self.last_cost * 1.2) if self.last_cost else 0.0, 30.0)
        return left >= need

    def _call_blocking(self, req):
        """一次性调用（流式不被支持时兜底）。"""
        return self._parse_blocking_response(
            self.client.chat.completions.create(**req))

    @staticmethod
    def _parse_blocking_response(response):
        """把一次性响应解析成 (content, meta)。"""
        choice = response.choices[0]
        content = choice.message.content or ""
        reasoning = getattr(choice.message, "reasoning_content", None) or ""
        _trace_reasoning(reasoning, note="blocking")
        u = getattr(response, "usage", None)
        return content, {
            "finish": getattr(choice, "finish_reason", None),
            "reasoning_chars": len(reasoning),
            "partial": False,
            "usage": {
                "prompt": getattr(u, "prompt_tokens", None) if u else None,
                "completion": getattr(u, "completion_tokens", None) if u else None,
                "reasoning": ((getattr(u, "completion_tokens_details", None)
                               and getattr(u.completion_tokens_details,
                                           "reasoning_tokens", None)) if u else None),
            },
        }

    def chat_json(self, system_prompt, user_prompt, max_tokens=None, thinking=None):
        """
        发起对话并解析为 JSON，失败返回 None。

        自动剥离 Markdown 代码块标记（```json ... ```）。

        max_tokens 用于收紧输出上限（纯 JSON 抽取不需要长篇输出，收紧可降低延迟）。
        thinking 见 `chat` 的同名参数。
        """
        content = self.chat(system_prompt, user_prompt, max_tokens=max_tokens,
                            thinking=thinking)
        if content is None:
            return None

        return self._extract_json(content)

    @staticmethod
    def _extract_json(text):
        """从 LLM 返回文本中提取 JSON 对象（容错处理）。"""
        if not text:
            return None

        text = text.strip()
        # 剥离代码块
        code_block = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if code_block:
            text = code_block.group(1).strip()

        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试截取首个 { 到最后一个 } 之间的内容
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass

        return None


# 全局单例
_llm = LLMClient()


def get_llm() -> LLMClient:
    return _llm


def has_useful_time(llm) -> bool:
    """
    剩余时间是否够「一次有价值的 LLM 调用」。

    用于**后续轮次**（工具跑完之后的修复 / 复核）的护栏：平台单次调用实测
    78~148s，剩余不足 config.LLM_MIN_USEFUL_S 时发出去必然超时，
    白等还会挤掉后续轮次 —— 此时应直接走确定性路径。

    未设置截止时间（time_left() is None）时返回 True，不干预无预算场景。
    """
    if llm is None:
        return False
    if not getattr(llm, "available", False):
        return False
    left = llm.time_left() if hasattr(llm, "time_left") else None
    return left is None or left >= config.LLM_MIN_USEFUL_S

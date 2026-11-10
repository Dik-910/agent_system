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


class LLMClient:
    """基于 OpenAI 兼容协议的 LLM 客户端。"""

    def __init__(self):
        self.client = None
        # 调用统计（用于在容器日志里报告 LLM 阶段耗时，便于事后归因）
        self.calls = 0
        self.skipped = 0
        self.failures = 0
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
        return (f"LLM 调用 {self.calls} 次 / 因超时跳过 {self.skipped} 次 / "
                f"失败 {self.failures} 次 / 累计 {self.time_spent:.1f}s")

    def chat(self, system_prompt, user_prompt, temperature=None, max_tokens=None):
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

        Returns:
            str 或 None（失败/超时/时间不足时）
        """
        if not self.available:
            return None

        # 时间护栏：来不及返回就不要发
        left = self.time_left()
        if left is not None and left <= _MIN_CALL_S:
            self.skipped += 1
            print(f"  [LLM] 剩余时间 {left:.1f}s 不足 {_MIN_CALL_S:.0f}s，"
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
        try:
            try:
                response = self.client.chat.completions.create(**req)
            except TypeError as te:
                # SDK 大版本差异兜底：新版把 max_tokens 改名为
                # max_completion_tokens（旧名仍兼容一段时间，但不保证）。
                # 平台实际装到的是 openai 3.37.0，而参考示例 pin 的是 1.12.0，
                # 中间跨了大版本 —— 这里做一次参数名回退，避免整条 LLM 路径失效。
                if "max_tokens" not in str(te):
                    raise
                req.pop("max_tokens", None)
                req["max_completion_tokens"] = max_tokens
                print("  [LLM] 该 SDK 不接受 max_tokens，改用 "
                      "max_completion_tokens 重试一次", file=sys.stderr)
                response = self.client.chat.completions.create(**req)
        except Exception as e:                                  # noqa: BLE001
            self.failures += 1
            print(f"  [LLM] 调用失败（超时 {req_timeout:.0f}s）：{e}", file=sys.stderr)
            return None
        finally:
            self.time_spent += time.time() - t0

        self.calls += 1
        choice = response.choices[0]
        content = choice.message.content

        # 调试开关（默认关闭，生产不打印）：用于诊断「调用成功但内容为空」。
        # 推理型模型（如 deepseek 系列）会先消耗 token 做 reasoning，
        # 若 max_tokens 给小了，reasoning 会把预算吃光、content 为空 ——
        # finish_reason 会显示 length，usage 能看到 reasoning token 占比。
        if os.environ.get("SCAN_AGENT_DEBUG_LLM") == "1":
            u = getattr(response, "usage", None)
            print(f"  [LLM-debug] finish_reason={getattr(choice, 'finish_reason', '?')} "
                  f"usage={u} content_len={len(content or '')}", file=sys.stderr)

        # ---- 推理模型适配：reasoning 吃光 max_tokens -> 正文为空 ----
        #
        # deepseek-v4-pro 是推理模型：思考阶段的 token **也计入 max_tokens**。
        # 当上限不足时返回 finish_reason=length 且 content 为空 ——
        # 这**不是**「模型答不出」，而是「预算没给够」，直接向上层报空会白白
        # 退化到确定性路径（实测任务二曾因此连续 2 个 case 全部空返回）。
        # 因此这里自动放大上限重试一次（上限有天花板，不会无限翻倍）。
        if (not (content or "").strip()
                and getattr(choice, "finish_reason", "") == "length"):
            bigger = min(int(max_tokens or 0) * 2, _REASONING_RETRY_CAP)
            spent = time.time() - t0
            left2 = self.time_left()
            affordable = left2 is None or left2 > spent * 1.2 + _MIN_CALL_S
            if bigger > int(max_tokens or 0) and affordable:
                print(f"  [LLM] reasoning 占满 {max_tokens} tokens（正文为空），"
                      f"以 {bigger} 重试", file=sys.stderr)
                return self.chat(system_prompt, user_prompt,
                                 temperature=temperature, max_tokens=bigger)
            print(f"  [LLM] reasoning 占满 {max_tokens} tokens 且时间不足，"
                  f"放弃本次调用", file=sys.stderr)

        return content

    def chat_json(self, system_prompt, user_prompt, max_tokens=None):
        """
        发起对话并解析为 JSON，失败返回 None。

        自动剥离 Markdown 代码块标记（```json ... ```）。

        max_tokens 用于收紧输出上限（纯 JSON 抽取不需要长篇输出，收紧可降低延迟）。
        """
        content = self.chat(system_prompt, user_prompt, max_tokens=max_tokens)
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

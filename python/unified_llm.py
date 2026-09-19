# -*- coding: utf-8 -*-
"""
unified_llm.py —— 多模型统一接入层（OpenAI 兼容协议）

一个 client 管住所有模型：统一调用、自动切换、用量统计、成本可算。

    pip install requests
    export LLM_BASE_URL="https://你的接口地址/v1"
    export LLM_API_KEY="sk-xxxxxx"

    from unified_llm import UnifiedLLM
    llm = UnifiedLLM(models=["deepseek-v3", "qwen-max", "glm-4-plus"])
    print(llm.chat("用一句话解释什么是向量数据库"))
    print(llm.report())
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    raise SystemExit("缺少依赖，请先执行：pip install requests")


DEFAULT_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.example.com/v1")
DEFAULT_API_KEY = os.getenv("LLM_API_KEY", "")

# 价格表：元 / 百万 token。按你实际的渠道价覆盖即可。
# 这里填的是公开刊例价量级，仅用于本地成本估算，不作为结算依据。
PRICING: Dict[str, Dict[str, float]] = {
    "deepseek-v3":  {"in": 2.0,  "out": 8.0},
    "deepseek-r1":  {"in": 4.0,  "out": 16.0},
    "qwen-max":     {"in": 20.0, "out": 60.0},
    "qwen-plus":    {"in": 4.0,  "out": 12.0},
    "glm-4-plus":   {"in": 50.0, "out": 50.0},
    "kimi-k2":      {"in": 4.0,  "out": 16.0},
}
DEFAULT_PRICE = {"in": 10.0, "out": 30.0}


# ---------------- 错误分类 ----------------
# 切换模型的前提是"判断这个错误值不值得切"。
# 参数错、内容被拒 —— 换成哪一家结果都一样，切过去只是白烧一次额度，所以直接抛出。
# 超时、限流、5xx、以及渠道级问题（key 失效、模型不存在）—— 换一家很可能就成了。

class UnifiedLLMError(Exception):
    """本库所有异常的基类"""


class RetryableError(UnifiedLLMError):
    """临时性 / 渠道级失败，可以重试或切换下一个候选模型"""


class FatalError(UnifiedLLMError):
    """确定性失败，重试与切换都无意义，直接向上抛"""


# 视为「可以重试或切换」的 HTTP 状态码
RETRYABLE_STATUS = {
    401,  # 鉴权失败通常是渠道级问题，换一家可能就好了
    403,
    404,  # 该渠道没有这个模型
    408,  # 请求超时
    409,
    425,
    429,  # 限流
    500, 502, 503, 504,  # 上游故障
}

# 视为「确定性失败」的状态码：请求本身有问题，换谁都一样
FATAL_STATUS = {400, 405, 413, 415, 422}


def classify_status(status: int, model: str, body: str = "") -> Exception:
    """把 HTTP 状态码翻译成对应的异常类型"""
    detail = f"[{model}] HTTP {status}: {body[:200]}"
    if status in FATAL_STATUS:
        return FatalError(detail)
    return RetryableError(detail)


@dataclass
class UsageRecord:
    """单次调用的用量记录"""
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ts: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost(self) -> float:
        p = PRICING.get(self.model, DEFAULT_PRICE)
        return (self.prompt_tokens * p["in"] + self.completion_tokens * p["out"]) / 1_000_000


class UnifiedLLM:
    """多模型统一接入客户端"""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        models: Optional[List[str]] = None,
        timeout: int = 60,
        max_retries: int = 2,
        retry_delay: float = 1.0,
        price_table: Optional[Dict[str, Dict[str, float]]] = None,
    ):
        self.api_key = api_key or DEFAULT_API_KEY
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        if not self.api_key or "example.com" in self.base_url:
            raise ValueError(
                "未配置接口地址或密钥。请传参，或设置环境变量 LLM_BASE_URL / LLM_API_KEY"
            )
        # 候选模型按顺序排列，前一个不可用时自动切下一个
        self.models: List[str] = models or ["deepseek-v3", "qwen-max", "glm-4-plus"]
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        if price_table:
            PRICING.update(price_table)

        self._usage: List[UsageRecord] = []
        self._lock = threading.Lock()

    # ---------------- 同步调用 ----------------

    def chat(
        self,
        prompt: Optional[str] = None,
        messages: Optional[List[Dict[str, str]]] = None,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        failover: bool = True,
        **extra: Any,
    ) -> str:
        """
        发起一次对话。指定了 model 就只用它；
        没指定就按 self.models 顺序尝试，失败自动切换。
        """
        msgs = self._build_messages(prompt, messages)
        candidates = [model] if model else self.models
        last_err: Optional[Exception] = None

        for m in candidates:
            for attempt in range(self.max_retries + 1):
                try:
                    data = self._request(m, msgs, temperature, max_tokens, extra)
                    self._record(m, data.get("usage", {}))
                    return data["choices"][0]["message"]["content"]
                except FatalError:
                    # 请求本身有问题，换一家、再重试都没意义，直接抛出
                    raise
                except Exception as e:  # noqa: BLE001  可重试错误
                    last_err = e
                    if attempt < self.max_retries:
                        time.sleep(self.retry_delay * (attempt + 1))
            if not failover:
                break

        raise RuntimeError(f"所有候选模型均调用失败，最后一次错误：{last_err}")

    def stream(
        self,
        prompt: Optional[str] = None,
        messages: Optional[List[Dict[str, str]]] = None,
        model: Optional[str] = None,
        temperature: float = 0.7,
        **extra: Any,
    ) -> Iterator[str]:
        """流式输出，逐段产出文本"""
        msgs = self._build_messages(prompt, messages)
        m = model or self.models[0]
        payload: Dict[str, Any] = {
            "model": m, "messages": msgs, "temperature": temperature, "stream": True
        }
        payload.update(extra)

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                stream=True,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise RetryableError(f"[{m}] 网络异常：{e}") from e

        with resp:
            if resp.status_code != 200:
                raise classify_status(resp.status_code, m, resp.text)
            for line in resp.iter_lines():
                if not line:
                    continue
                s = line.decode("utf-8")
                if not s.startswith("data:"):
                    continue
                s = s[5:].strip()
                if s == "[DONE]":
                    break
                try:
                    delta = json.loads(s)["choices"][0].get("delta", {})
                except (ValueError, KeyError, IndexError):
                    continue
                if delta.get("content"):
                    yield delta["content"]

    # ---------------- 用量与成本 ----------------

    @property
    def usage(self) -> List[UsageRecord]:
        return list(self._usage)

    def report(self) -> Dict[str, Any]:
        """输出本次进程内的用量与成本汇总"""
        by_model: Dict[str, Dict[str, Any]] = {}
        for u in self._usage:
            b = by_model.setdefault(
                u.model, {"调用次数": 0, "输入": 0, "输出": 0, "成本": 0.0}
            )
            b["调用次数"] += 1
            b["输入"] += u.prompt_tokens
            b["输出"] += u.completion_tokens
            b["成本"] += u.cost

        for v in by_model.values():
            v["成本"] = round(v["成本"], 4)

        return {
            "总调用次数": len(self._usage),
            "总 tokens": sum(u.total_tokens for u in self._usage),
            "总成本_元": round(sum(u.cost for u in self._usage), 4),
            "按模型拆分": by_model,
        }

    def reset_usage(self) -> None:
        """清空累计用量（例如按天归档后重置）"""
        with self._lock:
            self._usage.clear()

    def export_usage(self, path: str) -> str:
        """把用量明细导出成 JSONL，方便落到你的计费或数仓"""
        with open(path, "w", encoding="utf-8") as f:
            for u in self._usage:
                f.write(json.dumps({
                    "ts": u.ts,
                    "model": u.model,
                    "prompt_tokens": u.prompt_tokens,
                    "completion_tokens": u.completion_tokens,
                    "cost": round(u.cost, 6),
                }, ensure_ascii=False) + "\n")
        return path

    # ---------------- 内部方法 ----------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: Optional[int],
        extra: Dict[str, Any],
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        payload.update(extra)

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            # 连不上、读超时、连接被重置 —— 都属于临时性失败
            raise RetryableError(f"[{model}] 网络异常：{e}") from e

        if resp.status_code != 200:
            raise classify_status(resp.status_code, model, resp.text)
        return resp.json()

    def _record(self, model: str, usage: Dict[str, Any]) -> None:
        if not usage:
            return
        with self._lock:
            self._usage.append(UsageRecord(
                model=model,
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            ))

    @staticmethod
    def _build_messages(
        prompt: Optional[str], messages: Optional[List[Dict[str, str]]]
    ) -> List[Dict[str, str]]:
        if messages:
            return messages
        if prompt:
            return [{"role": "user", "content": prompt}]
        raise ValueError("prompt 和 messages 至少要给一个")

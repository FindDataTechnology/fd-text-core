"""嵌入通道：SiliconFlow OpenAI 兼容 /embeddings（与法条线同供应商、同调用形态）。

依赖注入 fetch 实现便于测试；重试策略照搬法条 worker 的经验：
瞬时故障（429/5xx/超时）指数退避重试，确定的 4xx 立即失败（重试修不了载荷）。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Sequence

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class EmbeddingUnavailable(RuntimeError):
    """嵌入上游不可用/不可信。"""


@dataclass
class EmbedResult:
    vectors: list[list[float]]
    usage: dict | None = None


def _default_post(url: str, payload: dict, headers: dict, timeout: int) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace") if exc.fp else ""
        try:
            return exc.code, json.loads(body) if body else {}
        except json.JSONDecodeError:
            return exc.code, {"error": body}


class Embedder:
    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str,
        *,
        timeout: int = 60,
        max_attempts: int = 6,
        post: Callable | None = None,
        sleep: Callable | None = None,
        log: Callable | None = None,
    ) -> None:
        if not (api_base and api_key and model):
            raise ValueError("embedder 需要 api_base / api_key / model")
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_attempts = max_attempts
        self._post = post or _default_post
        self._sleep = sleep or time.sleep
        self._log = log or (lambda *_: None)

    def embed(self, texts: Sequence[str]) -> EmbedResult:
        if not texts:
            return EmbedResult(vectors=[])
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {"model": self.model, "input": list(texts)}
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                status, body = self._post(
                    f"{self.api_base}/embeddings", payload, headers, self.timeout
                )
            except Exception as exc:  # 网络/超时归入可重试
                status, body = None, {"error": f"{type(exc).__name__}: {exc}"}
                last = exc
            if status is not None and 200 <= status < 300:
                return self._parse(body, len(texts))
            retryable = status in RETRYABLE_STATUS or status is None
            msg = f"嵌入请求 status={status} body={str(body)[:200]}"
            if status is not None and 400 <= status < 500 and status not in RETRYABLE_STATUS:
                raise EmbeddingUnavailable(f"{msg}（确定的客户端错误，不重试）")
            if not retryable:
                raise EmbeddingUnavailable(msg)
            last = RuntimeError(msg)
            if attempt < self.max_attempts:
                backoff = min(2 ** (attempt - 1), 30)
                self._log(f"嵌入第 {attempt} 次失败，{backoff}s 后重试: {msg}")
                self._sleep(backoff)
        raise EmbeddingUnavailable(f"嵌入重试耗尽: {last}")

    def _parse(self, body: dict, count: int) -> EmbedResult:
        data = body.get("data") or []
        by_index = {d.get("index"): d.get("embedding") for d in data}
        vectors = [by_index.get(i) for i in range(count)]
        missing = [i for i, v in enumerate(vectors) if not isinstance(v, list) or len(v) == 0]
        if missing:
            raise EmbeddingUnavailable(f"响应缺少第 {missing} 条的向量")
        return EmbedResult(vectors=vectors, usage=body.get("usage"))

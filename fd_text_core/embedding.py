"""嵌入通道：SiliconFlow OpenAI 兼容 /embeddings（与法条线同供应商、同调用形态）。

依赖注入 fetch 实现便于测试；重试策略照搬法条 worker 的经验：
瞬时故障（429/5xx/超时）指数退避重试，确定的 4xx 立即失败（重试修不了载荷）。
"""
from __future__ import annotations

import json
import os
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
        dimensions: int | None = None,
        timeout: int = 60,
        max_attempts: int = 6,
        provider: dict | None = None,
        post: Callable | None = None,
        sleep: Callable | None = None,
        log: Callable | None = None,
    ) -> None:
        if not (api_base and api_key and model):
            raise ValueError("embedder 需要 api_base / api_key / model")
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        # MRL 截断维度：显式下发而非依赖服务端默认。8B 原生 4096 维超出
        # pgvector HNSW 的 2000 维上限，必须截断才能建索引；把截断写成
        # 请求参数，服务端哪天改了默认返回维度会立刻以维度不符暴露。
        self.dimensions = dimensions
        # 上游钉住（OpenAI 兼容网关的 provider 字段，如 {"only": ["X"]}）：
        # 网关若挂了多个都能响应同一模型名的上游，不指定时会在它们之间轮换，
        # 而各家的量化/实现不同 → 同一段文本多次调用返回不同向量（实测 9 次里
        # 3-4 次余弦 <0.999，最低 0.9972）。这会让锚点自检门持续误报，更糟的是
        # 会让库内向量与查询向量来自不同副本、检索静默漂移。钉住单一上游后实测
        # 10 次调用余弦全部 >=0.999951。
        # 传空（默认）则不下发该字段，保持与直连服务商的既有行为一致。
        self.provider = provider
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
        payload: dict = {"model": self.model, "input": list(texts)}
        if self.dimensions:
            payload["dimensions"] = self.dimensions
        if self.provider:
            payload["provider"] = self.provider
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

def provider_from_env() -> dict | None:
    """把 FDTEXT_EMBEDDING_PROVIDER_ONLY 解析为网关的 provider 载荷。

    逗号分隔的名单 -> {"only": [...]}；未配置返回 None（不下发该字段，
    保持直连单一服务商的既有行为）。集中在此解析，避免各构造点各写一遍。
    """
    raw = os.environ.get("FDTEXT_EMBEDDING_PROVIDER_ONLY", "").strip()
    if not raw:
        return None
    names = [x.strip() for x in raw.split(",") if x.strip()]
    return {"only": names} if names else None


def embedder_from_env() -> "Embedder | None":
    """按环境变量构造 Embedder（含上游钉住）；缺凭据返回 None 由调用方降级。"""
    base = os.environ.get("FDTEXT_EMBEDDING_API_BASE", "").strip()
    key = os.environ.get("FDTEXT_EMBEDDING_API_KEY", "").strip()
    if not (base and key):
        return None
    return Embedder(
        base,
        key,
        os.environ.get("FDTEXT_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B"),
        dimensions=int(os.environ.get("FDTEXT_EMBEDDING_DIM", "1024")),
        provider=provider_from_env(),
    )

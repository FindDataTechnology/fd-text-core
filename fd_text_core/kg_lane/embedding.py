"""嵌入客户端：服务端 MRL 截断语义。

与引擎核心 `embedding.py` 的分工：核心那个服务「语料切块向量化」（零外部 SDK、
urllib 自实现）；本模块服务 KG 车道的实体/事实向量化，走 OpenAI 兼容的
`/embeddings` 端点（graphiti 的 `EmbedderClient` 契约）。

两条硬规矩（来自 2026-10-18 词元嵌入通道切换的事故与实测）：

1. **dimensions 必须由调用方显式下发**。不带时上游会返回 8B 原生 4096 维；
   带 `dimensions=1024` 时是**服务端 MRL 截断**（与原生前 1024 维余弦 0.999892）。
2. **禁止客户端裁剪**。`embedding[:1024]` 是硬砍前 N 维，与 MRL 不是同一空间；
   若库内向量是 MRL 建的，混用会让检索静默漂移且无人能发现。

维度不符一律显式报错——宁可跑批失败，也不要向量进错空间。
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MODEL = "Qwen/Qwen3-Embedding-8B"
DEFAULT_DIM = 1024


class EmbeddingDimensionError(ValueError):
    """上游返回维度与配置不符（多半是上游忽略了 dimensions）。"""


#: 上游瞬时故障（5xx/超时/连接断）的重试参数。
#: 网关的上游会抖（2026-10-22 实测：单次 502 后紧接一次 200），一次失败就
#: 让整条调用挂掉不可接受；但长退避会拖死查询面，3 次短退避正好。
RETRY_ATTEMPTS = 3
RETRY_BASE_SECONDS = 1.0


async def _with_retry(call):
    """5xx/超时/连接类错误短退避重试；4xx（配错/欠费）立刻抛。"""
    import asyncio

    last: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return await call()
        except Exception as exc:  # noqa: BLE001 - 按类型/状态码判可重试性
            status = getattr(exc, "status_code", None) or getattr(
                getattr(exc, "response", None), "status_code", None)
            retryable = status is None or status >= 500
            if not retryable or attempt == RETRY_ATTEMPTS:
                raise
            last = exc
            await asyncio.sleep(RETRY_BASE_SECONDS * 2 ** (attempt - 1))
    raise last  # pragma: no cover - 循环必在 return 或 raise 处离开


@dataclass(frozen=True)
class EmbeddingSettings:
    base_url: str
    api_key: str
    model: str = DEFAULT_MODEL
    dim: int = DEFAULT_DIM
    # 词元通道下钉扎由网关账号级配置注入，此处默认留空；
    # 直连多上游服务商时才需要填（否则向量会抖）。
    provider_only: str = ""

    def require(self) -> "EmbeddingSettings":
        missing = [
            name
            for name, value in (("base_url", self.base_url), ("api_key", self.api_key))
            if not value
        ]
        if missing:
            raise ValueError(f"embedding settings missing: {', '.join(missing)}")
        return self


def build_embedder(settings: EmbeddingSettings, client=None):
    """构造 graphiti 兼容的 embedder。client 可注入（测试用）。"""
    if client is None:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url)

    try:  # graphiti 只在装了 kg extra 时可用；无它时用等价基类（duck-typing 足够）
        from graphiti_core.embedder.client import EmbedderClient as _Base
    except ModuleNotFoundError:  # pragma: no cover - 无 graphiti 环境走这里
        class _Base:  # type: ignore[no-redef]
            async def create(self, input_data):  # noqa: ANN001
                raise NotImplementedError

            async def create_batch(self, input_data_list):  # noqa: ANN001
                raise NotImplementedError

    class _ServerMRLEmbedder(_Base):
        """显式 dimensions + 不做客户端截断 + 维度校验。"""

        def __init__(self) -> None:
            self._client = client
            self._settings = settings.require()

        def _kwargs(self, input_data):
            kwargs = {
                "input": input_data,
                "model": self._settings.model,
                "dimensions": int(self._settings.dim),
            }
            if self._settings.provider_only:
                kwargs["extra_body"] = {
                    "provider": {"only": [self._settings.provider_only]}
                }
            return kwargs

        def _check(self, vec):
            if len(vec) != int(self._settings.dim):
                raise EmbeddingDimensionError(
                    f"embedding dim mismatch: got {len(vec)}, configured {self._settings.dim} "
                    f"(upstream ignored `dimensions`? model={self._settings.model})"
                )
            return vec

        async def create(self, input_data) -> list[float]:
            async def _call():
                res = await self._client.embeddings.create(**self._kwargs(input_data))
                return self._check(res.data[0].embedding)

            return await _with_retry(_call)

        async def create_batch(self, input_data_list) -> list[list[float]]:
            async def _call():
                res = await self._client.embeddings.create(**self._kwargs(input_data_list))
                return [self._check(item.embedding) for item in res.data]

            return await _with_retry(_call)

    return _ServerMRLEmbedder()

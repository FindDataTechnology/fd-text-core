"""引擎配置：全部经环境变量注入，缺失即诚实报错（不猜测默认值）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    """配置缺失/非法。"""


def _require(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise ConfigError(f"缺少必需环境变量 {name}")
    return v


@dataclass(frozen=True)
class Settings:
    """从环境变量构造，失败即 ConfigError。"""

    database_url: str
    s3_endpoint: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    # 8B 原生 4096 维超出 pgvector HNSW 的 2000 维上限；MRL 截断到 1024
    # 与 vector(1024) 列定义对齐（text-embedding-upgrade-8b）
    embedding_model: str = "Qwen/Qwen3-Embedding-8B"
    embedding_api_base: str = ""
    embedding_api_key: str = ""
    embedding_dim: int = 1024
    # 上游钉住：逗号分隔的 provider 名单（如 "SiliconFlow"），空=不下发。
    # 用途与必要性见 embedding.Embedder 的 provider 参数注释：网关若挂了多个
    # 都能响应同一模型名的上游，不钉住会让同一文本多次调用返回不同向量，
    # 触发锚点自检门误报并使检索静默漂移。直连单一服务商时留空。
    embedding_provider_only: str = ""
    tsv_config_default: str = "simple"
    cold_store_min_chars: int = 200_000
    max_read_chars: int = 200_000
    search_limit_max: int = 100
    search_limit_default: int = 20
    rrf_k: int = 60
    vector_prefetch: int = 50
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=_require("FDTEXT_DATABASE_URL"),
            s3_endpoint=_require("FDTEXT_S3_ENDPOINT"),
            s3_bucket=os.environ.get("FDTEXT_S3_BUCKET", "fd-text"),
            s3_access_key=_require("FDTEXT_S3_ACCESS_KEY"),
            s3_secret_key=_require("FDTEXT_S3_SECRET_KEY"),
            embedding_model=os.environ.get("FDTEXT_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B"),
            embedding_api_base=os.environ.get("FDTEXT_EMBEDDING_API_BASE", ""),
            embedding_api_key=os.environ.get("FDTEXT_EMBEDDING_API_KEY", ""),
            embedding_dim=int(os.environ.get("FDTEXT_EMBEDDING_DIM", "1024")),
    # 上游钉住：逗号分隔的 provider 名单，如 "SiliconFlow"。空=不下发。
    # 用途见 embedding.Embedder 的 provider 参数注释（网关多上游轮换会导致
    # 同一文本返回不同向量）。直连单一服务商时留空即可。
    embedding_provider_only=os.environ.get("FDTEXT_EMBEDDING_PROVIDER_ONLY", "").strip(),
            tsv_config_default=os.environ.get("FDTEXT_TSV_CONFIG", "simple"),
        )

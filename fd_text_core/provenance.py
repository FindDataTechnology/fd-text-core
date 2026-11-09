"""provenance 契约：引用可复现的三元组。

与 fd-legal-search-mcp 的 Provenance 契约同构（source/stable_id/as_of），
使跨垂域引用在上层表现得一致。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class Provenance:
    source: str  # 采集源标识，如 arxiv / gutenberg
    stable_id: str  # 文档稳定标识（引擎内 corpus_documents.id 或源 ID）
    as_of: str  # 索引时点 ISO8601

    @staticmethod
    def now(source: str, stable_id: str, *, when: datetime | None = None) -> Provenance:
        dt = when or datetime.now(timezone.utc)
        return Provenance(source=source, stable_id=stable_id, as_of=dt.isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def require_triplet(p: dict | Provenance | None) -> dict:
    """质量门/查询面共用：三元组齐备返回 dict，否则 ValueError 点名缺哪个字段。"""
    d = p.to_dict() if isinstance(p, Provenance) else dict(p or {})
    missing = [k for k in ("source", "stable_id", "as_of") if not d.get(k)]
    if missing:
        raise ValueError(f"provenance 缺失字段: {', '.join(missing)}")
    return d

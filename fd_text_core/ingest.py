"""L1 摄取：对象存储布局 -> corpus_documents（去重 / 版本退役 / 嵌入任务挂钩）。

语言中立契约：任何语言的采集器只要把 JSONL 产物写进
`<bucket>/<domain>/structured/...`，引擎就能消费。fd-law-data（Node）即如此。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from .db import execute, fetch_all, fetch_one
from .provenance import Provenance

CURRENT = "current"
SUPERSEDED = "superseded"  # 中性退役词：绝不用垂域私有词汇（如法律线的「历史版本」）

VALID_ORIGINS = ("crawl", "upload")  # upload 为将来自动取通道预留，本期无对外端点


@dataclass
class Doc:
    """采集器产出的规范化文档。"""

    domain: str
    source: str
    source_id: str
    title: str | None
    content: str
    metadata: dict = field(default_factory=dict)
    origin: str = "crawl"
    cold_block_key: str | None = None

    def __post_init__(self) -> None:
        if self.origin not in VALID_ORIGINS:
            raise ValueError(f"origin 非法: {self.origin}，可用={VALID_ORIGINS}")
        if not (self.domain and self.source and self.source_id):
            raise ValueError("domain/source/source_id 均不可为空")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def load_documents(docs: Iterable[Doc], batch_key: str | None = None) -> dict:
    """幂等批量入库。

    返回统计：inserted / skipped（未变）/ superseded（旧版退役）/ queued（嵌入任务）。
    未变则跳过且不产生嵌入任务；新版到达则旧版退役并重建索引面。
    """
    stats = {"inserted": 0, "skipped": 0, "superseded": 0, "queued": 0}
    bkey = batch_key or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for d in docs:
        existing = fetch_one(
            "SELECT id, content_hash FROM corpus_documents WHERE domain=%s AND source=%s AND source_id=%s",
            (d.domain, d.source, d.source_id),
        )
        if existing and existing["content_hash"] == d.content_hash:
            stats["skipped"] += 1
            continue

        if existing and existing["content_hash"] == d.content_hash:
            stats["skipped"] += 1
            continue

        if existing:
            # 旧版本整行退役（保留可查），其切块与待办嵌入任务随之失效；
            # 部分唯一索引保证同一身份只有一个 current 行。
            execute(
                "UPDATE corpus_documents SET status=%s, updated_at=now() WHERE id=%s",
                (SUPERSEDED, existing["id"]),
            )
            execute("DELETE FROM corpus_chunks WHERE document_id=%s", (existing["id"],))
            execute("DELETE FROM embed_tasks WHERE document_id=%s", (existing["id"],))
            stats["superseded"] += 1

        prov = Provenance.now(d.source, d.source_id).to_dict()
        row = fetch_one(
            """
            INSERT INTO corpus_documents
                (domain, source, source_id, title, content_hash, status, origin,
                 cold_block_key, metadata, provenance)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
            RETURNING id
            """,
            (
                d.domain, d.source, d.source_id, d.title, d.content_hash, CURRENT,
                d.origin, d.cold_block_key, json.dumps(d.metadata), json.dumps(prov),
            ),
        )
        if row is None:
            # RETURNING 不应为空；发生了就诚实失败而非静默续跑
            raise RuntimeError(f"入库未拿到 id: {d.domain}/{d.source}/{d.source_id}")
        doc_id = row["id"]
        stats["inserted"] += 1

        # 自动挂钩嵌入任务（claim-ledger 认领点，零人工触发）
        execute(
            """
            INSERT INTO embed_tasks (document_id, domain, batch_key, state)
            VALUES (%s,%s,%s,'pending')
            ON CONFLICT (document_id, batch_key) DO NOTHING
            """,
            (doc_id, d.domain, bkey),
        )
        stats["queued"] += 1

    return stats


def iter_structured(s3: Any, domain: str, limit: int = 1000) -> Iterator[dict]:
    """从 s3 客户端（boto3）读 `<bucket>/<domain>/structured/*.jsonl` 逐行产出 dict。"""
    import io

    bucket = s3._bucket if hasattr(s3, "_bucket") else None  # 测试替身可提供
    prefix = f"{domain}/structured/"
    kwargs = {"Bucket": bucket, "Prefix": prefix}
    continuation = None
    seen = 0
    while True:
        if continuation:
            kwargs["ContinuationToken"] = continuation
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".jsonl"):
                continue
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            for line in io.BytesIO(body):
                line = line.strip()
                if line:
                    yield json.loads(line)
                    seen += 1
                    if seen >= limit:
                        return
        if not resp.get("IsTruncated"):
            return
        continuation = resp.get("NextContinuationToken")


def current_document_ids(domain: str) -> list[int]:
    return [
        r["id"]
        for r in fetch_all(
            "SELECT id FROM corpus_documents WHERE domain=%s AND status=%s ORDER BY id",
            (domain, CURRENT),
        )
    ]

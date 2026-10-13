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

COLD_STORE_MIN_CHARS = 200_000  # 超过此长度的正文自动落 zstd 冷存，PG 只留指针


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
        # 正文必须有确定归处：短正文存 content 列，长正文写冷存并存指针。
        # 二者不得同时为空——否则嵌入 worker 取不到文本、无法切块（实测
        # 4 部剧本里 3 部因此失败，见 text-engine-short-doc-storage）。
        block_key = d.cold_block_key
        content_col = None
        if block_key is None:
            if len(d.content) >= COLD_STORE_MIN_CHARS:
                from .coldstore import put_block

                block_key, _ = put_block(
                    d.domain,
                    f"{hashlib.sha256(d.source_id.encode()).hexdigest()[:16]}.blk",
                    d.content,
                )
            else:
                content_col = d.content
        if block_key is None and content_col is None:
            raise ValueError(
                f"文档 {d.domain}/{d.source}/{d.source_id} 正文无归处："
                "content 与 cold_block_key 同时为空，拒绝入库（须补全而非静默写入）"
            )
        row = fetch_one(
            """
            INSERT INTO corpus_documents
                (domain, source, source_id, title, content_hash, status, origin,
                 cold_block_key, content, metadata, provenance)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
            RETURNING id
            """,
            (
                d.domain, d.source, d.source_id, d.title, d.content_hash, CURRENT,
                d.origin, block_key, content_col, json.dumps(d.metadata), json.dumps(prov),
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


def iter_structured(s3: Any, bucket: str, domain: str, limit: int = 1000) -> Iterator[dict]:
    """读 `<bucket>/<domain>/structured/*.jsonl`，按 run 折叠为最新版后产出 dict。

    桶名 SHALL 为显式参数：不从客户端属性反查（曾从 `s3._bucket` 取，而该属性
    只是测试替身的约定，真实 boto3 客户端没有——`Bucket=None` 传 botocore 必然
    TypeError，生产路径从未跑通。gov 垂域接入时实测暴露）。

    **按 run 折叠为最新版**（latest-wins）：分片键含 run id 且不可变，同一文档
    会随每次重抓出现多份分片；不折叠则逐行全入库撞版本退役路径（同一身份
    多行 → 多次退役）。规则 = 分片键字节序排序（键含 14 位 run 时间戳，
    字节序即时间序）、同一 `(source, source_id)` 后出现者胜。limit 按
    折叠后计数。
    """
    prefix = f"{domain}/structured/"
    kwargs = {"Bucket": bucket, "Prefix": prefix}
    continuation = None
    parts: list[tuple[str, bytes]] = []  # (key, body)
    while True:
        if continuation:
            kwargs["ContinuationToken"] = continuation
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".jsonl"):
                continue
            parts.append((key, s3.get_object(Bucket=bucket, Key=key)["Body"].read()))
        if not resp.get("IsTruncated"):
            break
        continuation = resp.get("NextContinuationToken")

    folded: dict[str, dict] = {}
    for key, body in sorted(parts, key=lambda kv: kv[0]):
        for line in body.decode("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            folded[f"{row.get('source')}:{row.get('source_id')}"] = row  # 后出现者胜

    seen = 0
    for row in folded.values():
        if seen >= limit:
            return
        yield row
        seen += 1


def current_document_ids(domain: str) -> list[int]:
    return [
        r["id"]
        for r in fetch_all(
            "SELECT id FROM corpus_documents WHERE domain=%s AND status=%s ORDER BY id",
            (domain, CURRENT),
        )
    ]

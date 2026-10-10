"""嵌入 worker：claim-ledger 认领 + 锚点余弦自检门 + 批量 upsert。

自检门（spec text-indexing-search）：开工前用库内锚点重算向量，与历史锚点
余弦 >= 0.999 才允许执行。低于阈值说明模型或通道漂移了——继续写入会污染
整库向量且难以回滚，因此拒开工并明确报错。

claim-ledger：一次认领一批 pending 任务并置 claimed，成功后置 finished；中断
后重跑只处理未完成任务，绝不重复已完成工作（02.1 的断点等价性要求）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import coldstore, ingest
from .chunking import chunk_text, verify_lossless
from .db import execute, fetch_all, fetch_one
from .embedding import Embedder, EmbeddingUnavailable
from .verticals.registry import get as get_vertical
from .verticals.registry import get_strategy

ANCHOR_TAG = "worker-startup"
SIMILARITY_FLOOR = 0.999


class GateFailure(RuntimeError):
    """自检门未通过，拒绝开工。"""


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"维度不符: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def vector_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def run_self_check(embedder: Embedder, model: str, *, bootstrap_text: str | None = None) -> float:
    """锚点余弦自检。首次（无锚点）写入锚点并返回 1.0。"""
    anchor = fetch_one("SELECT embedding FROM anchor_embedding WHERE tag=%s", (ANCHOR_TAG,))
    text = bootstrap_text or "fd-text-core 嵌入自检锚点 · embedding self-check anchor"
    vec = embedder.embed([text]).vectors[0]
    if anchor is None:
        execute(
            "INSERT INTO anchor_embedding (tag, model, dim, embedding) VALUES (%s,%s,%s,%s::vector)",
            (ANCHOR_TAG, model, len(vec), vector_literal(vec)),
        )
        return 1.0
    historical = _parse_vector(anchor["embedding"])
    sim = cosine(historical, vec)
    if sim < SIMILARITY_FLOOR:
        raise GateFailure(
            f"嵌入自检未通过：与历史锚点余弦 {sim:.6f} < {SIMILARITY_FLOOR}"
            f"（模型 {model} 或通道疑似漂移，拒绝执行以免污染向量库）"
        )
    return sim


def _parse_vector(raw) -> list[float]:
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    s = str(raw).strip().strip("[]")
    return [float(x) for x in s.split(",") if x]


@dataclass
class WorkerResult:
    claimed: int = 0
    embedded: int = 0
    chunks: int = 0
    failed: int = 0


def ensure_chunks(doc_row: dict, text: str) -> int:
    """按垂域策略切块入库（幂等：已有切块则跳过），返回写入块数。"""
    existing = fetch_one(
        "SELECT count(*)::int AS n FROM corpus_chunks WHERE document_id=%s", (doc_row["id"],)
    )
    if existing and existing["n"] > 0:
        return 0
    strategy = get_strategy(doc_row["domain"])
    chunks = chunk_text(text, strategy)
    ok, detail = verify_lossless(text, chunks)
    if not ok:
        raise ValueError(f"document {doc_row['id']} 切块不无损：{detail}")
    for c in chunks:
        execute(
            """
            INSERT INTO corpus_chunks
                (domain, document_id, chunk_no, chunk_text, char_start, char_end,
                 section_path, tsv, tsv_config)
            VALUES (%s,%s,%s,%s,%s,%s,%s, to_tsvector(%s::regconfig, %s), %s)
            ON CONFLICT (domain, document_id, chunk_no) DO NOTHING
            """,
            (
                doc_row["domain"], doc_row["id"], c.chunk_no, c.chunk_text,
                c.char_start, c.char_end, c.section_path,
                _tsv_config(doc_row["domain"]), c.chunk_text,
                _tsv_config(doc_row["domain"]),
            ),
        )
    return len(chunks)


def _tsv_config(domain: str) -> str:
    import os

    return os.environ.get("FDTEXT_TSV_CONFIG", "simple")


def claim_batch(limit: int = 50) -> list[dict]:
    rows = fetch_all(
        """
        UPDATE embed_tasks SET state='claimed', claimed_at=now()
        WHERE id IN (
            SELECT id FROM embed_tasks WHERE state='pending'
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT %s
        )
        RETURNING id, document_id, domain, batch_key
        """,
        (limit,),
    )
    return rows


def run_once(embedder: Embedder, model: str, limit: int = 50) -> WorkerResult:
    """跑一批。开工前必过自检门。"""
    sim = run_self_check(embedder, model)
    res = WorkerResult()
    tasks = claim_batch(limit)
    res.claimed = len(tasks)
    for t in tasks:
        try:
            doc = fetch_one("SELECT * FROM corpus_documents WHERE id=%s", (t["document_id"],))
            if doc is None:
                execute("UPDATE embed_tasks SET state='missing_doc', finished_at=now() WHERE id=%s", (t["id"],))
                res.failed += 1
                continue
            text = _document_text(doc)
            written = ensure_chunks(doc, text)
            res.chunks += written
            # 只嵌尚未嵌入的块，断点续嵌天然幂等
            targets = fetch_all(
                "SELECT id, chunk_text FROM corpus_chunks WHERE document_id=%s AND embedding IS NULL ORDER BY chunk_no",
                (doc["id"],),
            )
            if targets:
                vectors = embedder.embed([r["chunk_text"] for r in targets]).vectors
                for row, v in zip(targets, vectors):
                    execute(
                        "UPDATE corpus_chunks SET embedding=%s::vector, embedded_at=now() WHERE id=%s AND domain=%s",
                        (vector_literal(v), row["id"], doc["domain"]),
                    )
            execute(
                "UPDATE embed_tasks SET state='finished', finished_at=now() WHERE id=%s", (t["id"],)
            )
            res.embedded += 1
        except Exception as exc:
            execute(
                "UPDATE embed_tasks SET state='failed', finished_at=now(), error=%s WHERE id=%s",
                (str(exc)[:500], t["id"]),
            )
            res.failed += 1
    return WorkerResult(res.claimed, res.embedded, res.chunks, res.failed)


def _document_text(doc: dict) -> str:
    """取正文：优先冷存块，其次 structured 回源（本期摄取路径暂缺者为简单返回）。"""
    if doc.get("cold_block_key"):
        return coldstore.get_block(doc["cold_block_key"])
    raise RuntimeError(
        f"document {doc['id']} 无正文可用（冷存缺 key 且结构化正文未存）——"
        "属数据管线缺口，须补全而非静默跳过"
    )

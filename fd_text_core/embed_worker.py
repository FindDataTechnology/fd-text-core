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
from .tokenize import cjk_tokens_text
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
    reclaimed: int = 0  # 本次开工回收的陈旧 claim 数


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
    cjk = _cjk_enabled(doc_row["domain"])
    t2s = _t2s_enabled(doc_row["domain"])
    cfg = _tsv_config(doc_row["domain"])
    for c in chunks:
        if cjk:
            # 引擎分词：token 以空格连接喂 simple 配置（simple 按空白切且不做
            # 词干化/停用词，正好承接我们的 token）。查询侧走 cjk_tsquery 同构
            # （含同一 t2s 归一开关——两侧取值必须一致，见 tokenize 模块）。
            execute(
                """
                INSERT INTO corpus_chunks
                    (domain, document_id, chunk_no, chunk_text, char_start, char_end,
                     section_path, tsv, tsv_config)
                VALUES (%s,%s,%s,%s,%s,%s,%s, to_tsvector('simple', %s), 'engine-cjk')
                ON CONFLICT (domain, document_id, chunk_no) DO NOTHING
                """,
                (
                    doc_row["domain"], doc_row["id"], c.chunk_no, c.chunk_text,
                    c.char_start, c.char_end, c.section_path,
                    cjk_tokens_text(c.chunk_text, t2s=t2s),
                ),
            )
        else:
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
                    cfg, c.chunk_text, cfg,
                ),
            )
    return len(chunks)


def _cjk_enabled(domain: str) -> bool:
    from .verticals.registry import get

    try:
        return bool(get(domain).cjk)
    except KeyError:
        return False


def _t2s_enabled(domain: str) -> bool:
    """垂域繁简归一开关（默认 True）。未注册垂域回退 True（与垂域默认同值）。"""
    from .verticals.registry import get

    try:
        return bool(get(domain).t2s)
    except KeyError:
        return True


def _tsv_config(domain: str) -> str:
    """垂域配置优先（与 querylib 同源）；未注册垂域回退环境变量。

    此前实现完全忽略 domain、只读环境变量，与查询侧读垂域配置的行为不一致
    ——两处配置面分裂会让写入与查询用不同分词器，正是要防的静默失配。
    """
    from .verticals.registry import get

    import os

    try:
        v = get(domain)
        if v.tsv_config:
            return v.tsv_config
    except KeyError:
        pass
    return os.environ.get("FDTEXT_TSV_CONFIG", "simple")


def reclaim_stale(stale_after: str = "5 minutes") -> int:
    """回收陈旧 claim：worker 崩溃/被杀会把任务留在 claimed 态，

    而 claim_batch 只挑 pending，于是这批任务会永久悬空（规模冒烟中实测
    撞到：脚本达上限退出后 74 条任务卡 claimed，对应 73 篇文档有正文却无切块）。
    开工前先回收，保证「中断后续跑」的等价性承诺成立。
    """
    return execute(
        "UPDATE embed_tasks SET state='pending', claimed_at=NULL "
        "WHERE state='claimed' AND claimed_at < now() - %s::interval",
        (stale_after,),
    )


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
    res.reclaimed = reclaim_stale()
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
    """取正文：短文档在 content 列，长文档在冷存块；二者皆空如实报错。"""
    if doc.get("content"):
        return doc["content"]
    if doc.get("cold_block_key"):
        return coldstore.get_block(doc["cold_block_key"])
    raise RuntimeError(
        f"document {doc['id']} 无正文可用（content 与 cold_block_key 皆为空）——"
        "属数据管线缺口，须补全而非静默跳过"
    )

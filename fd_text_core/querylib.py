"""L4 统一查询库：关键词(tsvector) ⊕ 向量(pgvector) 的 RRF 混合检索。

所有垂域共用同一份实现——垂域只提供 facet 词表与 tsv 分词配置，
不各自实现检索（spec text-query-surface 的「域命名空间工具」前提）。

RRF 融合：score = Σ 1/(k + rank_channel)，k=60 起步，零权重调参。
重排器仅留接口位（RERANK_HOOK），本期不实现任何重排模型。
"""
from __future__ import annotations

import os
import re
from typing import Callable, Iterable

from .db import UpstreamUnavailable, fetch_all, fetch_one
from .embedding import EmbeddingUnavailable, Embedder
from .provenance import require_triplet
from .tokenize import cjk_tsquery
from .verticals.registry import get as get_vertical

RRF_K = int(os.environ.get("FDTEXT_RRF_K", "60"))
VECTOR_PREFETCH = int(os.environ.get("FDTEXT_VECTOR_PREFETCH", "50"))
RERANK_HOOK: Callable | None = None  # 重排器接口位：callable(list[dict]) -> list[dict]

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class FacetError(ValueError):
    """facet 名或取值不在垂域词表内。"""


def _validate_facets(domain: str, facets: dict | None) -> dict:
    """校验并回显可用值。未知 facet 名或取值 -> FacetError。"""
    if not facets:
        return {}
    v = get_vertical(domain)
    out = {}
    for key, val in facets.items():
        if not _IDENT.match(str(key)):
            raise FacetError(f"facet 名非法: {key}")
        if key not in v.facets:
            raise FacetError(f"垂域 {domain} 无 facet「{key}」，可用={sorted(v.facets)}")
        allowed = v.facets[key]
        vals = val if isinstance(val, (list, tuple)) else [val]
        for one in vals:
            if one not in allowed:
                raise FacetError(
                    f"facet「{key}」取值 {one!r} 不在词表内，可用={sorted(allowed)}"
                )
        out[key] = vals
    return out


def _tsv_config(domain: str) -> str:
    return get_vertical(domain).tsv_config or os.environ.get("FDTEXT_TSV_CONFIG", "simple")


def _filters_sql(domain: str, facets: dict, origin: str | None) -> tuple[str, list]:
    """把 facet 翻译成 metadata JSONB 过滤条件。"""
    clauses, params = [], []
    for key, vals in facets.items():
        clauses.append("d.metadata->>%s = ANY(%s)")
        params += [key, list(vals)]
    if origin:
        clauses.append("d.origin = %s")
        params.append(origin)
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    return where, params


def keyword_search(domain: str, query: str, limit: int, facets: dict | None = None) -> list[dict]:
    if not query.strip():
        return []
    where, params = _filters_sql(domain, facets or {}, None)
    if _cjk_enabled(domain):
        # 引擎分词查询：与写入侧 cjk_tokens_text 同构（两边只有一个来源，
        # 否则 token 永不相交、检索静默返回空——见 tokenize 模块 docstring）。
        # t2s 归一开关也取自同一垂域配置：一侧折简另一侧不折同样是失配。
        # cjk_tsquery 已做字词字符过滤，无 tsquery 语法注入面。
        tq = cjk_tsquery(query, t2s=_t2s_enabled(domain))
        if not tq:
            return []
        sql = f"""
            SELECT c.id, c.document_id, c.chunk_no, c.chunk_text, c.char_start, c.char_end,
                   c.section_path, d.title, d.source, d.source_id, d.provenance,
                   ts_rank_cd(c.tsv, to_tsquery('simple', %s)) AS kw_score
            FROM corpus_chunks c
            JOIN corpus_documents d ON d.id = c.document_id AND d.status='current'
            WHERE c.domain=%s AND c.tsv @@ to_tsquery('simple', %s){where}
            ORDER BY kw_score DESC, c.document_id, c.chunk_no
            LIMIT %s
        """
        rows = fetch_all(sql, [tq, domain, tq] + params + [limit])
    else:
        sql = f"""
            SELECT c.id, c.document_id, c.chunk_no, c.chunk_text, c.char_start, c.char_end,
                   c.section_path, d.title, d.source, d.source_id, d.provenance,
                   ts_rank_cd(c.tsv, plainto_tsquery(%s::regconfig, %s)) AS kw_score
            FROM corpus_chunks c
            JOIN corpus_documents d ON d.id = c.document_id AND d.status='current'
            WHERE c.domain=%s AND c.tsv @@ plainto_tsquery(%s::regconfig, %s){where}
            ORDER BY kw_score DESC, c.document_id, c.chunk_no
            LIMIT %s
        """
        cfg = _tsv_config(domain)
        rows = fetch_all(sql, [cfg, query, domain, cfg, query] + params + [limit])
    for r in rows:
        r["by"] = "keyword"
    return rows


def _cjk_enabled(domain: str) -> bool:
    try:
        return bool(get_vertical(domain).cjk)
    except KeyError:
        return False


def _t2s_enabled(domain: str) -> bool:
    """繁简归一开关（与写入侧 embed_worker._t2s_enabled 同源同默认）。"""
    try:
        return bool(get_vertical(domain).t2s)
    except KeyError:
        return True


def vector_search(
    domain: str, query: str, embedder: Embedder, limit: int, facets: dict | None = None
) -> list[dict]:
    if not query.strip():
        return []
    vec = embedder.embed([query]).vectors[0]
    lit = "[" + ",".join(f"{x:.7g}" for x in vec) + "]"
    where, params = _filters_sql(domain, facets or {}, None)
    sql = f"""
        SELECT c.id, c.document_id, c.chunk_no, c.chunk_text, c.char_start, c.char_end,
               c.section_path, d.title, d.source, d.source_id, d.provenance,
               1 - (c.embedding <=> %s::vector) AS sim
        FROM corpus_chunks c
        JOIN corpus_documents d ON d.id = c.document_id AND d.status='current'
        WHERE c.domain=%s AND c.embedding IS NOT NULL{where}
        ORDER BY c.embedding <=> %s::vector
        LIMIT %s
    """
    rows = fetch_all(sql, [lit, domain] + params + [lit, limit])
    for r in rows:
        r["by"] = "vector"
        r["score"] = r.pop("sim", None)
    return rows


def rrf_fuse(channels: Iterable[list[dict]], k: int = RRF_K) -> list[dict]:
    """倒数排名融合。同 id 跨通道合并，通道来源记入 matched_by。"""
    fused: dict[int, dict] = {}
    for ch in channels:
        for rank, row in enumerate(ch, start=1):
            rid = row["id"]
            if rid in fused:
                cur = fused[rid]
                cur["rrf"] += 1.0 / (k + rank)
                if row["by"] not in cur["matched_by"]:
                    cur["matched_by"].append(row["by"])
            else:
                item = dict(row)
                item["rrf"] = 1.0 / (k + rank)
                item["matched_by"] = [row["by"]]
                item.pop("by", None)
                item.pop("kw_score", None)
                fused[rid] = item
    out = sorted(fused.values(), key=lambda r: (-r["rrf"], r["id"]))
    if RERANK_HOOK:
        out = RERANK_HOOK(out)
    return out


def hybrid_search(
    domain: str,
    query: str,
    *,
    embedder: Embedder | None = None,
    limit: int | None = None,
    facets: dict | None = None,
    origin: str | None = None,
    channels: str = "hybrid",
) -> dict:
    """统一入口。返回 {results, degraded, facets_used}。

    degraded 非空时说明某通道不可用并实现降级——查询面必须如实披露，
    不得拿半份结果冒充完整结果。
    """
    lim = min(int(limit or int(os.environ.get("FDTEXT_SEARCH_LIMIT", "20"))),
              int(os.environ.get("FDTEXT_SEARCH_LIMIT_MAX", "100")))
    validated = _validate_facets(domain, facets)
    degraded: list[str] = []

    kw_rows = keyword_search(domain, query, max(lim, VECTOR_PREFETCH), validated) if channels in ("hybrid", "keyword") else []
    vec_rows: list[dict] = []
    if channels in ("hybrid", "vector"):
        if embedder is None:
            degraded.append("vector: 未提供 embedder")
        else:
            try:
                vec_rows = vector_search(domain, query, embedder, VECTOR_PREFETCH, validated)
            except EmbeddingUnavailable as exc:
                degraded.append(f"vector: {exc}")
            except UpstreamUnavailable as exc:
                degraded.append(f"vector: {exc}")

    results = rrf_fuse([kw_rows, vec_rows])[:lim]
    for r in results:
        try:
            require_triplet(r.get("provenance"))
        except ValueError as exc:
            degraded.append(f"provenance: doc {r.get('document_id')} {exc}")
    return {
        "results": results,
        "degraded": degraded,
        "facets_used": {k: v for k, v in validated.items()},
        "count": len(results),
    }


def read_chunks(document_id: int, offset: int = 0, limit: int = 4000) -> dict | None:
    """按 document_id 顺序读切片内容；返回 None 表示文档不存在。"""
    doc = fetch_one(
        "SELECT id, domain, title, source, source_id, provenance, cold_block_key, metadata "
        "FROM corpus_documents WHERE id=%s AND status='current'",
        (document_id,),
    )
    if doc is None:
        return None
    rows = fetch_all(
        """
        SELECT chunk_no, chunk_text, char_start, char_end, section_path
        FROM corpus_chunks WHERE document_id=%s AND char_end > %s
        ORDER BY chunk_no LIMIT %s
        """,
        (document_id, offset, max(1, min(int(os.environ.get("FDTEXT_READ_LIMIT", "200")), 200))),
    )
    text = "".join(r["chunk_text"] for r in rows)[:limit]
    return {
        "document": doc,
        "chunks": rows,
        "text": text,
        "next_offset": (rows[-1]["char_end"] if rows else offset),
    }

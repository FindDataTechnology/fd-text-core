"""L4 统一查询库：关键词(tsvector) ⊕ 向量(pgvector) 的 RRF 混合检索。

所有垂域共用同一份实现——垂域只提供 facet 词表与 tsv 分词配置，
不各自实现检索（spec text-query-surface 的「域命名空间工具」前提）。

RRF 融合：score = Σ 1/(k + rank_channel)，k=60 起步，零权重调参。
重排器仅留接口位（RERANK_HOOK），本期不实现任何重排模型。
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable

from .db import UpstreamUnavailable, fetch_all, fetch_one
from .embedding import Embedder, EmbeddingUnavailable
from .provenance import require_triplet
from .tokenize import cjk_tsquery
from .verticals.registry import get as get_vertical

RRF_K = int(os.environ.get("FDTEXT_RRF_K", "60"))
# 融合通道权重（默认 = 实证点：向量侧 2.0，见 change fusion-weight-vector-tilt
# 的 30 条对照实验）。调用时读环境变量（非导入时），使运行期可配、可测。
RRF_W_KEYWORD_DEFAULT = "1.0"
RRF_W_VEC_DEFAULT = "2.0"
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
    """垂域配置优先；未注册垂域回退环境变量。

    与写入侧 ``embed_worker._tsv_config`` 同构——写侧早有此兜底、读侧没有，
    两侧分裂会让读写用不同分词器（正是本模块要防的静默失配）。实测后果：
    垂域未注册时 ``get_vertical`` 抛 KeyError，把质量门的 ``--eval-all``
    直接打崩（该枚举按标注集目录进行，不看注册表）。
    """
    try:
        v = get_vertical(domain)
        if v.tsv_config:
            return v.tsv_config
    except KeyError:
        pass
    return os.environ.get("FDTEXT_TSV_CONFIG", "simple")


def _filters_sql(domain: str, facets: dict, origin: str | None) -> tuple[str, list]:
    """把 facet 翻译成 metadata JSONB 过滤条件。

    默认 facet 名就是 metadata 键、按等值匹配。垂域可用 ``facet_paths`` 声明
    例外——papers 的 ``cat`` 存在 ``metadata.categories``（数组）里，必须按包含
    匹配，否则 ``->>'cat'`` 恒 NULL，**按类目过滤静默返回空集**
    （实测 2026-11-13：0 篇 vs 实际 4 篇 cs.CL）。空集被读成"没有这类论文"
    比报错更坏，所以匹配方式也要能声明。
    """
    paths = getattr(get_vertical(domain), "facet_paths", {}) or {}
    clauses, params = [], []
    for key, vals in facets.items():
        meta_key, mode = paths.get(key, (key, "eq"))
        if mode == "overlap":
            # 数组包含：一篇 cs.CL+cs.LG 的交叉论文在两个取值下都能命中
            clauses.append("d.metadata->%s ?| %s")
            params += [meta_key, list(vals)]
        elif mode == "column":
            # 取值在**表列**上而不在 metadata 里（如 origin）——按 metadata 查会恒空。
            # 列名无法参数化，故必须过 _IDENT（注入面在这里）。
            if not _IDENT.match(str(meta_key)):
                raise FacetError(f"facet 映射到非法列名: {meta_key}")
            clauses.append(f"d.{meta_key} = ANY(%s)")
            params.append(list(vals))
        else:
            clauses.append("d.metadata->>%s = ANY(%s)")
            params += [meta_key, list(vals)]
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
        # 原串精确命中优先（ILIKE 对 CJK 与 LIKE 逐字节等价）：ts_rank_cd 按
        # 覆盖密度打分，高频 bigram 的密度会压过完整文号/法条号只出现一次的
        # 文档——精确串查询的目标被埋。该因子只改组内次序，不改召回集。
        sql = f"""
            SELECT c.id, c.document_id, c.chunk_no, c.chunk_text, c.char_start, c.char_end,
                   c.section_path, d.title, d.source, d.source_id, d.provenance,
                   ts_rank_cd(c.tsv, to_tsquery('simple', %s)) AS kw_score
            FROM corpus_chunks c
            JOIN corpus_documents d ON d.id = c.document_id AND d.status='current'
            WHERE c.domain=%s AND c.tsv @@ to_tsquery('simple', %s){where}
            ORDER BY (c.chunk_text ILIKE '%%' || %s || '%%') DESC,
                     kw_score DESC, c.document_id, c.chunk_no
            LIMIT %s
        """
        rows = fetch_all(sql, [tq, domain, tq, query] + params + [limit])
    else:
        # ILIKE（大小写不敏感）与 plainto_tsquery 的大小写折叠语义对齐。
        sql = f"""
            SELECT c.id, c.document_id, c.chunk_no, c.chunk_text, c.char_start, c.char_end,
                   c.section_path, d.title, d.source, d.source_id, d.provenance,
                   ts_rank_cd(c.tsv, plainto_tsquery(%s::regconfig, %s)) AS kw_score
            FROM corpus_chunks c
            JOIN corpus_documents d ON d.id = c.document_id AND d.status='current'
            WHERE c.domain=%s AND c.tsv @@ plainto_tsquery(%s::regconfig, %s){where}
            ORDER BY (c.chunk_text ILIKE '%%' || %s || '%%') DESC,
                     kw_score DESC, c.document_id, c.chunk_no
            LIMIT %s
        """
        cfg = _tsv_config(domain)
        rows = fetch_all(sql, [cfg, query, domain, cfg, query, query] + params + [limit])
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


def _exact_first(rows: list[dict], query: str) -> list[dict]:
    """融合后排序的「原串精确命中优先」（与关键词通道同一原则）。

    为什么融合层也要：RRF 是名次算术，双通道在场的平庸候选（Σ 1/(k+r)）
    会压过单通道第 1 的强证据候选——文号类查询实测（gov 1564）keyword 第 1、
    vector 未命中，融合后掉出前 10。切块含完整查询原串是强于任何名次融合的
    相关性证据，提到最前。大小写不敏感与非 CJK 分支的 ILIKE 语义对齐；
    自然语言查询通常不含完整原串，本因子对其为空操作。
    """
    if not query.strip():
        return rows
    q = query.strip().lower()
    return sorted(rows, key=lambda r: (0 if q in (r.get("chunk_text") or "").lower() else 1, -r["rrf"]))


def rrf_fuse(
    channels: Iterable[list[dict]],
    k: int = RRF_K,
    weights: Iterable[float] | None = None,
) -> list[dict]:
    """倒数排名融合。同 id 跨通道合并，通道来源记入 matched_by。

    `weights` 与 `channels` 顺序对应；None 即全 1.0（与不加权逐位等价）。
    **权重为什么存在**（难题集实证，change fusion-weight-vector-tilt）：
    均匀权重下混合低于单通道向量（难题集 MRR 0.9352 vs 1.0——RRF 名次算术
    被较弱的关键词通道平均拉低）。权重取值由两个标注集实测确定并入配置
    指纹；无证据不得调整（spec 明文）。
    """
    ch_list = list(channels)
    ws = [1.0] * len(ch_list) if weights is None else [float(w) for w in weights]
    if len(ws) != len(ch_list):
        raise ValueError(f"weights 长度 {len(ws)} 与通道数 {len(ch_list)} 不匹配")
    fused: dict[int, dict] = {}
    for ch, w in zip(ch_list, ws):
        for rank, row in enumerate(ch, start=1):
            rid = row["id"]
            if rid in fused:
                cur = fused[rid]
                cur["rrf"] += w / (k + rank)
                if row["by"] not in cur["matched_by"]:
                    cur["matched_by"].append(row["by"])
            else:
                item = dict(row)
                item["rrf"] = w / (k + rank)
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

    fused = rrf_fuse(
        [kw_rows, vec_rows],
        weights=[
            float(os.environ.get("FDTEXT_RRF_W_KEYWORD", RRF_W_KEYWORD_DEFAULT)),
            float(os.environ.get("FDTEXT_RRF_W_VEC", RRF_W_VEC_DEFAULT)),
        ],
    )
    if channels == "hybrid":
        # 原串优先只作用于融合路径；keyword/vector 纯通道保持各自的本征排序
        # （评测层的三通道对照才可比，且 vector 的语义序不被文本包含判定污染）。
        fused = _exact_first(fused, query)
    results = fused[:lim]
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

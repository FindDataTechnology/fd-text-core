"""三通道对同一 facet 的行为一致——真库、真嵌入，`pytest --live` 才跑。

为什么这条必须是 live：2026-11-13 的缺陷（`malformed array literal`）在纯单元层看不见——
`_filters_sql` 的片段与参数**各自**都对，错位只在把它们放回同一条语句、并且真的交给
Postgres 时才暴露。同一族还有一层：facet 在关键词道上错位报错时，向量道是好的，
于是"换个通道就有结果"会被误读成"语义检索更强"。

本文件钉住规格里的场景「三通道对同一 facet 的行为一致」：
同一组 facet 分别走 `keyword` / `vector` / `hybrid`，**任何一条道返回的文档都必须满足该 facet**。
判据取自数据库本身（读回每篇的 categories），不是取自返回报文的自述。

默认跳过（CI 无网内通道）；网内：
    FDTEXT_DATABASE_URL=… FDTEXT_EMBEDDING_API_BASE=… FDTEXT_EMBEDDING_API_KEY=… \
    pytest tests/test_live_facet_lane_parity.py --live
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

pytestmark = pytest.mark.skipif(
    not os.environ.get("FDTEXT_DATABASE_URL"),
    reason="live 用例：需要 FDTEXT_DATABASE_URL",
)

LANES = ("keyword", "vector", "hybrid")


def _need(request):
    if not request.config.getoption("--live"):
        pytest.skip("live 用例：网内以 pytest --live 实跑")
    if not (os.environ.get("FDTEXT_EMBEDDING_API_BASE") and os.environ.get("FDTEXT_EMBEDDING_API_KEY")):
        pytest.skip("缺嵌入凭据（vector 道无法实跑）")


def _payload(r):
    for attr in ("structuredContent", "structured_content", "content"):
        v = getattr(r, attr, None)
        if v is not None:
            return v
    return r


def _pick_facet_with_data() -> tuple[str, dict]:
    """挑一个**语料里真有文档**的 facet 取值，避免把"空集"当成"过滤生效"。"""
    from fd_text_core.db import fetch_all
    from fd_text_core.verticals import registry

    registry.load_verticals()
    papers = registry.get("papers")
    paths = dict(getattr(papers, "facet_paths", {}) or {})
    # 用 overlap 的 cat：它同时是"数组包含"与"词表可能滞后"两件事的现场
    rows = fetch_all(
        "SELECT c v, count(*) n FROM corpus_documents d,"
        " jsonb_array_elements_text(d.metadata->'categories') c"
        " WHERE d.domain='papers' GROUP BY c ORDER BY n DESC LIMIT 1"
    )
    assert rows, "papers 语料里没有任何 categories——本用例的前提不成立"
    value = rows[0]["v"]
    assert value in papers.facets.get("cat", frozenset()), (
        f"语料里出现词表外的类目 {value!r}——先修词表（facet 覆盖门也会报这条）"
    )
    assert paths.get("cat") == ("categories", "overlap"), f"papers.cat 映射变了：{paths.get('cat')}"
    return value, {"cat": [value]}


def test_every_lane_applies_the_same_facet(request):
    _need(request)
    from fd_text_core.db import fetch_all
    from fd_text_core.server import mcp

    value, facets = _pick_facet_with_data()

    cats: dict[int, set[str]] = {}
    for row in fetch_all("SELECT id, metadata->'categories' c FROM corpus_documents WHERE domain='papers'"):
        cats[row["id"]] = set(row["c"] or [])

    async def call(args):
        return _payload(await mcp.call_tool("papers_search", args))

    problems = []
    per_lane = {}
    for lane in LANES:
        res = asyncio.run(
            call({"query": "machine learning", "facets": facets, "limit": 10, "channels": lane})
        )
        assert not (isinstance(res, dict) and res.get("ok") is False), (
            f"{lane} 道返回错误：{res} —— 带 facet 的检索不得因参数拼装错位而失败"
        )
        results = res.get("results", []) if isinstance(res, dict) else []
        per_lane[lane] = len(results)
        assert res.get("facets_used", {}).get("cat") == [value], (
            f"{lane} 道的 facets_used 回显不对：{res.get('facets_used')}"
        )
        for item in results:
            doc_id = item["document_id"]
            if value not in cats.get(doc_id, set()):
                problems.append((lane, doc_id, sorted(cats.get(doc_id, set()))[:4]))

    assert not problems, (
        f"有 {len(problems)} 行不满足 facet cat={value}（按道统计 {per_lane}）：{problems[:5]}\n"
        "同一组 facet 必须在三条道上同等施加——差异只能来自召回与排序，不能来自过滤"
    )
    # 语料里该类目确有文档，所以至少关键词道不该是 0（否则是把"没过滤"当成"过滤生效"）
    assert per_lane["keyword"] > 0 or per_lane["vector"] > 0 or per_lane["hybrid"] > 0, per_lane

"""关键词查询的参数顺序：**带 facet 的查询必须真能跑**。

2026-11-13 生产实测的缺陷（上线前就存在，不是当天改出来的）：`keyword_search` 把
`{where}` 片段插在 `c.tsv @@ …` 之后、`ORDER BY … ILIKE` **之前**，但参数表却写成
`[…, query] + params + [limit]`——query 排在 facets 前面。于是任何带 facet 的
关键词查询都错位一格：

    $n   期望「元数据键 categories」→ 实际拿到 query
    $n+1 期望「取值数组 ["math.NA"]」→ 实际拿到 "categories"
        → psycopg.errors.InvalidTextRepresentation: malformed array literal: "categories"

`hybrid_search` 对关键词通道没有 try/except，所以这不是"少几条结果"，是**整个查询报错**。

为什么之前的测试没抓到：`test_pure` 只断言 `_filters_sql` 产出的 SQL 片段与参数**各自**
长什么样，从没把两者放回同一条语句里对齐。本文件补的就是这一步——占位符在 SQL 文本里的
**序号**与参数下标必须一致。

三条断言（都对顺序敏感，写反任何一处都会红）：
1. `len(params) == sql.count("%s")`（必要不充分：错位时长度也相等）；
2. ILIKE 那个占位符上必须正好是 `query`，最后一个必须是 `limit`；
3. facets 的参数必须落在 ILIKE 之前的那一段里，且键与取值数组都在。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fd_text_core.querylib as q  # noqa: E402
from fd_text_core.verticals import registry  # noqa: E402

registry.load_verticals()

ILIKE_MARK = "ILIKE '%%' || %s"
QUERY = "研究"


def _ordinal(sql: str, needle: str) -> int:
    """needle 里第一个 `%s` 是整条 SQL 的第几个占位符（0 基）。"""
    return sql[: sql.index(needle)].count("%s")


def _capture(monkeypatch, domain: str, facets: dict, cjk: bool, query: str = QUERY):
    seen: dict = {}

    def fake_fetch_all(sql, params):
        seen["sql"], seen["params"] = sql, params
        return []

    monkeypatch.setattr(q, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(q, "_cjk_enabled", lambda d: cjk)
    rows = q.keyword_search(domain, query, 7, facets=facets)
    assert rows == []
    return seen, 7


def _assert_aligned(seen: dict, limit: int, query: str, must_contain: tuple):
    sql, params = seen["sql"], seen["params"]
    assert len(params) == sql.count("%s"), (
        f"占位符 {sql.count('%s')} 个、参数 {len(params)} 个：数量就不对\n{sql}"
    )
    i = _ordinal(sql, ILIKE_MARK)
    assert params[i] == query, (
        f"ILIKE 占位符（第 {i} 个）上是 {params[i]!r}，应为 query {query!r} —— "
        "参数表把 query 排到 facets 前面了"
    )
    assert params[-1] == limit, f"最后一个参数应是 limit，实际 {params[-1]!r}"

    where_params = params[:i]
    for item in must_contain:
        assert item in where_params, (
            f"facets 参数 {item!r} 没落在 ILIKE 之前的那段 {where_params!r} 里"
        )


@pytest.mark.parametrize("cjk", [False, True], ids=["非CJK分支", "CJK分支"])
def test_overlap_mode_keeps_key_and_array_in_order(monkeypatch, cjk):
    """overlap（papers.cat → metadata.categories 数组）：键在前、数组在后。"""
    seen, limit = _capture(monkeypatch, "papers", {"cat": ["math.NA"]}, cjk)
    _assert_aligned(seen, limit, QUERY, ("categories", ["math.NA"]))


@pytest.mark.parametrize("cjk", [False, True], ids=["非CJK分支", "CJK分支"])
def test_column_mode_puts_the_array_where_the_clause_is(monkeypatch, cjk):
    """column（papers.origin → 表列 origin）：只有取值数组，没有元数据键。"""
    seen, limit = _capture(monkeypatch, "papers", {"origin": ["crawl"]}, cjk)
    _assert_aligned(seen, limit, QUERY, (["crawl"],))


@pytest.mark.parametrize("cjk", [False, True], ids=["非CJK分支", "CJK分支"])
def test_eq_mode_keeps_key_and_array_in_order(monkeypatch, cjk):
    """eq（默认）：news.has_body → metadata->>'has_body'。"""
    seen, limit = _capture(monkeypatch, "news", {"has_body": ["true"]}, cjk)
    _assert_aligned(seen, limit, QUERY, ("has_body", ["true"]))


def test_no_facet_still_aligns(monkeypatch):
    """无 facet 时也必须对齐——这条是别的测试最容易漏掉的分支。"""
    seen, limit = _capture(monkeypatch, "papers", None, False)
    _assert_aligned(seen, limit, QUERY, ())


def _all_declared_facets():
    for domain in ("papers", "gov", "screenplay", "news"):
        try:
            vert = registry.get(domain)
        except KeyError:
            continue
        for facet, vocab in vert.facets.items():
            values = sorted(vocab)
            if values:
                yield domain, facet, values[0]


def test_every_declared_facet_aligns(monkeypatch):
    """全量普查：注册表里每个垂域的每个 facet 都要能对齐。

    这条不枚举具体键名，所以**以后新加的 facet 自动被覆盖**——上面三条只钉住了
    现在存在的三种匹配方式。
    """
    checked = 0
    for domain, facet, value in _all_declared_facets():
        for cjk in (False, True):
            seen, limit = _capture(monkeypatch, domain, {facet: [value]}, cjk)
            assert len(seen["params"]) == seen["sql"].count("%s")
            i = _ordinal(seen["sql"], ILIKE_MARK)
            assert seen["params"][i] == QUERY, (
                f"{domain}.{facet}（cjk={cjk}）ILIKE 位置错：{seen['params'][i]!r}"
            )
            assert [value] in seen["params"][:i], (
                f"{domain}.{facet}（cjk={cjk}）的取值数组没落在 where 段"
            )
        checked += 1
    assert checked >= 8, f"只覆盖到 {checked} 个 facet，注册表加载不全"

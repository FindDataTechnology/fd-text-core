from fd_text_core.chunking import chunk_text, verify_lossless
from fd_text_core.verticals.news.parser import clean_news_text, parse_news
from fd_text_core.verticals.registry import get, list_verticals, load_verticals


def test_news_is_a_registered_vertical():
    load_verticals()
    assert "news" in list_verticals()
    vertical = get("news")
    assert vertical.cjk is True
    assert vertical.facets["source_type"] == frozenset(
        {"announcement", "ministry", "interaction", "news"}
    )
    assert set(vertical.presets) == {"latest", "by_entity", "timeline", "similar"}


def test_news_chunking_is_lossless():
    text = "某公司发布公告。\n\n公司拟以十二亿元收购标的公司全部股权。" * 80
    chunks = chunk_text(text, get("news").strategy)
    ok, detail = verify_lossless(text, chunks)
    assert ok, detail


def test_parser_splits_title_and_paragraphs():
    raw = "关于第三季度报告的公告\n\n公司于 2026 年 10 月 20 日披露报告。\n\n特此公告。"
    tree, meta = parse_news(raw)
    assert tree["title"] == "关于第三季度报告的公告"
    assert tree["paragraphs"][0].startswith("公司于")
    assert meta["paragraph_count"] == 2


def test_parser_does_not_invent_a_title():
    raw = "正文第一段以句号结束。\n\n第二段。"
    tree, _ = parse_news(raw)
    assert tree["title"] == ""
    assert len(tree["paragraphs"]) == 2


def test_clean_text_preserves_content():
    raw = "标题\r\n\r\n正文　有全角空格。\r\n"
    cleaned = clean_news_text(raw)
    assert "正文 有全角空格。" in cleaned
    assert "\r" not in cleaned


def test_loader_projects_every_declared_facet():
    """词表声明的每个 facet 都必须在入库时被写入。

    2026-11-13 实测：news 词表三件里 has_body 从未落库，于是任何 has_body 过滤
    恒 0 命中（生产 5908 篇全无该键）。空集被读成"没有这类文档"，而不是
    "字段没写"—— 这条把"声明即必须落库"钉住。
    """
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "load_news_spool", Path(__file__).resolve().parents[1] / "scripts" / "load_news_spool.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    docs, skipped = mod.rows_to_docs([
        {"source": "cn-announcement", "source_id": "A1", "content": "公司发布公告，正文若干。",
         "title": "公告", "url": "https://example.invalid/1", "published_at": "2026-11-01"},
        {"source": "cn-announcement", "source_id": "A2", "content": ""},  # 缺正文：应被 skip
    ])
    assert len(docs) == 1 and len(skipped) == 1, skipped
    md = docs[0].metadata
    for facet in get("news").facets:
        assert facet in md, f"词表声明了 {facet} 却未落库——按它过滤会恒 0"
    assert md["has_body"] == "true"
    assert md["source_type"] in get("news").facets["source_type"]
    assert md["lang"] in get("news").facets["lang"]

from fd_text_core.chunking import chunk_text, verify_lossless
from fd_text_core.verticals.news.parser import clean_news_text, parse_news
from fd_text_core.verticals.registry import get, load_verticals, list_verticals


def test_news_is_a_registered_vertical():
    load_verticals()
    assert "news" in list_verticals()
    vertical = get("news")
    assert vertical.cjk is True
    assert vertical.facets["source_type"] == frozenset(
        {"announcement", "ministry", "interaction"}
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

"""资讯 facet 投影的测试（change: unify-facet-projection-at-source task 3.1/3.2）。"""
from __future__ import annotations

from fd_text_core.verticals.news.facets import _SOURCE_TYPE_FALLBACK, project_facets


def test_declared_source_type_wins():
    out = project_facets({"source": "anything", "source_type": "policy", "content": "x"})
    assert out["source_type"] == "policy"


def test_fallback_govcn_is_policy():
    # 血泪：govcn 落 announcement 会被 EMBED_SKIP_SOURCE_TYPES 跳过不嵌
    assert project_facets({"source": "govcn", "content": "x"})["source_type"] == "policy"


def test_fallback_cninfo_is_announcement():
    assert project_facets({"source": "cninfo", "content": "x"})["source_type"] == "announcement"


def test_unknown_source_defaults_announcement():
    assert project_facets({"source": "brand-new-source", "content": "x"})["source_type"] == "announcement"


def test_lang_default_zh():
    assert project_facets({"source": "news-rss", "content": "x"})["lang"] == "zh"
    assert project_facets({"source": "news-rss", "content": "x", "lang": "en"})["lang"] == "en"


def test_has_body_reflects_content():
    assert project_facets({"source": "news-rss", "content": "正文"})["has_body"] == "true"
    assert project_facets({"source": "news-rss", "content": ""})["has_body"] == "false"


def test_fallback_table_matches_old_loader_table():
    """兜底映射必须与旧 load_news_spool._facet_source_type 逐键相等（防搬运漏改）。"""
    expected = {
        "cninfo": "announcement",
        "cn-gov": "ministry",
        "sse-interaction": "interaction",
        "news-rss": "news",
        "govcn": "policy",
    }
    assert _SOURCE_TYPE_FALLBACK == expected

"""papers 引用图 connector：离线测试（不访问网络）。

覆盖契约要点：
  - 正向/反向引用图返回结构正确
  - 失败显式且可判别：quota_exceeded / upstream_unavailable / unreachable /
    not_indexed 各自成类，**空 hits 必须伴随 degraded 原因**
  - 礼貌调用：mailto 进入参数与 User-Agent
  - CC0 归属
"""
from __future__ import annotations

import urllib.error

import pytest

from fd_text_core.verticals.papers import connectors as C


def _patch_json(monkeypatch, payload):
    seen = {}

    def fake(url, *, timeout=C.CITATION_TIMEOUT, headers=None):
        seen["url"] = url
        seen["headers"] = headers or {}
        seen["timeout"] = timeout
        return payload

    monkeypatch.setattr(C, "_http_json", fake)
    return seen


def test_references_parses_ids_and_counts(monkeypatch):
    seen = _patch_json(
        monkeypatch,
        {
            "id": "https://openalex.org/W1",
            "referenced_works_count": 2,
            "referenced_works": ["https://openalex.org/Wx", "https://openalex.org/Wy"],
        },
    )
    r = C.openalex_references(work_id="W1", mailto="ops@finddata.dev")
    assert r.degraded == []
    h = r.hits[0]
    assert h["work_id"] == "W1"
    assert h["reference_count"] == 2
    assert h["reference_ids"] == ["Wx", "Wy"]
    assert h["source"] == "openalex" and h["license"] == "CC0"
    # 礼貌调用：mailto 进 UA
    assert "mailto:ops@finddata.dev" in seen["headers"]["User-Agent"]


def test_citations_returns_citing_works_and_total(monkeypatch):
    _patch_json(
        monkeypatch,
        {
            "meta": {"count": 83390},
            "results": [
                {"id": "https://openalex.org/Wc", "title": "Citing", "publication_year": 2021}
            ],
        },
    )
    c = C.openalex_citations(work_id="W1", limit=1, mailto="ops@finddata.dev")
    assert c.degraded == []
    assert c.hits[0]["citing_work_id"] == "Wc"
    assert c.hits[0]["total_count"] == 83390
    assert c.hits[0]["license"] == "CC0"


def test_resolve_by_doi(monkeypatch):
    seen = _patch_json(monkeypatch, {"results": [{"id": "https://openalex.org/W2919115771"}]})
    wid, reason = C._resolve_work_id(doi="10.1038/nature14539", mailto="ops@finddata.dev")
    assert wid == "W2919115771" and reason == ""
    # URL 已 urlencode，故解码后断言
    import urllib.parse
    assert "doi:10.1038/nature14539" in urllib.parse.unquote(seen["url"])


def test_missing_identifier_is_degraded_not_empty_hits():
    r = C.openalex_references()
    assert r.hits == []
    assert r.degraded and "missing_identifier" in r.degraded[0]


def test_not_indexed_is_degraded_with_clear_reason(monkeypatch):
    _patch_json(monkeypatch, {"results": []})
    r = C.openalex_references(doi="10.1/nope", mailto="ops@finddata.dev")
    assert r.hits == []
    assert r.degraded and "not_indexed" in r.degraded[0]
    # 未收录 ≠ 不存在：提示语必须出现
    assert "不等于" in r.degraded[0]


@pytest.mark.parametrize("code,expected", [
    (429, "quota_exceeded"),
    (503, "upstream_unavailable"),
    (404, "http_error"),
])
def test_http_failures_are_classified(monkeypatch, code, expected):
    def boom(url, *, timeout=C.CITATION_TIMEOUT, headers=None):
        raise urllib.error.HTTPError(url, code, "x", {}, None)

    monkeypatch.setattr(C, "_http_json", boom)
    for fn in (C.openalex_references, C.openalex_citations):
        out = fn(work_id="W1", mailto="m")
        assert out.hits == []
        assert out.degraded and expected in out.degraded[0], (fn.__name__, out.degraded)


def test_unreachable_is_classified(monkeypatch):
    def boom(url, *, timeout=C.CITATION_TIMEOUT, headers=None):
        raise urllib.error.URLError("dns fail")

    monkeypatch.setattr(C, "_http_json", boom)
    r = C.openalex_references(work_id="W1", mailto="m")
    assert r.hits == []
    assert r.degraded and "unreachable" in r.degraded[0]


def test_limit_is_capped(monkeypatch):
    seen = _patch_json(monkeypatch, {"meta": {"count": 10}, "results": []})
    C.openalex_citations(work_id="W1", limit=500, mailto="m")
    assert f"per-page={C.CITATION_LIMIT_MAX}" in seen["url"]

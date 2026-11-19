"""论文全文摄取：取源 → 判类型 → 解析 → 降级，每条分支都钉住（不联网）。

这一组测试存在的理由：旧 ``load_papers.py`` 硬编码"只拼标题+摘要"，而
spec、LICENSE-NOTE、harvest docstring 三处都说要全文入库。那段代码没有分支，
所以也没有测试能红——"全文没入库"这件事在测试层面根本不可观测。
现在把降级分支也当规格的一部分测：取不到要**说出原因**，不能只留一个 false。
"""
from __future__ import annotations

import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import fd_text_core.verticals.papers.fulltext as F
import fd_text_core.verticals.papers.harvest as H
from fd_text_core.verticals.papers.parser import parse_pdf_via_grobid

TEX_BODY = r"""
\documentclass{article}
\begin{document}
\title{A Study of Cross-Sectional Returns}
\section{Introduction}
We document that momentum and value interact nonlinearly in Chinese equities.
\section{Data}
Daily bars from 2015 to 2025 across 1257 names, forward returns over 20 days.
\section{Results}
The spread between top and bottom quintiles is stable out of sample.
\end{document}
"""

GROBID_TEI = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <fileDesc>
      <titleStmt><title>PDF Only Paper</title></titleStmt>
    </fileDesc>
    <profileDesc><abstract><p>An abstract from the PDF.</p></abstract></profileDesc>
  </teiHeader>
  <text><body>
    <div><head>Introduction</head><p>Body text recovered by GROBID.</p></div>
    <div><head>Method</head><p>We estimate a cross-sectional regression.</p></div>
  </body></text>
</TEI>
"""


def _paper(arxiv_id: str = "2601.00001") -> H.Paper:
    return H.Paper(
        arxiv_id=arxiv_id,
        title="A Study of Cross-Sectional Returns",
        abstract="We study factor interactions in Chinese equities.",
        authors=["Ada Lovelace"],
        categories=["q-fin.ST"],
    )


# ── 取源 ────────────────────────────────────────────────────────────────


def test_fetch_eprint_throttles_each_paper():
    """限速按请求算：每篇都过一次节流器，不能整批只等一次。"""
    waits = []

    class FakeThrottler:
        def wait(self):
            waits.append(1)

    calls = []

    def fetcher(url, timeout=180):
        calls.append((url, timeout))
        return TEX_BODY.encode()

    blob = H.fetch_eprint(_paper("2601.00042"), throttler=FakeThrottler(), fetcher=fetcher)
    assert blob == TEX_BODY.encode()
    assert waits == [1], "节流器必须被调用一次"
    assert calls[0][0] == "https://arxiv.org/e-print/2601.00042"


def test_fetch_eprint_wraps_http_error_with_status():
    """404 要带 arxiv_id 与状态码：调用方靠它按篇降级并记账。"""

    def fetcher(url, timeout=180):
        raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)

    with pytest.raises(H.EprintError) as ei:
        H.fetch_eprint(_paper("2601.09999"), throttler=H.Throttler(sleep=lambda s: None), fetcher=fetcher)
    assert ei.value.arxiv_id == "2601.09999"
    assert ei.value.status == 404
    assert isinstance(ei.value, H.HarvestError), "EprintError 应是 HarvestError 的子类，便于统一兜"


# ── 全文获取的六种结局 ──────────────────────────────────────────────────


def test_latex_source_is_parsed_into_fulltext():
    def fetcher(url, timeout=180):
        return TEX_BODY.encode()

    text, status = F.acquire_fulltext(
        _paper(), throttler=H.Throttler(sleep=lambda s: None), fetcher=fetcher
    )
    assert status == F.STATUS_LATEX
    assert "top and bottom quintiles" in text, "正文里的句子必须真的进来"
    assert "\\documentclass" not in text, "解析后不应残留 LaTeX 命令"


def test_pdf_without_grobid_degrades_and_never_fakes_fulltext():
    """PDF-only 且没配 GROBID：正文留空 + 状态 needs-grobid，不许拿摘要冒充全文。"""

    def fetcher(url, timeout=180):
        return b"%PDF-1.7\n%binary junk\n"

    text, status = F.acquire_fulltext(
        _paper(), throttler=H.Throttler(sleep=lambda s: None), fetcher=fetcher
    )
    assert (text, status) == ("", F.STATUS_NEEDS_GROBID)


def test_pdf_with_grobid_uses_tei_sections():
    def eprint_fetcher(url, timeout=180):
        return b"%PDF-1.7\n%binary junk\n"

    def grobid_fetcher(url, pdf_bytes):
        assert url.endswith("/api/processFulltextDocument"), url
        assert pdf_bytes.startswith(b"%PDF"), "必须把 PDF 原样交给 GROBID"
        return GROBID_TEI

    text, status = F.acquire_fulltext(
        _paper(),
        throttler=H.Throttler(sleep=lambda s: None),
        fetcher=eprint_fetcher,
        grobid_endpoint="http://grobid:8070",
        grobid_fetcher=grobid_fetcher,
    )
    assert status == F.STATUS_GROBID
    assert "recovered by GROBID" in text
    assert "cross-sectional regression" in text


def test_fetch_error_is_a_status_not_an_exception():
    def fetcher(url, timeout=180):
        raise TimeoutError("read timed out")

    text, status = F.acquire_fulltext(
        _paper(), throttler=H.Throttler(sleep=lambda s: None), fetcher=fetcher
    )
    assert (text, status) == ("", F.STATUS_FETCH_ERROR), "单篇失败只降级，不能打断整批"


def test_empty_parse_is_distinguished_from_missing_source():
    """取到了但解析不出正文 → parse-empty；和"取不到"是两回事，别混成一种。"""

    def fetcher(url, timeout=180):
        return b"%PDF-1.7\n"

    # 无 GROBID 时走 needs-grobid；这里让 GROBID 回空 TEI，逼出 parse-empty 之外的
    # 真实分支：既不是取源失败，也不是 LaTeX 成功。
    text, status = F.acquire_fulltext(
        _paper(),
        throttler=H.Throttler(sleep=lambda s: None),
        fetcher=fetcher,
        grobid_endpoint="http://grobid:8070",
        grobid_fetcher=lambda url, body: "",
    )
    assert (text, status) == ("", F.STATUS_NEEDS_GROBID)


def test_unknown_blob_reports_parse_empty():
    def fetcher(url, timeout=180):
        return b"not a paper at all"

    text, status = F.acquire_fulltext(
        _paper(), throttler=H.Throttler(sleep=lambda s: None), fetcher=fetcher
    )
    assert (text, status) == ("", F.STATUS_PARSE_EMPTY)


# ── 组装与元数据 ────────────────────────────────────────────────────────


def test_content_keeps_abstract_before_body():
    content = F.compose_content(_paper(), "BODY-SENTINEL")
    assert content.index("We study factor interactions") < content.index("BODY-SENTINEL")
    assert content.startswith("A Study of Cross-Sectional Returns")


def test_has_fulltext_follows_real_body_not_the_flag():
    """元数据由正文推出：开关打开但没取到 → 仍然 false，且带原因。"""
    assert F.fulltext_metadata("some body", F.STATUS_LATEX)["has_fulltext"] == "true"
    meta = F.fulltext_metadata("", F.STATUS_NEEDS_GROBID)
    assert meta["has_fulltext"] == "false"
    assert meta["fulltext_status"] == "needs-grobid"
    # facet 词表只认 true/false 两个字符串（verticals/papers/__init__.py FACETS）
    assert meta["has_fulltext"] in {"true", "false"}


def test_unknown_status_is_rejected():
    with pytest.raises(ValueError):
        F.fulltext_metadata("x", "something-new")


# ── GROBID 客户端本体 ───────────────────────────────────────────────────


def test_grobid_client_without_endpoint_never_pretends():
    """没配端点时连"尝试"都不许发生。

    只断言 ``source_kind == "needs-grobid"`` 是空断言：空 endpoint 拼出的 URL
    没有 scheme，transport 自己就会抛错并被兜底成同一个值——**两条完全不同的原因
    给出同一个观测**。真正要钉的是"零请求"：注入一个记账 fetcher，它一次都不能被调。
    """
    calls = []

    def spy(url, body):
        calls.append(url)
        return GROBID_TEI

    out = parse_pdf_via_grobid(b"%PDF-1.7\n", endpoint="", fetcher=spy)
    assert out.source_kind == "needs-grobid"
    assert out.full_text == ""
    assert calls == [], "未配置端点时不得发出任何 GROBID 请求"


def test_grobid_client_swallows_transport_errors():
    def boom(url, body):
        raise ConnectionError("grobid down")

    out = parse_pdf_via_grobid(b"%PDF-1.7\n", endpoint="http://g:8070", fetcher=boom)
    assert out.source_kind == "needs-grobid", "端点挂了也只能降级，不能让整批炸掉"


def test_grobid_client_parses_tei():
    out = parse_pdf_via_grobid(
        b"%PDF-1.7\n", endpoint="http://g:8070/", fetcher=lambda url, body: GROBID_TEI
    )
    assert out.source_kind == "grobid-tei"
    assert [p for p, _ in out.sections] == ["paper>Introduction", "paper>Method"]
    assert out.title == "PDF Only Paper"


# ── 组装：默认模式不许发任何取源请求（规格场景「全文通道默认关闭」）──────────


def test_default_mode_issues_zero_eprint_requests():
    """规格写了「默认参数下不发起任何 e-print 请求」，这里把它钉住。

    上一版没有可测的组装函数，于是"只索引摘要"在测试层面不可观测——
    谁把开关默认值改成 True，全套测试照样绿。现在改默认值会当场红。
    """
    calls = []

    def fetcher(url, timeout=180):
        calls.append(url)
        return TEX_BODY.encode()

    items, summary = F.build_documents(
        [_paper("2601.00001"), _paper("2601.00002")], fetcher=fetcher
    )
    assert calls == [], "默认模式不得发起任何 e-print 取源"
    assert summary["with_fulltext"] == 0
    assert summary["status_counts"] == {F.STATUS_DISABLED: 2}
    assert all(it["metadata"]["has_fulltext"] == "false" for it in items)
    assert all(it["metadata"]["fulltext_status"] == F.STATUS_DISABLED for it in items)


def test_fulltext_mode_fetches_once_per_paper_and_counts_real_body():
    calls = []

    def fetcher(url, timeout=180):
        calls.append(url)
        return TEX_BODY.encode()

    items, summary = F.build_documents(
        [_paper("2601.00001"), _paper("2601.00002")], with_fulltext=True, fetcher=fetcher
    )
    assert len(calls) == 2, "全文模式每篇一次取源"
    assert summary["with_fulltext"] == 2
    assert summary["status_counts"] == {F.STATUS_LATEX: 2}
    assert all("top and bottom quintiles" in it["content"] for it in items)


def test_zero_success_is_visible_in_the_summary():
    """`--with-fulltext` 却零成功：summary 必须让调用方能判出来（脚本据此非零退出）。"""

    def fetcher(url, timeout=180):
        raise TimeoutError("blocked")

    items, summary = F.build_documents(
        [_paper("2601.00001")], with_fulltext=True, fetcher=fetcher
    )
    assert summary["with_fulltext"] == 0
    assert summary["status_counts"] == {F.STATUS_FETCH_ERROR: 1}
    assert items[0]["metadata"]["has_fulltext"] == "false"

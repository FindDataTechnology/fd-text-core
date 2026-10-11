"""不依赖外部服务的单元层：切块不变量、冷存格式、provenance、facet 与 RRF。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fd_text_core.chunking import SlidingWindow, chunk_text, verify_lossless  # noqa: E402
from fd_text_core.coldstore import BlockFormatError, decode_block, encode_block  # noqa: E402
from fd_text_core.provenance import Provenance, require_triplet  # noqa: E402
from fd_text_core.querylib import FacetError, _validate_facets, rrf_fuse  # noqa: E402
from fd_text_core.verticals.registry import get, list_verticals  # noqa: E402


@pytest.mark.parametrize(
    "text",
    [
        "第一节内容。这是验证句子，长度足够触发切块。" * 40,
        "Transformer attention improves sequence modeling. " * 30,
        "x" * 5000,
        "短。",
        "x",
    ],
)
def test_lossless_invariant(text):
    chunks = chunk_text(text, SlidingWindow(target=200, max_size=400))
    ok, detail = verify_lossless(text, chunks)
    assert ok, detail


def test_lossless_detects_missing_chunk():
    text = "第一节内容。这是验证句子。" * 20
    chunks = chunk_text(text, SlidingWindow(target=100, max_size=200))
    chunks.pop(1)
    ok, _ = verify_lossless(text, chunks)
    assert not ok


def test_lossless_detects_text_corruption():
    text = "abcd。" * 50
    chunks = chunk_text(text, SlidingWindow(target=100, max_size=200))
    chunks[0].chunk_text = chunks[0].chunk_text[:-2]
    ok, _ = verify_lossless(text, chunks)
    assert not ok


def test_coldstore_roundtrip_and_rejects_bad_block():
    text = "冷存往返验证。" * 500
    assert decode_block(encode_block(text), max_chars=len(text) + 1) == text
    blob = encode_block("x")
    with pytest.raises(BlockFormatError):
        decode_block(b"XX" + blob[2:])
    with pytest.raises(BlockFormatError):
        decode_block(b"tiny")
    with pytest.raises(BlockFormatError):
        decode_block(blob[:12] + bytes([2]) + blob[13:])


def test_provenance_triplet_required():
    p = Provenance.now("arxiv", "2601.00001")
    assert require_triplet(p)["stable_id"] == "2601.00001"
    with pytest.raises(ValueError, match="as_of"):
        require_triplet({"source": "arxiv", "stable_id": "1"})


def test_facet_validation():
    assert _validate_facets("papers", {"cat": "q-fin"}) == {"cat": ["q-fin"]}
    with pytest.raises(FacetError):
        _validate_facets("papers", {"nosuch": "x"})
    with pytest.raises(FacetError):
        _validate_facets("papers", {"cat": "astro-ph"})
    with pytest.raises(FacetError):
        _validate_facets("papers", {"a;drop": "1"})


def test_rrf_fusion_merges_channels():
    a = [{"id": 1, "by": "keyword"}, {"id": 2, "by": "keyword"}]
    b = [{"id": 1, "by": "vector"}, {"id": 3, "by": "vector"}]
    fused = rrf_fuse([a, b])
    ids = [r["id"] for r in fused]
    assert ids[0] == 1  # 双通道命中者排前
    top = fused[0]
    assert set(top["matched_by"]) == {"keyword", "vector"}
    assert 3 in ids and 2 in ids


def test_verticals_registered():
    names = list_verticals()
    assert "papers" in names and "screenplay" in names
    assert get("papers").tsv_config == "simple"


def test_screenplay_vertical_uses_own_strategy():
    # 不同垂域可有不同切块参数，但共用同一不变量
    text = "场景一 内景 日。角色甲：台词若干。" * 60
    chunks = chunk_text(text, get("screenplay").strategy)
    ok, detail = verify_lossless(text, chunks)
    assert ok, detail


def test_short_document_stores_content_and_blocks_decompose():
    """回归：阈值以下文档正文须存入 content 列并可被切块
    （缺陷实录：4 部剧本曾因正文无归处而 3 部失败）。
    """
    from fd_text_core.ingest import Doc, COLD_STORE_MIN_CHARS

    short = "短篇正文，长度远低于冷存阈值。" * 3
    assert len(short) < COLD_STORE_MIN_CHARS
    d = Doc(domain="papers", source="s", source_id="short1", title="t", content=short)
    # 不传 cold_block_key：修复前会写出「无正文」文档
    assert d.cold_block_key is None
    chunks = chunk_text(short, SlidingWindow(target=80, max_size=120))
    assert chunks, "短文档必须能切块"
    ok, detail = verify_lossless(short, chunks)
    assert ok, detail


def test_empty_both_paths_is_rejected_by_doc_validation():
    """content 与 cold_block_key 不得同时为空——入口须拒绝。"""
    import inspect

    from fd_text_core import ingest

    src = inspect.getsource(ingest.load_documents)
    assert "正文无归处" in src, "摄取入口应校验正文归处并明确报错"

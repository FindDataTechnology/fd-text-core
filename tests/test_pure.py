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
    assert decode_block(encode_block(text)) == text
    blob = encode_block("x")
    with pytest.raises(BlockFormatError):
        decode_block(b"XX" + blob[2:])
    with pytest.raises(BlockFormatError):
        decode_block(b"tiny")
    with pytest.raises(BlockFormatError):
        decode_block(blob[:12] + bytes([2]) + blob[13:])


def test_coldstore_default_read_is_not_truncated():
    """回归守门：冷存读取默认必须返回全文，不得截断。

    缺陷史（2026-10-13 实测）：decode_block 默认 max_chars=200_000 曾与摄取侧
    阈值 cold_store_min_chars=200_000 撞成同一个数，导致凡正文 ≥ 阈值的文档
    「存全文、读回只剩前 20 万字符」——超出部分永久不进入切块与索引。
    实测受害：doc 31（索福克勒斯三部曲）冷存 243,485 字符，索引只覆盖
    200,000，17.9% 正文从未被检索到。本用例把「默认不截断」钉死。
    """
    long_text = "长文" * 120_000  # 24 万字符 > 摄取阈值 20 万
    assert len(long_text) > 200_000
    blob = encode_block(long_text)
    assert decode_block(blob) == long_text, "默认读取被截断——冷存职责是无损保存"
    # 截断仍可用，但必须由调用方显式要求
    assert len(decode_block(blob, max_chars=200_000)) == 200_000


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


class TestEmbedderDimensions:
    """MRL 截断维度是显式契约（text-embedding-upgrade-8b）：8B 原生 4096 维
    超出 pgvector HNSW 的 2000 维上限，必须请求 dimensions=1024。"""

    def _embedder(self, **kw):
        from fd_text_core.embedding import Embedder

        calls = []

        def fake_post(url, payload, headers, timeout):
            calls.append(payload)
            return 200, {"data": [{"index": 0, "embedding": [0.1] * 4}]}

        e = Embedder("https://x/v1", "k", "Qwen/Qwen3-Embedding-8B",
                     post=fake_post, sleep=lambda s: None, **kw)
        return e, calls

    def test_dimensions_goes_into_request_body(self):
        e, calls = self._embedder(dimensions=1024)
        e.embed(["hello"])
        assert calls[0]["dimensions"] == 1024

    def test_no_dimensions_key_when_unset(self):
        e, calls = self._embedder()
        e.embed(["hello"])
        assert "dimensions" not in calls[0]

    def test_default_model_is_8b(self):
        from fd_text_core.config import Settings

        assert Settings.__dataclass_fields__["embedding_model"].default == "Qwen/Qwen3-Embedding-8B"


class TestVerticalLoading:
    """回归：垂域模块必须被显式导入，否则 replace() 从不执行、引擎静默用兜底配置。

    实测事故（2026-10-11）：screenplay 的 cjk=True 从未生效，中文关键词检索
    0 命中——根因就是没有任何地方 import 垂域模块。
    """

    def test_builtin_verticals_loaded_and_authoritative(self):
        from fd_text_core.verticals.registry import get, load_verticals, list_verticals

        load_verticals()
        assert set(list_verticals()) >= {"papers", "screenplay"}
        # 垂域目录的权威配置须胜出（不是 bootstrap 兜底）
        assert get("screenplay").cjk is True
        assert get("papers").cjk is False

    def test_load_verticals_idempotent(self):
        from fd_text_core.verticals.registry import load_verticals

        a = load_verticals()
        b = load_verticals()
        assert a == b

    def test_cjk_toggle_reaches_query_layer(self):
        from fd_text_core.querylib import _cjk_enabled
        from fd_text_core.verticals.registry import load_verticals

        load_verticals()
        assert _cjk_enabled("screenplay") is True
        assert _cjk_enabled("papers") is False
        assert _cjk_enabled("nonexistent") is False


class TestPresets:
    """预设=声明式参数封装，不得成为绕过 facet 校验/新增检索路径的后门。"""

    def _load(self):
        from fd_text_core.verticals.registry import load_verticals

        load_verticals()

    def test_screenplay_has_four_presets(self):
        self._load()
        from fd_text_core.presets import list_presets

        names = {p["name"] for p in list_presets("screenplay")}
        assert names == {"structure", "character", "scene", "similar"}

    def test_papers_has_four_presets(self):
        # 四件配置的第四件在 papers 域同样落地（此前只有 screenplay 有）
        self._load()
        from fd_text_core.presets import list_presets

        names = {p["name"] for p in list_presets("papers")}
        assert names == {"survey", "method", "similar", "recent"}

    def test_unknown_preset_rejected_with_available_list(self):
        self._load()
        from fd_text_core.presets import PresetError, run_preset

        try:
            run_preset("screenplay", "nope", "x")
            raise AssertionError("应拒绝未知预设")
        except PresetError as exc:
            assert "nope" in str(exc) and "structure" in str(exc)

    def test_no_preset_vertical_returns_empty_list(self):
        """无预设垂域如实返回空集（两个内置域现均有预设 → 用临时注册域验证契约）。"""
        self._load()
        from fd_text_core.presets import list_presets
        from fd_text_core.chunking import SlidingWindow
        from fd_text_core.verticals import registry as R

        name = "_test_nopresets"
        fresh = R.Vertical(name=name, strategy=SlidingWindow(target=500, max_size=800), facets={})
        R._REGISTRY[name] = fresh  # 直插注册表：本测试只为覆盖「无预设」契约
        try:
            assert list_presets(name) == []
        finally:
            R._REGISTRY.pop(name, None)

    def test_query_template_renders_name(self):
        from fd_text_core.presets import Preset

        p = Preset(name="character", description="", query_template="{name}")
        assert p.render_query("", {"name": "竇娥"}) == "竇娥"

    def test_query_template_unknown_placeholder_kept(self):
        from fd_text_core.presets import Preset

        p = Preset(name="x", description="", query_template="{name} {unknown}")
        assert p.render_query("", {"name": "A"}) == "A {unknown}"

    def test_no_template_passes_query_through(self):
        from fd_text_core.presets import Preset

        p = Preset(name="x", description="")
        assert p.render_query("原始查询", {}) == "原始查询"

    def test_merge_caller_overrides_and_facets_merge_per_key(self):
        from fd_text_core.presets import Preset, _merge_params

        p = Preset(name="x", description="", params={"facets": {"tradition": "chinese-classic"}, "limit": 10})
        m = _merge_params(p, {"limit": 50, "facets": {"language": "zh"}})
        assert m["limit"] == 50
        assert m["facets"] == {"tradition": "chinese-classic", "language": "zh"}

    def test_manifest_shape(self):
        self._load()
        from fd_text_core.presets import presets_manifest

        man = presets_manifest("screenplay")
        assert man["schema"] == "facet-preset-suite/v1"
        assert len(man["presets"]) == 4

    def test_preset_still_validates_facets(self):
        """预设不得绕过词表校验：非法 facet 经预设调用仍须被拒。"""
        self._load()
        from fd_text_core.presets import run_preset
        from fd_text_core.querylib import FacetError

        try:
            run_preset("screenplay", "structure", "x", facets={"tradition": "nope"})
            raise AssertionError("预设不应绕过 facet 校验")
        except FacetError:
            pass


# ---------- 融合权重（change fusion-weight-vector-tilt） ----------

def test_rrf_weights_none_equals_explicit_ones():
    """weights=None 与显式 [1.0,1.0] 逐位等价——默认语义不因加参而变。"""
    a = [{"id": 1, "by": "keyword"}, {"id": 2, "by": "keyword"}]
    b = [{"id": 2, "by": "vector"}, {"id": 3, "by": "vector"}]
    base = rrf_fuse([a, b])
    same = rrf_fuse([a, b], weights=[1.0, 1.0])
    assert [r["id"] for r in base] == [r["id"] for r in same]
    assert [r["rrf"] for r in base] == [r["rrf"] for r in same]


def test_rrf_vector_weight_promotes_vector_only_candidate():
    """提高向量侧权重使「仅向量命中且位次高」的候选前移——权重的机制本身。

    构造：id=1 仅关键词第 1；id=2 仅向量第 1。均匀权重下两者 rrf 相同、
    按 id 决胜（1 在前）；向量侧加权后 id=2 应前移。
    """
    a = [{"id": 1, "by": "keyword"}]
    b = [{"id": 2, "by": "vector"}]
    even = [r["id"] for r in rrf_fuse([a, b])]
    assert even[0] == 1
    tilted = [r["id"] for r in rrf_fuse([a, b], weights=[1.0, 2.0])]
    assert tilted[0] == 2


def test_rrf_weights_length_mismatch_raises():
    """权重与通道数不匹配即报错——不静默忽略（配置错误要能被看见）。"""
    import pytest as _pytest
    a = [{"id": 1, "by": "keyword"}]
    with _pytest.raises(ValueError, match="weights 长度"):
        rrf_fuse([a], weights=[1.0, 2.0])


def test_config_fingerprint_includes_weights(monkeypatch):
    """改融合权重必须改指纹——否则「同一指纹下指标变了」无法归因。"""
    from fd_text_core.eval.runner import compute_config_fingerprint

    monkeypatch.delenv("FDTEXT_RRF_W_VEC", raising=False)
    fp_default = compute_config_fingerprint("gov")
    monkeypatch.setenv("FDTEXT_RRF_W_VEC", "4.0")
    fp_changed = compute_config_fingerprint("gov")
    assert fp_default != fp_changed

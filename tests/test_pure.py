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

    # 归处判定在 2026-11-08 的批量化里搬进了 _placement（逐条与批量共用同一份判定）
    src = inspect.getsource(ingest._placement)
    assert "正文无归处" in src, "摄取入口应校验正文归处并明确报错"


def test_doc_strips_nul_bytes_from_content_title_and_metadata():
    """回归：正文里的 NUL 必须于入口清除。

    实录（2026-11-08）：fd-info-ship-spool 连续 13 天每 5 分钟失败，原因是一条
    `psycopg.DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes`——
    5000 条待入库被 13 条脏数据整体拖死。脏数据落盘形如 JSON 转义 `\\u0000`
    （6 个 ASCII 字符，原始字节里根本没有 0x00），`json.loads` 之后才是真 NUL。
    因此这里同时断言「原始字节无损、解码后有 NUL」这一前提，避免测试自身造错场景。
    """
    import hashlib
    import json

    from fd_text_core.ingest import Doc

    dirty_content = "正文左\u0000右"
    # 前提：JSON 里是转义串，字节里没有 0x00（正是它躲过字节级扫描的原因）
    payload = json.dumps({"content": dirty_content}, ensure_ascii=False)
    assert b"\x00" not in payload.encode("utf-8")
    assert json.loads(payload)["content"] == dirty_content, "解码后才出现 NUL"

    d = Doc(
        domain="news",
        source="_t_nul",
        source_id="nul1",
        title="标\u0000题",
        content=dirty_content,
        metadata={"entities": ["A\u0000B"], "raw_title": "t\u0000t", "\u0000k": "v"},
    )

    assert "\x00" not in d.content and d.content == "正文左右"
    assert "\x00" not in (d.title or "") and d.title == "标题"
    assert "\x00" not in json.dumps(d.metadata), "metadata 里也不许残留（jsonb 同样拒收）"
    assert d.metadata["entities"] == ["AB"]
    assert "k" in d.metadata, "dict 的键也要清洗"
    # 清洗在 content_hash 之前：脏数据反复摄取应得到同一哈希（否则每轮都判为新版）
    assert d.content_hash == hashlib.sha256("正文左右".encode("utf-8")).hexdigest()


def test_doc_leaves_clean_text_untouched_and_silent(caplog):
    """干净文档不得被改动，也不得刷警告（否则日志里全是噪音）。"""
    import logging

    from fd_text_core.ingest import Doc

    with caplog.at_level(logging.WARNING, logger="fd_text_core.ingest"):
        d = Doc(domain="news", source="_t_clean", source_id="c1",
                title="题", content="正文", metadata={"lang": "zh"})
    assert (d.title, d.content, d.metadata) == ("题", "正文", {"lang": "zh"})
    assert not [r for r in caplog.records if "NUL" in r.getMessage()]


def test_dedup_query_pins_current_status():
    """回归：去重查询必须只看 `status='current'` 的那一行。

    实录（2026-11-08，fd-info-ship-spool 追赶期）：
      `psycopg.errors.UniqueViolation: … uq_document_current_only`
      `DETAIL: Key (domain, source, source_id)=(news, cninfo, 1225615052) already exists.`
    生产库上对同一 key 直查取证（只读）：
      · 不带 status 的查询 → id 27884 **superseded**（h=7f8a52cf0537）
      · 带 status='current' → id 28423 current（h=5642894e63ec）
    即引擎拿**已退役行的哈希**比对 → 判为"新版" → 把「退役」打在死行上（no-op）
    → 盲插新 current → 撞部分唯一索引。取到哪一行取决于堆上物理行序，属运气。

    为什么不用 DB 用例锁这条：行序由 PG 物理布局决定，本机造不出生产那个布局
    （试过"先插 superseded 再插 current"的最坏序，新旧实现都绿——那样的用例是自证）。
    所以这里守的是**查询形状**：去重查询必须带 status 谓词。

    2026-11-08 批量化后去重有两条路径（`_load_one` 逐条、`_load_batch` 成批），
    两条都必须钉，漏一条就等于留了同一个洞。
    """
    import inspect

    from fd_text_core import ingest

    for fn in (ingest._load_one, ingest._load_batch):
        src = inspect.getsource(fn)
        assert "status=%s" in src, f"{fn.__name__} 的去重查询必须钉 status"
        assert "CURRENT" in src, f"{fn.__name__} 的 status 谓词应绑定 CURRENT 常量"


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


def test_tsv_config_falls_back_for_unregistered_domain(monkeypatch):
    """未注册垂域不得抛 KeyError——读写两侧必须同构兜底。

    回归对象：`querylib._tsv_config` 曾直接 `get_vertical(domain).tsv_config`，
    垂域未注册即抛 KeyError，把质量门 `--eval-all` 打崩（该枚举按标注集目录
    进行，不看注册表）。`embed_worker._tsv_config` 早有兜底，这是两侧分裂。
    """
    from fd_text_core.querylib import _tsv_config

    monkeypatch.delenv("FDTEXT_TSV_CONFIG", raising=False)
    assert _tsv_config("no-such-vertical") == "simple"
    monkeypatch.setenv("FDTEXT_TSV_CONFIG", "zhparser")
    assert _tsv_config("no-such-vertical") == "zhparser"
    # 已注册垂域仍取垂域配置，不被环境变量覆盖
    assert _tsv_config("papers") == "simple"


def test_builtin_verticals_have_no_runtime_switch():
    """工具面由注册表单一真源决定：内置域一律注册，不设环境开关。

    回归对象：`FDTEXT_ENABLE_GOV` 曾让同一份代码在不同部署环境产生不同
    tools/list，使 manifest 与实况静默不一致（change fix-text-core-gov-surface D1）。
    """
    assert {"papers", "screenplay", "gov"} <= set(list_verticals())


def test_eval_all_partitions_labeled_domains_by_registry():
    """有标注集但垂域未注册 → 归入「未注册」，不进跑分路径。

    回归对象：`--eval-all` 只按磁盘目录枚举，未注册域会被送进跑分并在
    `_tsv_config` 抛 KeyError 使门崩溃（退出码 1）。这里断言切分口径本身，
    不依赖数据库与嵌入通道。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from conformance_gate import partition_labeled_by_registry

    runnable, unregistered = partition_labeled_by_registry(
        ["gov", "papers", "screenplay"], ["papers", "screenplay"]
    )
    assert runnable == ["papers", "screenplay"]
    assert unregistered == ["gov"]

    # 全部已注册时差集为空，全部可跑
    runnable, unregistered = partition_labeled_by_registry(
        ["papers", "screenplay"], ["papers", "screenplay", "gov"]
    )
    assert runnable == ["papers", "screenplay"] and unregistered == []

    # 空标注集不产生任何域
    assert partition_labeled_by_registry([], ["papers"]) == ([], [])


def test_spool_rows_strip_html_at_ingest_boundary():
    """回归：spool 里的原始 HTML 必须在摄取边界转成正文（2026-11-08 实录）。

    cn-gov 的 spool 46 条 **100% 含 HTML**（上游 fetch_detail 返回 html_content），
    入库后 1275 个 gov 切片里 88% 是标记，政务检索只能捞出标记、向量化纯烧钱。
    本用例走真解析器（纯标准库，无外部依赖），断言标签被去掉、正文关键字保留。
    """
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "scripts"))
    from load_news_spool import _strip_html_to_text  # noqa: E402

    html = (
        "<html><body>"
        '<div class="social-icon-wrapper"><a href="#"><img src="weibo.png"></a></div>'
        '<div id="content1"><p>各省、自治区、直辖市生态环境厅（局）：</p>'
        "<p>为加强监督管理，现就有关事项通知如下。</p>"
        "<p>一、检查范围。本次检查覆盖重点排污单位。</p></div>"
        "</body></html>"
    )
    body = _strip_html_to_text("cn-gov", html)
    assert body, "解析器可用时必须拿到正文"
    assert "重点排污单位" in body, "正文关键字必须保留"
    assert "social-icon" not in body and "weibo" not in body, "页脚/分享按钮必须被过滤"
    assert body.count("<") == 0, f"不应残留标签：{body[:80]}"

    # 干净正文原样返回（不去折腾非 HTML 内容）
    assert _strip_html_to_text("cninfo", "纯文本正文，无标签。") == "纯文本正文，无标签。"

    # 解析不出正文时返回空串 → 调用方按"缺正文"跳过，不落脏数据
    assert _strip_html_to_text("cn-gov", "<div></div>") == ""


def test_skip_source_types_parsing_and_default():
    """停向量化的开关语义：空=全嵌；给了名单才跳过；名单按逗号切并去空。"""
    import importlib
    import os

    from fd_text_core import embed_worker

    # 默认（本仓测试环境没设该变量）= 空集合 => 不跳过任何来源
    if "FDTEXT_EMBED_SKIP_SOURCE_TYPES" not in os.environ:
        assert embed_worker.SKIP_SOURCE_TYPES == frozenset()

    os.environ["FDTEXT_EMBED_SKIP_SOURCE_TYPES"] = " announcement , ,ministry "
    try:
        m = importlib.reload(embed_worker)
        assert m.SKIP_SOURCE_TYPES == frozenset({"announcement", "ministry"})
    finally:
        os.environ.pop("FDTEXT_EMBED_SKIP_SOURCE_TYPES", None)
        importlib.reload(embed_worker)

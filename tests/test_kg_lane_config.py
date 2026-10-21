"""垂域配置面与装配的测试（全离线）。"""
from __future__ import annotations

import pytest

from fd_text_core.kg_lane import config as cfg
from fd_text_core.kg_lane import lane, merge


def _valid_dict(**over):
    base = {
        "name": "demo",
        "entity_types": [
            {"name": "Court", "description": "法院：行使审判权的国家机关"},
            {"name": "Statute", "description": "法规：法律与司法解释等规范性文件"},
        ],
        "extraction_instructions": "抽取案件中的法院、法规与它们之间的关系。",
        "admission": {
            "external_authority_available": False,
            "evidence": "检索了公开数据库与厂商 API，未见该领域的权威图谱接口",
        },
        "corpus": {"domain": "gov", "description": "中文公文"},
    }
    base.update(over)
    return base


def test_valid_config_loads_and_validates():
    c = cfg.from_dict(_valid_dict())
    assert c.name == "demo"
    assert c.entity_type_dict()["Court"].startswith("法院")
    assert c.granularity.mode == "document"  # 默认不切碎


def test_admission_blocks_verticals_with_external_graph():
    """准入前置：已有外部权威图谱的垂域不得自建（spec 首条要求）。"""
    raw = _valid_dict(admission={"external_authority_available": True,
                                 "evidence": "OpenAlex 提供引用图"})
    with pytest.raises(cfg.ConfigError, match="external authoritative graph"):
        cfg.from_dict(raw)


def test_admission_requires_evidence():
    raw = _valid_dict(admission={"external_authority_available": False, "evidence": "查过"})
    with pytest.raises(cfg.ConfigError, match="evidence"):
        cfg.from_dict(raw)


def test_granularity_chunking_requires_measured_cost():
    """切碎语料更贵——声明切分必须带实测依据。"""
    raw = _valid_dict(granularity={"mode": "chunk", "measured_cost_note": ""})
    with pytest.raises(cfg.ConfigError, match="measured cost note"):
        cfg.from_dict(raw)


def test_granularity_chunking_accepted_with_evidence():
    raw = _valid_dict(granularity={
        "mode": "chunk",
        "measured_cost_note": "实测整篇 17 万 token，按块切分合计 28 万 token，故必须切分",
    })
    assert cfg.from_dict(raw).granularity.mode == "chunk"


def test_granularity_rejects_unknown_mode():
    raw = _valid_dict(granularity={"mode": "sentence", "measured_cost_note": "x" * 30})
    with pytest.raises(cfg.ConfigError, match="granularity mode"):
        cfg.from_dict(raw)


def test_entity_type_needs_meaningful_description():
    raw = _valid_dict(entity_types=[
        {"name": "A", "description": "太短"},
        {"name": "B", "description": "这是一个有意义的类型说明"},
    ])
    with pytest.raises(cfg.ConfigError, match="description"):
        cfg.from_dict(raw)


def test_requires_at_least_two_entity_types():
    raw = _valid_dict(entity_types=[
        {"name": "Court", "description": "法院：行使审判权的国家机关"},
    ])
    with pytest.raises(cfg.ConfigError, match=">=2"):
        cfg.from_dict(raw)


def test_duplicate_entity_types_rejected():
    raw = _valid_dict(entity_types=[
        {"name": "Court", "description": "法院：行使审判权的国家机关"},
        {"name": "Court", "description": "重复的类型名也要被拒"},
    ])
    with pytest.raises(cfg.ConfigError, match="duplicate"):
        cfg.from_dict(raw)


def test_missing_key_reports_which():
    raw = _valid_dict()
    del raw["corpus"]
    with pytest.raises(cfg.ConfigError, match="missing required key"):
        cfg.from_dict(raw)


def test_registry_register_get_list():
    cfg.clear()
    c = cfg.from_dict(_valid_dict())
    cfg.register(c)
    assert cfg.list_verticals() == ["demo"]
    assert cfg.get("demo").name == "demo"
    with pytest.raises(cfg.ConfigError, match="already registered"):
        cfg.register(c)
    cfg.clear()


# ------------------------------------------------------------ merge (2.3)

def test_book_title_stripped():
    assert merge.canonical_name("《中华人民共和国刑法》") == "中华人民共和国刑法"


def test_article_number_split():
    head, art = merge.split_statute_article("中华人民共和国合同法第四百二十四条")
    assert head == "中华人民共和国合同法"
    assert art == "第四百二十四条"


def test_article_split_with_book_titles_and_spaces():
    assert merge.canonical_name("《中华人民共和国刑法》第313条") == "中华人民共和国刑法"


def test_article_in_middle_not_split():
    # 条号在中间说明它是正文/名字的一部分，不能拆
    name = "关于第四百二十四条的解释"
    head, art = merge.split_statute_article(name)
    assert art is None and head == merge.normalize(name)


def test_merge_plan_groups_isolated_article_nodes():
    """试点问题：法规名+条号的孤立节点要能归到法规主体。"""
    names = ["中华人民共和国刑法", "《中华人民共和国刑法》第313条",
             "中华人民共和国刑法第四百二十四条", "中华人民共和国民法典"]
    plan = merge.plan_merge(names)
    assert "中华人民共和国刑法" in plan
    assert len(plan["中华人民共和国刑法"]) == 3
    assert "中华人民共和国民法典" not in plan  # 只有一个名字，不成组


def test_merge_is_idempotent():
    assert merge.is_idempotent([
        "《中华人民共和国刑法》第313条",
        "中华人民共和国合同法 第四百二十四条",
        "中华人民共和国民法典",
    ])


def test_normalize_handles_full_width_and_spaces():
    assert merge.normalize("刑法　第三百条") == "刑法第三百条"
    assert merge.normalize(" 刑法 ") == "刑法"


# ------------------------------------------------------------- lane (2.4)

def test_settings_from_env_requires_keys():
    with pytest.raises(cfg.ConfigError, match="LEXKG_LLM_BASE_URL"):
        lane.settings_from_env({})


def test_settings_from_env_parses():
    env = {
        "LEXKG_LLM_BASE_URL": "https://gw/v1", "LEXKG_LLM_API_KEY": "k",
        "LEXKG_EMBED_BASE_URL": "https://sf/v1", "LEXKG_EMBED_API_KEY": "k2",
        "LEXKG_EMBED_DIM": "1024", "LEXKG_FANOUT_CAP": "50",
    }
    s = lane.settings_from_env(env)
    assert s.llm.model == "deepseek-v4.1-flash"
    assert s.embedding.dim == 1024
    assert s.fanout_cap == 50
    assert s.embedding.provider_only == ""  # 网关注入钉扎


def test_parse_uri():
    assert lane.parse_uri("bolt://127.0.0.1:6889") == ("127.0.0.1", 6889)
    assert lane.parse_uri("127.0.0.1") == ("127.0.0.1", 6379)


def test_episode_kwargs_has_no_uuid():
    """试点坑：传 uuid 会走 get_by_uuid 路径，FalkorDB 下连接被服务端关闭。"""
    c = cfg.from_dict(_valid_dict())
    kw = lane.episode_kwargs(c, name="ep1", body="正文", reference_time="2020-01-01")
    assert "uuid" not in kw
    assert kw["group_id"] == "demo"
    assert set(kw["entity_types"]) == {"Court", "Statute"}
    assert "Court" in kw["custom_extraction_instructions"]


def test_entity_type_models_are_attributeless():
    """FalkorDB 不接受非空 attributes 写边，故实体模型必须无字段。"""
    c = cfg.from_dict(_valid_dict())
    models = lane._entity_type_models(c)
    for name, model in models.items():
        assert model.model_fields == {}, name
        assert model.__doc__


def test_noop_cross_encoder_returns_neutral_scores():
    import asyncio

    ce = lane._NoopCrossEncoder()
    out = asyncio.run(ce.rank("q", ["a", "b"]))
    assert out == [("a", 0.0), ("b", 0.0)]


# ------------------------------------------------- 记账包装（2.4 / 5.1 接线）
def test_classify_operation_order_matters():
    """invalidation 的提示词里常含 extract，必须先判失效。"""
    from fd_text_core.kg_lane.cost import classify_operation

    assert classify_operation("extract_nodes") == "extraction"
    assert classify_operation("invalidate_edges") == "invalidation"
    assert classify_operation("dedupe_entities") == "dedup"
    assert classify_operation(None) == "other"


def test_classify_operation_falls_back_to_message_text():
    from fd_text_core.kg_lane.cost import classify_operation

    msg = type("M", (), {"content": "Please INVALIDATE these edges"})()
    assert classify_operation(None, [msg]) == "invalidation"


def test_ledger_roundtrip_reloads_existing_records(tmp_path):
    """续跑/跨进程汇总必须能读回已有台账，否则账只看本次。"""
    from fd_text_core.kg_lane.cost import CostLedger

    path = tmp_path / "ledger.jsonl"
    first = CostLedger(path)
    first.begin_episode("ep1")
    first.record(operation="extraction", model="m", prompt_tokens=10, completion_tokens=5)

    second = CostLedger(path).load_existing()
    second.begin_episode("ep2")
    second.record(operation="embedding", model="e", meta={"texts": 3})

    reloaded = CostLedger(path).load_existing()
    agg = reloaded.aggregate()
    assert set(agg) == {"ep1", "ep2"}
    assert agg["ep1"]["extraction"]["total_tokens"] == 15
    assert agg["ep2"]["embedding"]["calls"] == 1


async def test_recording_openai_captures_usage_and_episode(tmp_path):
    from fd_text_core.kg_lane import cost as cost_mod

    class Usage:
        prompt_tokens, completion_tokens = 100, 20

    class Resp:
        usage = Usage()
        model = "deepseek-v4.1-flash"

    class Completions:
        async def create(self, **kw):
            return Resp()

    class Chat:
        completions = Completions()

    class Inner:
        chat = Chat()

    ledger = cost_mod.CostLedger(tmp_path / "l.jsonl")
    ledger.begin_episode("ep-x")
    wrapped = cost_mod.RecordingOpenAI(Inner(), ledger)

    token = cost_mod._current.set(("ep-x", "extraction"))
    try:
        await wrapped.chat.completions.create(model="deepseek-v4.1-flash")
    finally:
        cost_mod._current.reset(token)

    assert len(ledger.records) == 1
    rec = ledger.records[0]
    assert rec.episode_id == "ep-x" and rec.operation == "extraction"
    assert rec.prompt_tokens == 100 and rec.completion_tokens == 20


async def test_ledger_embedder_records_text_count(tmp_path):
    from fd_text_core.kg_lane import cost as cost_mod

    class Inner:
        async def create(self, x):
            return [0.1]

        async def create_batch(self, xs):
            return [[0.1] for _ in xs]

    ledger = cost_mod.CostLedger(tmp_path / "l.jsonl")
    ledger.begin_episode("ep-y")
    emb = cost_mod.LedgerEmbedder(Inner(), ledger, "Qwen/Qwen3-Embedding-8B")

    await emb.create_batch(["a", "b", "c"])
    assert ledger.records[0].operation == "embedding"
    assert ledger.records[0].meta == {"requests": 1, "texts": 3}
    assert ledger.records[0].episode_id == "ep-y"


# --------------------------------------------- 随仓配置必须可加载（3.1 / 4.1）
def test_shipped_vertical_configs_load_and_pass_admission():
    """legal/gov 两份随仓配置必须一直可加载——配置坏了要在 CI 就炸，不能等跑批。"""
    from pathlib import Path

    from fd_text_core.kg_lane import config

    root = Path(config.__file__).parent / "verticals"
    names = sorted(p.stem.split(".")[0] for p in root.glob("*.kg.json"))
    assert names == ["gov", "legal"], f"unexpected shipped configs: {names}"

    for name in names:
        cfg = config.load(root / f"{name}.kg.json")
        assert cfg.name == name
        assert len(cfg.entity_types) >= 2
        # 准入：自建抽取的前提是外部没有权威图谱，配置里必须留核验依据
        assert cfg.admission.external_authority_available is False
        assert len(cfg.admission.evidence) >= 10
        # 粒度：默认不切碎
        assert cfg.granularity.mode == "document"


# ------------------------------------------- 语料索引 / 溯源可达（3.3 前置）
def test_build_corpus_index_maps_episode_id_to_provenance():
    """缺这张表，查询结果的 provenance 会静默为空（试点第 2 个缺陷）。"""
    from fd_text_core.kg_lane import lane

    episodes = [
        {"episode_id": "gc:1", "kind": "guide_case",
         "provenance": {"stable_id": "guide_cases:1", "doc_title": "指导案例1号"}},
        {"episode_id": "law:9:第一条", "kind": "statute_article", "provenance": {}},
    ]
    idx = lane.build_corpus_index(episodes)
    assert set(idx) == {"gc:1", "law:9:第一条"}
    assert idx["gc:1"]["stable_id"] == "guide_cases:1"
    assert idx["gc:1"]["episode_id"] == "gc:1"
    # 没有 provenance 的条目也要能回指 episode 本身
    assert idx["law:9:第一条"]["episode_id"] == "law:9:第一条"
    assert idx["law:9:第一条"]["kind"] == "statute_article"


# ------------------- 超级边抑制阈值（kg-lane-retrieval-precision 2.1）
def test_walk_super_edge_threshold_default_and_validation():
    """缺省 50 有据可依（gov 图 HAS_DOC_TYPE 连全部 212 篇公文）；非法值拒绝。"""
    from fd_text_core.kg_lane import config

    cfg = config.from_dict({
        "name": "t",
        "entity_types": [{"name": "Agency", "description": "发文机关的语义说明，供抽取使用"},
                         {"name": "Document", "description": "公文实体的语义说明，供抽取使用"}],
        "extraction_instructions": "抽取指令必须足够长以通过校验门槛，这里补充说明。",
        "admission": {"external_authority_available": False,
                      "evidence": "核验过外部无权威图谱接口可用"},
        "corpus": {"domain": "t"},
    })
    assert cfg.walk_super_edge_threshold == 50

    bad = dict(cfg.to_dict(), walk_super_edge_threshold=0)
    try:
        config.from_dict(bad)
        raise AssertionError("threshold=0 must be rejected")
    except config.ConfigError as e:
        assert "walk_super_edge_threshold" in str(e)

    ok = config.from_dict(dict(cfg.to_dict(), walk_super_edge_threshold=120))
    assert ok.walk_super_edge_threshold == 120

"""归并层接线测试（kg-lane-delivery 2.1 + 2.2，全离线）。

管线级回归用「仿 graphiti 流」的假后端：真实 graphiti 未装进本机，但其
0.31.1 源码已钉死并被逐一核验——add_episode 的抽取流是
extract_nodes（LLM，prompt_name=extract_nodes.extract_text）
→ 按名字精确合并同名实体（_collapse_exact_duplicate_extracted_nodes）
→ extract_edges（LLM，prompt_name=extract_edges.edge，端点按名字精确
匹配节点表，匹配不上就 materialize 悬挂节点）→ 落库。
假后端只复刻这条契约中与归并相关的部分，不起真图。
"""
from __future__ import annotations

import pytest

from fd_text_core.kg_lane import config as cfg
from fd_text_core.kg_lane import lane, merge


# --------------------------------------------------------------- 假 graphiti

class _ScriptedLLM:
    """按 prompt_name 回放固定抽取响应——扮演被 MergingLLMClient 包住的内层。"""

    def __init__(self, nodes_response: dict, edges_response: dict):
        self._by_name = {
            "extract_nodes.extract_text": nodes_response,
            "extract_edges.edge": edges_response,
        }
        self.calls: list[str | None] = []

    async def generate_response(self, messages, response_model=None, max_tokens=None,
                                model_size=None, group_id=None, prompt_name=None,
                                **kwargs):
        self.calls.append(prompt_name)
        return self._by_name[prompt_name]


class _FakeGraphiti:
    """复刻 graphiti 0.31.1 add_episode 中与归并相关的最小契约。

    与真实流的关键对齐点（都是源码核验过的行为）：
      1. 抽取响应经 llm_client.generate_response(prompt_name=...) 取回；
      2. 实体名精确相等即合并（同名节点 collapse）；
      3. 边端点按名字精确匹配节点表；匹配不上 materialize 悬挂节点
         ——归并接线失效时的症状（原始名悬挂节点）会在这里暴露。
    """

    def __init__(self, llm_client):
        self.llm_client = llm_client
        self.nodes: list[dict] = []
        self.edges: list[dict] = []

    async def add_episode(self, **kwargs):
        entities = (await self.llm_client.generate_response(
            [], prompt_name="extract_nodes.extract_text"))["extracted_entities"]
        by_name: dict[str, dict] = {}
        for e in entities:  # 同名精确合并（graphiti _collapse_... 的语义）
            node = by_name.get(e["name"])
            if node is None:
                node = {"name": e["name"], "uuid": f"n{len(by_name)}"}
                by_name[e["name"]] = node
                self.nodes.append(node)
        edges = (await self.llm_client.generate_response(
            [], prompt_name="extract_edges.edge"))["edges"]
        for e in edges:
            resolved = []
            for end in ("source_entity_name", "target_entity_name"):
                node = by_name.get(e[end])
                if node is None:  # materialize 悬挂节点（真 graphiti 同款）
                    node = {"name": e[end], "uuid": f"n{len(by_name)}"}
                    by_name[e[end]] = node
                    self.nodes.append(node)
                resolved.append(node)
            self.edges.append({"source": resolved[0]["name"],
                               "target": resolved[1]["name"],
                               "fact": e["fact"]})
        return self


def _legal_dict():
    return cfg.load("fd_text_core/kg_lane/verticals/legal.kg.json")


def _raw_extraction() -> tuple[dict, dict]:
    """试点缺陷现场形态：案例援引「《中华人民共和国刑法》第313条」。"""
    nodes = {"extracted_entities": [
        {"name": "《中华人民共和国刑法》第313条", "entity_type_id": 4},
        {"name": "某某诈骗案", "entity_type_id": 5},
        {"name": "中华人民共和国刑法", "entity_type_id": 4},  # 同批已有主体名
    ]}
    edges = {"edges": [
        {"source_entity_name": "某某诈骗案",
         "target_entity_name": "《中华人民共和国刑法》第313条",
         "relation_type": "CITES", "fact": "某某诈骗案援引刑法条文定罪"},
    ]}
    return nodes, edges


# ------------------------------------------------------------ 管线级回归

async def test_article_entity_becomes_statute_node_with_article_on_edge():
    """「法规名+条号」不再成为独立实体：身份是法规主体，条号上引用边。"""
    nodes_resp, edges_resp = _raw_extraction()
    merger = lane.merger_for(_legal_dict())
    graphiti = _FakeGraphiti(lane.MergingLLMClient(_ScriptedLLM(nodes_resp, edges_resp),
                                                   merger))
    await graphiti.add_episode(name="ep1", episode_body="正文", reference_time=None)

    names = {n["name"] for n in graphiti.nodes}
    assert names == {"中华人民共和国刑法", "某某诈骗案"}, names
    assert graphiti.edges == [{
        "source": "某某诈骗案",
        "target": "中华人民共和国刑法",  # 边挂到法规主体节点
        "fact": "某某诈骗案援引刑法条文定罪（article：第313条）",  # 条号在边属性上
    }]


async def test_pipeline_rewrite_is_idempotent():
    """同一批产物重复执行归并，图与边结果不变——跑批可重入。"""
    nodes_resp, edges_resp = _raw_extraction()
    merger = lane.merger_for(_legal_dict())
    g1 = _FakeGraphiti(lane.MergingLLMClient(
        _ScriptedLLM(nodes_resp, edges_resp), merger))
    await g1.add_episode(name="ep1", episode_body="正文", reference_time=None)
    want_nodes = {n["name"] for n in g1.nodes}
    want_edges = [dict(e) for e in g1.edges]

    # 第二跑：抽取响应已是被改写过的产物（模型回显图内规范名），再过归并层
    replay_nodes = {"extracted_entities": [{"name": n} for n in sorted(want_nodes)]}
    replay_edges = {"edges": [{"source_entity_name": e["source"],
                               "target_entity_name": e["target"],
                               "relation_type": "CITES", "fact": e["fact"]}
                              for e in want_edges]}
    g2 = _FakeGraphiti(lane.MergingLLMClient(
        _ScriptedLLM(replay_nodes, replay_edges), merger))
    await g2.add_episode(name="ep1", episode_body="正文", reference_time=None)
    assert {n["name"] for n in g2.nodes} == want_nodes
    assert g2.edges == want_edges  # fact 不得二次追加 article 标记


async def test_doc_number_stays_in_identity_on_write_path():
    """文号参与身份：113 号与 71 号在写入路径上必须仍是两个实体。"""
    nodes_resp = {"extracted_entities": [
        {"name": "《行政单位财务规则》财政部令第113号"},
        {"name": "《行政单位财务规则》财政部令第71号"},
    ]}
    merger = lane.merger_for(cfg.from_dict(_gov_like_dict()))
    graphiti = _FakeGraphiti(lane.MergingLLMClient(
        _ScriptedLLM(nodes_resp, {"edges": []}), merger))
    await graphiti.add_episode(name="ep", episode_body="b", reference_time=None)
    names = {n["name"] for n in graphiti.nodes}
    assert len(names) == 2, "同名不同文号被错误合并"
    assert all("113号" in n or "71号" in n for n in names), names


# ----------------------------------------------------------------- 别名

async def test_alias_lexicon_unifies_court_identity():
    """配置词表后，「最高法」与「最高人民法院」必须同一身份。"""
    legal = _legal_dict()
    merger = lane.merger_for(legal)
    nodes_resp = {"extracted_entities": [
        {"name": "最高法"}, {"name": "最高人民法院"},
    ]}
    graphiti = _FakeGraphiti(lane.MergingLLMClient(
        _ScriptedLLM(nodes_resp, {"edges": []}), merger))
    await graphiti.add_episode(name="ep", episode_body="b", reference_time=None)
    assert {n["name"] for n in graphiti.nodes} == {"最高人民法院"}

    # 身份键层面同键；且只有配置了词表才同键（对照：空词表不映射）
    assert merger.identity("最高法") == merger.identity("最高人民法院")
    assert merge.default_merger().identity("最高法") != \
           merge.default_merger().identity("最高人民法院")


def test_alias_maps_whole_name_only_not_substring():
    """别名是整体名精确匹配：不得把「最高人民法院公报」改写成别的东西。"""
    merger = lane.merger_for(_legal_dict())
    assert merger.canonical("最高人民法院公报") == "最高人民法院公报"
    assert merger.canonical("《最高法》") == "最高人民法院"  # 书名号先剥再匹配


def test_shipped_legal_config_has_alias_lexicon_gov_has_none():
    legal = _legal_dict()
    assert dict(legal.alias_lexicon) == {
        "最高法": "最高人民法院",
        "最高法院": "最高人民法院",
        "全国人大常委会": "全国人民代表大会常务委员会",
    }
    gov = cfg.load("fd_text_core/kg_lane/verticals/gov.kg.json")
    assert gov.alias_lexicon == ()  # gov 可不带，加载不受影响


# ------------------------------------------------------------ 包装层单测

async def test_merging_client_rewrites_only_extraction_payloads():
    """只动抽取响应；去重/摘要等其他 prompt 的响应原样透传。"""
    merger = lane.merger_for(_legal_dict())
    inner = _ScriptedLLM(
        {"extracted_entities": [{"name": "最高法"}]},
        {"edges": [{"source_entity_name": "最高法", "target_entity_name": "某某案",
                    "relation_type": "TRIED", "fact": "某案由最高法审理"}]},
    )
    client = lane.MergingLLMClient(inner, merger)

    got_nodes = await client.generate_response(
        [], prompt_name="extract_nodes.extract_text")
    assert [e["name"] for e in got_nodes["extracted_entities"]] == ["最高人民法院"]

    got_edges = await client.generate_response([], prompt_name="extract_edges.edge")
    assert got_edges["edges"][0]["source_entity_name"] == "最高人民法院"
    assert got_edges["edges"][0]["fact"] == "某案由最高法审理"  # 无条号，fact 不动

    # 其他 prompt（去重/摘要/属性抽取）：响应形状不含抽取键，必须原样透传
    class Other:
        async def generate_response(self, *a, **kw):
            return {"duplicate_facts": [], "contradicted_facts": []}

    same = await lane.MergingLLMClient(Other(), merger).generate_response(
        [], prompt_name="dedupe_nodes.entity_edge", model_size="small",
        group_id="legal")
    assert same == {"duplicate_facts": [], "contradicted_facts": []}


async def test_merging_client_forwards_all_kwargs_to_inner():
    """记账层在最外层归并层之下：kwargs（model_size/group_id）必须原样转交。"""
    seen = {}

    class Recorder:
        async def generate_response(self, messages, response_model=None,
                                    max_tokens=None, model_size=None,
                                    group_id=None, prompt_name=None, **kw):
            seen.update({"model_size": model_size, "group_id": group_id,
                         "prompt_name": prompt_name, "response_model": response_model})
            return {"extracted_entities": []}

    client = lane.MergingLLMClient(
        Recorder(), merge.default_merger())
    out = await client.generate_response(
        [], response_model=int, max_tokens=10, model_size="small",
        group_id="legal", prompt_name="extract_nodes.extract_text")
    assert out == {"extracted_entities": []}
    assert seen == {"model_size": "small", "group_id": "legal",
                    "prompt_name": "extract_nodes.extract_text",
                    "response_model": int}


def test_rewrite_skips_malformed_entries_without_raising():
    """畸形条目（空名/非字符串）不改写、不抛错——抽取产物的容错不归归并层。"""
    merger = merge.default_merger()
    entities = [{"name": ""}, {"name": None}, {"no_name": 1},
                {"name": "中华人民共和国刑法第四百二十四条"}]
    got = merger.rewrite_entity_payload(entities)
    assert got[:3] == entities[:3]
    # 刑法+条号：主体名保留、条号拆出（不在这里产生条级身份）
    assert got[3]["name"] == "中华人民共和国刑法"
    # 原始产物不被就地改动
    assert entities[3]["name"] == "中华人民共和国刑法第四百二十四条"


# -------------------------------------------------------------- 配置校验

def _gov_like_dict(**over):
    base = {
        "name": "demo",
        "entity_types": [
            {"name": "Agency", "description": "发文机关：行使行政管理职能的国家机关"},
            {"name": "Document", "description": "公文：一份有文号或标题的正式文件本体"},
        ],
        "extraction_instructions": "抽取机关、公文以及它们之间的关系，注意文号。",
        "admission": {"external_authority_available": False,
                      "evidence": "核验过公开渠道，无权威图谱 API 可用"},
        "corpus": {"domain": "gov"},
    }
    base.update(over)
    return base


def test_alias_lexicon_accepts_valid_entries():
    c = cfg.from_dict(_gov_like_dict(alias_lexicon={"最高法": "最高人民法院"}))
    assert c.alias_lexicon == (("最高法", "最高人民法院"),)
    # to_dict 往返保持词表
    again = cfg.from_dict(c.to_dict())
    assert again.alias_lexicon == c.alias_lexicon


def test_alias_lexicon_rejects_self_mapping():
    with pytest.raises(cfg.ConfigError, match="self-mapping"):
        cfg.from_dict(_gov_like_dict(alias_lexicon={"最高人民法院": "最高人民法院"}))


def test_alias_lexicon_rejects_chains():
    with pytest.raises(cfg.ConfigError, match="chain"):
        cfg.from_dict(_gov_like_dict(alias_lexicon={
            "最高法": "最高人民法院", "最高人民法院": "最高审判机关"}))


def test_alias_lexicon_rejects_empty_or_non_string_entries():
    with pytest.raises(cfg.ConfigError, match="non-empty strings"):
        cfg.from_dict(_gov_like_dict(alias_lexicon={"": "最高人民法院"}))
    with pytest.raises(cfg.ConfigError, match="non-empty strings"):
        cfg.from_dict(_gov_like_dict(alias_lexicon={"最高法": "  "}))


def test_alias_lexicon_rejects_wrong_shape():
    with pytest.raises(cfg.ConfigError, match="alias_lexicon must be an object"):
        cfg.from_dict(_gov_like_dict(alias_lexicon=["最高法"]))
    with pytest.raises(cfg.ConfigError, match="str->str"):
        cfg.from_dict(_gov_like_dict(alias_lexicon={"最高法": 123}))


def test_alias_lexicon_multiple_aliases_to_same_target_ok():
    """两个别名指向同一规范名是合法且常见的（起步词表就是这个形态）。"""
    c = cfg.from_dict(_gov_like_dict(alias_lexicon={
        "最高法": "最高人民法院", "最高法院": "最高人民法院"}))
    assert len(c.alias_lexicon) == 2


# ------------------------------------------------- 既有行为零退化（锁）

def test_merge_key_semantics_unchanged_by_wiring():
    """接线后 merge 纯函数行为不变（既有 113 个断言之外的补充锁）。"""
    assert merge.merge_key("《行政单位财务规则》财政部令第113号") == \
           merge.merge_key("行政单位财务规则财政部令第113号")
    assert merge.merge_key("《行政单位财务规则》财政部令第113号") != \
           merge.merge_key("《行政单位财务规则》财政部令第71号")
    assert merge.is_idempotent([
        "《中华人民共和国刑法》第313条",
        "《行政单位财务规则》财政部令第113号",
    ])


def test_merger_split_reports_article_and_doc_number():
    sp = merge.EntityMerger().split("《中华人民共和国刑法》第313条")
    assert (sp.name, sp.article) == ("中华人民共和国刑法", "第313条")
    sp2 = merge.EntityMerger().split("《行政单位财务规则》财政部令第113号")
    assert sp2.doc_number == "第113号" and sp2.article is None
    assert "第113号" in sp2.name  # 文号内联保留：身份区分靠它


def test_docno_wrapped_form_matches_builder_inline_form():
    """跨层身份对齐（kg-lane-delivery-v1 集成期定案）：

    散文抽取的括号形态与法规层确定性建图器的 inline_docno 形态必须产出
    **同一节点名**——graphiti 节点身份就是名字，两侧各拼各的会导致同一法规
    在跨层时分裂成两个节点。
    """
    m = merge.EntityMerger()
    prose = m.canonical("《行政单位财务规则》（财政部令第113号）")
    builder = merge.inline_docno(merge.canonical_name("行政单位财务规则"), "财政部令第113号")
    assert prose == builder == "行政单位财务规则（财政部令第113号）"
    # 规范形态幂等：再归并不变
    assert m.canonical(prose) == prose
    # 同名不同文号仍可分（113 vs 71）
    p71 = m.canonical("《行政单位财务规则》（财政部令第71号）")
    assert p71 == "行政单位财务规则（财政部令第71号）" and p71 != prose


def test_docno_bare_concatenated_stays_legacy_consistent():
    """裸拼接形保持旧行为：只消费序号段，机关令名留主体，变体间内部一致。"""
    a = merge.merge_key("《行政单位财务规则》财政部令第113号")
    b = merge.merge_key("行政单位财务规则财政部令第113号")
    assert a == b == ("行政单位财务规则财政部令", "第113号")
    # 年份序号括号形（2026年第28号）整段消费，不再残留（2026年）
    assert merge.canonical_name("关于X的决定（2026年第28号）") == "关于X的决定（2026年第28号）"


def test_docno_issuer_duplicate_prefix_normalized():
    """源数据脏形态：重复机关名前缀「财政部中华人民共和国财政部令第71号」
    必须确定性归一为「中华人民共和国财政部令第71号」（实测 law_db 76231 行正文原文）。
    规范形态（113 号）不受影响；归一是幂等的。
    """
    dirty = "《行政单位财务规则》（财政部中华人民共和国财政部令第71号）"
    clean = "《行政单位财务规则》（中华人民共和国财政部令第71号）"
    assert merge.canonical_name(dirty) == "行政单位财务规则（中华人民共和国财政部令第71号）"
    # 脏/净两形态归并为同一身份
    assert merge.merge_key(dirty) == merge.merge_key(clean)
    # 113 规范形不被改写
    assert merge.canonical_name("《行政单位财务规则》（中华人民共和国财政部令第113号）") == \
        "行政单位财务规则（中华人民共和国财政部令第113号）"
    # 幂等
    once = merge.canonical_name(dirty)
    assert merge.canonical_name(once) == once

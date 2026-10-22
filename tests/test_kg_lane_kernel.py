"""KG 车道内核测试（全离线）。

四类 LLM 失败形态、MRL 嵌入语义、成本分项与外推、双时态查询与扇出上限、
评测器基线参数名——每条都对应试点里真踩过的坑，作为回归锁。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from fd_text_core.kg_lane import cost, embedding, eval as lane_eval, llm, query


# --------------------------------------------------------------------- LLM

def test_envelope_shape_a_only_properties():
    assert llm.unwrap_schema_envelope({"properties": {"a": 1}}) == {"a": 1}


def test_envelope_shape_b_with_schema_keys():
    got = llm.unwrap_schema_envelope(
        {"properties": {"duplicate_facts": []}, "type": "object"}
    )
    assert got == {"duplicate_facts": []}


def test_envelope_not_unwrapped_when_business_field_present():
    real = {"properties": {"x": 1}, "duplicate_facts": []}
    assert llm.unwrap_schema_envelope(real) == real


def test_raw_control_characters_tolerated():
    raw = '{"fact": "第一行\n第二行", "n": 1}'
    assert llm.loads_forgiving(raw) == {"fact": "第一行\n第二行", "n": 1}


def test_truncated_json_repaired():
    truncated = '{"duplicate_facts": ["事实一", "事实二"], "contradicted_facts": ["矛盾'
    got = llm.loads_forgiving(truncated)
    assert got["duplicate_facts"] == ["事实一", "事实二"]
    assert isinstance(got.get("contradicted_facts"), list)


def test_schema_artifact_value_stripped_to_none():
    dirty = {"edges": [{"invalid_at": {"anyOf": [{"type": "string"}],
                                        "title": "Invalid At"}, "fact": "某事实"}]}
    out = llm.strip_schema_artifacts(dirty)
    assert out["edges"][0]["invalid_at"] is None
    assert out["edges"][0]["fact"] == "某事实"


def test_business_dict_with_type_key_not_harmed():
    # {'type': '中级'} 是合法业务值，不能因为含 type 就当 schema 噪声
    real = {"name": "某法院", "level": {"type": "中级"}}
    assert llm.strip_schema_artifacts(real) == real


def test_parse_pipeline_handles_fence_plus_envelope():
    raw = '```json\n{"properties": {"fact": "x"}, "type": "object"}\n```'
    assert llm.parse_llm_json(raw) == {"fact": "x"}


def test_unparseable_raises_with_snippet():
    with pytest.raises(ValueError, match="unparseable"):
        llm.loads_forgiving("完全不是 JSON 的一段话")


# --------------------------------------------------------------- embedding

class _FakeEmbeddings:
    def __init__(self, dim=None, fail_dim=None):
        self.calls = []
        self.dim = dim
        self.fail_dim = fail_dim

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        dim = self.fail_dim or kwargs.get("dimensions") or 4
        n = len(kwargs["input"]) if isinstance(kwargs["input"], list) else 1
        from types import SimpleNamespace

        return SimpleNamespace(data=[SimpleNamespace(embedding=[0.5] * dim) for _ in range(n)])


def _embedder(dim=1024, provider_only="", fail_dim=None):
    fake = _FakeEmbeddings(fail_dim=fail_dim)
    from types import SimpleNamespace

    client = SimpleNamespace(embeddings=fake)
    e = embedding.build_embedder(
        embedding.EmbeddingSettings(base_url="https://x/v1", api_key="k",
                                    dim=dim, provider_only=provider_only),
        client=client,
    )
    return e, fake


async def test_dimensions_always_sent_no_client_truncation():
    e, fake = _embedder(dim=1024)
    vec = await e.create("文本")
    assert fake.calls[0]["dimensions"] == 1024
    assert len(vec) == 1024  # 不是客户端截断，是上游按 dimensions 返回


async def test_dimension_mismatch_fails_loudly():
    e, _ = _embedder(dim=1024, fail_dim=4096)
    with pytest.raises(embedding.EmbeddingDimensionError):
        await e.create("文本")


async def test_provider_pin_only_when_configured():
    e1, fake1 = _embedder(provider_only="")
    await e1.create("x")
    assert "extra_body" not in fake1.calls[0]
    e2, fake2 = _embedder(provider_only="Nebius")
    await e2.create("x")
    assert fake2.calls[0]["extra_body"] == {"provider": {"only": ["Nebius"]}}


async def test_embedder_retries_transient_5xx(monkeypatch):
    """网关上游会抖（单次 502 后紧接 200）——瞬时错误必须重试，一次就挂不可接受。"""
    import asyncio as _asyncio

    e, fake = _embedder(dim=8)

    async def _no_sleep(_s):  # 测试不真等退避
        return None

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)
    calls = {"n": 0}
    real_create = _FakeEmbeddings.create

    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("500 upsteam blip")
        return await real_create(fake, **kwargs)

    fake.create = flaky
    vec = await e.create("文本")
    assert calls["n"] == 3 and len(vec) == 8


async def test_embedder_does_not_retry_client_errors():
    """4xx（配错/欠费）重试没有意义，必须立刻抛。"""

    e, fake = _embedder(dim=8)
    calls = {"n": 0}

    def err(status_code):
        ex = RuntimeError("client error")
        ex.status_code = status_code
        return ex

    async def bad(**kwargs):
        calls["n"] += 1
        raise err(401)

    fake.create = bad
    with pytest.raises(RuntimeError):
        await e.create("文本")
    assert calls["n"] == 1


# -------------------------------------------------------------------- cost

def test_ledger_records_and_aggregates(tmp_path):
    led = cost.CostLedger(tmp_path / "ledger.jsonl")
    led.begin_episode("ep1")
    led.record(operation="extraction", model="m", prompt_tokens=100, completion_tokens=50)
    led.record(operation="dedup", model="m", prompt_tokens=30, completion_tokens=10)
    led.record(operation="embedding", model="e")
    agg = led.aggregate()["ep1"]
    assert agg["extraction"]["total_tokens"] == 150
    assert agg["dedup"]["total_tokens"] == 40
    assert agg["embedding"]["calls"] == 1


def test_unknown_operation_falls_back_to_other(tmp_path):
    led = cost.CostLedger(tmp_path / "l.jsonl")
    rec = led.record(operation="weird", model="m", prompt_tokens=1)
    assert rec.operation == "other"


def test_summarize_covers_all_operations(tmp_path):
    led = cost.CostLedger(tmp_path / "l.jsonl")
    for ep in ("a", "b"):
        led.begin_episode(ep)
        led.record(operation="extraction", model="m", prompt_tokens=10, completion_tokens=5)
    s = cost.summarize(led)
    assert s["episodes"] == 2
    assert s["total_tokens"] == 30
    assert s["tokens_per_episode"] == 15
    assert "extraction" in s["by_operation"]


def test_extrapolate_declares_assumptions():
    measured = {"episodes": 2, "total_tokens": 30, "tokens_per_episode": 15}
    out = cost.extrapolate(measured, 100, usd_per_million_tokens=0.2)
    assert out["projected_tokens"] == 1500
    assert out["projected_usd"] == 0.0  # 1500/1e6*0.2 四舍五入
    assert out["assumptions"] and len(out["assumptions"]) >= 3
    assert any("单价" in a for a in out["assumptions"])


def test_extrapolate_without_measurement_raises():
    with pytest.raises(ValueError):
        cost.extrapolate({"episodes": 0}, 10)


# ------------------------------------------------------------------- query

def _edge(uuid, fact, src, tgt, valid_at=None, invalid_at=None):
    return query.EdgeRecord(uuid=uuid, fact=fact, name="REL", source_node_uuid=src,
                            target_node_uuid=tgt, episodes=[], valid_at=valid_at,
                            invalid_at=invalid_at)


class _FakeBackend:
    """按 group 分图：只有请求了正确图名才返回数据。

    这是「绕过 group_id 就查空图」的回归锁的载体——真实 FalkorDB 下拿错图
    名会静默返回空集，不会报错，所以测试必须让「拿错图名」表现为 0 结果。
    """

    def __init__(self, nodes, edges):
        self.nodes = nodes
        self.edges = edges
        self.seen_groups: list[str | None] = []

    def _check_group(self, group_id):
        self.seen_groups.append(group_id)

    async def get_nodes_by_name(self, name, group_ids):
        self._check_group(group_ids[0] if group_ids else None)
        if group_ids and "g" not in group_ids:
            return []
        return [n for n in self.nodes if n.name == name]

    async def get_nodes_by_uuids(self, uuids, group_id=None):
        self._check_group(group_id)
        if group_id not in (None, "g"):
            return []
        return [n for n in self.nodes if n.uuid in uuids]

    async def get_edges_touching_nodes(self, uuids, group_id=None, relation_types=None):
        self._check_group(group_id)
        if group_id not in (None, "g"):
            return []
        out = [e for e in self.edges
               if e.source_node_uuid in uuids or e.target_node_uuid in uuids]
        if relation_types:
            out = [e for e in out if e.name in set(relation_types)]
        return out

    async def search_edges(self, q, group_ids, limit):
        return self.edges[:limit]

    async def get_episode_names(self, uuids, group_id=None):
        self._check_group(group_id)
        return {}

    async def get_relation_degrees(self, group_id=None):
        # 缺省无度数信息 → walk 不做抑制（保持既有测试行为不变）
        return dict(getattr(self, "relation_degrees", {}) or {})


def _graph():
    nodes = [query.NodeRecord(uuid=f"n{i}", name=f"节点{i}") for i in range(6)]
    edges = [
        _edge("e1", "一跳到 n1", "n0", "n1",
              valid_at=datetime(2020, 1, 1, tzinfo=timezone.utc)),
        _edge("e2", "n1 到 n2", "n1", "n2",
              valid_at=datetime(2021, 1, 1, tzinfo=timezone.utc)),
        _edge("e3", "n2 已失效", "n2", "n3",
              valid_at=datetime(2019, 1, 1, tzinfo=timezone.utc),
              invalid_at=datetime(2022, 1, 1, tzinfo=timezone.utc)),
    ]
    return nodes, edges


async def test_walk_collects_hops():
    """max_hops=2 即两条边：n0->n1、n1->n2；n2->n3 属第 3 条边。"""
    nodes, edges = _graph()
    q = query.KGLaneQuery(_FakeBackend(nodes, edges), group_id="g")
    facts = await q.walk("节点0", max_hops=2)
    assert {f.fact for f in facts} == {"一跳到 n1", "n1 到 n2"}

    deeper = await q.walk("节点0", max_hops=3)
    assert {f.fact for f in deeper} == {"一跳到 n1", "n1 到 n2", "n2 已失效"}


async def test_walk_honours_fanout_cap():
    nodes, edges = _graph()
    q = query.KGLaneQuery(_FakeBackend(nodes, edges), group_id="g", fanout_cap=1)
    facts = await q.walk("节点0", max_hops=3)
    assert len(facts) <= 1  # 试点二跳曾扇出 2,539 条，必须可截断


async def test_as_of_filters_expired_fact():
    nodes, edges = _graph()
    q = query.KGLaneQuery(_FakeBackend(nodes, edges), group_id="g")
    facts = await q.walk("节点0", max_hops=3,
                         as_of=datetime(2023, 1, 1, tzinfo=timezone.utc))
    assert "n2 已失效" not in {f.fact for f in facts}
    facts_old = await q.walk("节点0", max_hops=3,
                             as_of=datetime(2020, 6, 1, tzinfo=timezone.utc))
    assert "n2 已失效" in {f.fact for f in facts_old}


def test_parse_dt_handles_iso_strings():
    """FalkorDB 返回 ISO 字符串；不转 datetime 会让 as_of 过滤变空操作。"""
    dt = query.parse_dt("2020-01-01T00:00:00+00:00")
    assert isinstance(dt, datetime) and dt.year == 2020
    assert query.parse_dt(None) is None
    assert query.parse_dt("not-a-date") is None


def test_fact_valid_at_open_ended():
    e = _edge("x", "f", "a", "b", valid_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert query.fact_valid_at(e, datetime(2025, 1, 1, tzinfo=timezone.utc))
    assert not query.fact_valid_at(e, datetime(2019, 1, 1, tzinfo=timezone.utc))


def test_fact_valid_at_accepts_naive_as_of():
    """评测题的 as_of 常是裸日期（naive），不能因此抛 TypeError。"""
    e = _edge("x", "f", "a", "b", valid_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert query.fact_valid_at(e, datetime(2021, 1, 1))          # naive 按 UTC
    assert not query.fact_valid_at(e, datetime(2019, 1, 1))


class _CloningDriver:
    """只有 clone 到正确图才有数据——模拟 graphiti 的按 group 切图。"""

    def __init__(self):
        self.default_group_id = "default_db"
        self._db = "default_db"

    def clone(self, database):
        c = _CloningDriver()
        c._db = database
        c.default_group_id = self.default_group_id
        return c

    async def execute_query(self, cypher, **params):
        if self._db == "default_db":
            return ([], None, None)  # 空图：绕过切图的可检测症状
        return ([{"uuid": "e1", "fact": "事实", "name": "REL",
                  "source_node_uuid": "a", "target_node_uuid": "b",
                  "episodes": [], "valid_at": "2020-01-01T00:00:00+00:00",
                  "invalid_at": None, "expired_at": None,
                  "reference_time": None}], None, None)


class _GraphitiStub:
    def __init__(self):
        self.driver = _CloningDriver()


async def test_backend_clones_graph_and_does_not_hit_empty_default():
    """回归锁：绕过 clone(database=group_id) 会静默查空图（试点最狠的坑）。"""
    backend = query.GraphitiQueryBackend(_GraphitiStub())
    edges = await backend.get_edges_touching_nodes(["a"], group_id="pilot")
    assert edges and edges[0].fact == "事实"
    assert edges[0].valid_at is not None  # 时效字段必须映射
    # 若实现退回 self.graphiti.driver（default_db），上面两条会同时失败


# -------------------------------------------------------------------- eval

def _q(qid="T1", kind="temporal"):
    return lane_eval.EvalQuestion(id=qid, kind=kind, query="查询", expectation="e")


async def test_runner_isolates_errors_and_keeps_going():
    class BadBaseline:
        async def call(self, tool, arguments):
            raise RuntimeError("boom")

    async def kg(q):
        return [{"fact": "ok"}]

    runner = lane_eval.DualEvalRunner(BadBaseline(), kg)
    report = await runner.run([_q(), _q("T2")])
    assert len(report.results) == 2
    assert all('"error"' in r.baseline_evidence[0] for r in report.results)
    assert all(r.kg_evidence[0]["fact"] == "ok" for r in report.results)


async def test_baseline_arg_name_mapped_per_tool():
    """回归锁：guide_cases_search 收 title_query，统一传 query 会把参数错误伪装成答不出。"""
    seen = {}

    class Rec:
        async def call(self, tool, arguments):
            seen[tool] = arguments
            return ["hit"]

    runner = lane_eval.DualEvalRunner(
        Rec(), None,
        baseline_tools={"multihop": ("semantic_search", "guide_cases_search")},
        baseline_arg_names={"semantic_search": "query", "guide_cases_search": "title_query"},
    )
    await runner.run([_q("M1", "multihop")], kg=False)
    assert seen["semantic_search"] == {"query": "查询"}
    assert seen["guide_cases_search"] == {"title_query": "查询"}


async def test_summary_records_reverse_conclusion():
    """反向结论必须如实计入（KG 不如基线时不能被隐藏）。"""
    class OK:
        async def call(self, tool, arguments):
            return ["hit"]

    async def kg(q):
        return []

    runner = lane_eval.DualEvalRunner(OK(), kg)
    report = await runner.run([_q()])
    s = report.summary()
    assert s["baseline_hits"] == 1 and s["kg_hits"] == 0
    assert s["baseline_only"] == 1 and s["kg_only"] == 0


def test_load_questions_rejects_bad_kind(tmp_path):
    p = tmp_path / "q.json"
    p.write_text(json.dumps({"questions": [{"id": "x", "kind": "nope", "query": "q"}]}))
    with pytest.raises(ValueError, match="kind"):
        lane_eval.load_questions(str(p))


def test_load_questions_accepts_list_and_wrapper(tmp_path):
    qs = [{"id": "a", "kind": "temporal", "query": "q"}]
    p1 = tmp_path / "a.json"
    p1.write_text(json.dumps(qs))
    assert lane_eval.load_questions(str(p1))[0].id == "a"
    p2 = tmp_path / "b.json"
    p2.write_text(json.dumps({"questions": qs}))
    assert lane_eval.load_questions(str(p2))[0].id == "a"


def test_load_questions_reads_as_of(tmp_path):
    """时点题的 as_of 必须进 EvalQuestion——否则「按 valid_at 过滤」无法执行。"""
    qs = [{"id": "a", "kind": "temporal", "query": "q", "as_of": "2020-06-01"},
          {"id": "b", "kind": "temporal", "query": "q"}]
    p = tmp_path / "q.json"
    p.write_text(json.dumps({"questions": qs}))
    got = lane_eval.load_questions(str(p))
    assert got[0].as_of == "2020-06-01"
    assert got[1].as_of == ""


def test_report_jsonl_roundtrip(tmp_path):
    r = lane_eval.EvalResult(question=_q(), baseline_evidence=["b"], kg_evidence=[{"fact": "k"}])
    r.verdict = "kg_wins"
    path = tmp_path / "out.jsonl"
    lane_eval.EvalReport([r]).to_jsonl(str(path))
    got = json.loads(path.read_text().strip())
    assert got["verdict"] == "kg_wins" and got["kg_evidence"][0]["fact"] == "k"


async def test_walk_passes_group_id_to_every_backend_call():
    """回归锁：查询面必须把图名带到每个后端调用。

    真实 FalkorDB 拿错图名**不报错，只返回空集**——试点最严重的缺陷就长这样
    （walk('陶德华') 0 条 -> 修好后 48 条）。所以这里断言三件事：
    ①结果非空（拿错图会空）②每次调用都带了正确 group ③没有一次是 None。
    """
    nodes, edges = _graph()
    backend = _FakeBackend(nodes, edges)
    q = query.KGLaneQuery(backend, group_id="g")
    facts = await q.walk("节点0", max_hops=2)

    assert facts, "walk returned nothing — likely queried the wrong graph"
    assert backend.seen_groups, "backend never received a group_id"
    assert set(backend.seen_groups) == {"g"}, f"wrong groups passed: {backend.seen_groups}"


# ------------------------------- null 归一（EdgeDuplicate 现场缺陷，2026-10-20）
def test_null_to_empty_list_normalizes_declared_keys_only():
    """模型对「无重复/无矛盾」常回 null，而 pydantic 要求 list。

    只归一调用方声明的键——猜键名会把真正的缺失数据填成空列表。
    """
    from fd_text_core.kg_lane import llm

    raw = {"duplicate_facts": None, "contradicted_facts": None, "other": None}
    out = llm.null_to_empty_list(raw, ("duplicate_facts", "contradicted_facts"))
    assert out["duplicate_facts"] == []
    assert out["contradicted_facts"] == []
    assert out["other"] is None, "未声明的键必须原样保留"


def test_parse_llm_json_normalizes_null_list_field():
    from fd_text_core.kg_lane import llm

    out = llm.parse_llm_json('{"duplicate_facts": null, "contradicted_facts": null}',
                             list_keys=("duplicate_facts", "contradicted_facts"))
    assert out == {"duplicate_facts": [], "contradicted_facts": []}


def test_list_fields_reads_pydantic_annotations():
    from pydantic import BaseModel

    from fd_text_core.kg_lane import llm

    class M(BaseModel):
        items: list[str]
        name: str
        maybe: list[int] | None = None

    assert set(llm._list_fields(M)) == {"items", "maybe"}
    assert llm._list_fields(None) == ()


def test_default_baseline_arg_names_match_real_tool_signatures():
    """默认映射必须与 fd-legal-search-mcp 的实际形参名一致。

    现状（2026-10-20 复核 tools_law.py/tools_guide.py/tools_corpus.py）：
    `law_semantic_search(query)`；`law_search/guide_cases_search/legal_docs_search/
    wenshu_search(title_query)`。映射写错会让基线表现看起来更差，把结论带偏。
    """
    from fd_text_core.kg_lane import eval as ev

    runner = ev.DualEvalRunner(baseline=None, kg_query_fn=None)
    assert runner.baseline_arg_names["law_semantic_search"] == "query"
    for tool in ("law_search", "guide_cases_search", "legal_docs_search", "wenshu_search"):
        assert runner.baseline_arg_names[tool] == "title_query", tool
    # 默认基线工具也应是真实存在的名字，不是占位的 semantic_search
    for tools in runner.baseline_tools.values():
        for t in tools:
            assert t in runner.baseline_arg_names, f"{t} has no declared arg name"


async def test_mcp_client_list_tools_parses_param_names():
    """跑分前核对基线签名：不同工具查询形参名不同，映射错了会把参数错误当答不出。"""
    from fd_text_core.kg_lane.eval import McpBaselineClient

    client = McpBaselineClient("http://x.invalid/mcp")
    seen = []

    async def fake_post(payload):
        seen.append(payload["method"])
        return ({
            "result": {
                "tools": [
                    {"name": "law_semantic_search",
                     "inputSchema": {"properties": {"query": {}, "top_k": {}}}},
                    {"name": "guide_cases_search",
                     "inputSchema": {"properties": {"title_query": {}, "limit": {}}}},
                ]
            }
        }, {})

    client._post = fake_post  # type: ignore[method-assign]
    client._initialized = True
    tools = await client.list_tools()
    assert tools["law_semantic_search"] == ["query", "top_k"]
    assert tools["guide_cases_search"] == ["limit", "title_query"]
    assert seen == ["tools/list"]


# -------------------------------------- 截断修复：括号顺序与字符串内括号（10-21）
def test_close_unterminated_uses_lifo_order():
    """括号必须后进先出补：`{`→`[`→`{` 的收尾是 `}]}`，不是 `]}}`。

    这是实测缺陷：旧实现按固定顺序先补 `]` 再补 `}`，造出 `]}}` 仍解析失败，
    导致整条 episode 报废。
    """
    from fd_text_core.kg_lane import llm

    raw = '{"entities": [{"name": "X"'
    fixed = llm._close_unterminated(raw)
    assert fixed == '{"entities": [{"name": "X"}]}'
    import json
    assert json.loads(fixed)["entities"][0]["name"] == "X"


def test_close_unterminated_ignores_brackets_inside_strings():
    """括号计数不能数字符串内部的括号。

    旧实现用 `candidate.count("[") - candidate.count("]")`，字符串里出现
    方括号就会数错，补出畸形 JSON。必须逐字符扫描并跳过字符串内部。
    """
    from fd_text_core.kg_lane import llm
    import json

    raw = '{"note": "含[方括号]与{花括号}'  # 字符串内不配平，整体也缺右括号
    fixed = llm._close_unterminated(raw)
    parsed = json.loads(fixed)
    assert parsed["note"] == "含[方括号]与{花括号}"


def test_parse_llm_json_repairs_unterminated_structure():
    """端到端：nemotron 实测的两种截断形态都要能解析。"""
    from fd_text_core.kg_lane import llm

    a = llm.parse_llm_json('{"entities": [{"name": "无锡市扬名空天产业园"')
    assert a["entities"][0]["name"] == "无锡市扬名空天产业园"

    b = llm.parse_llm_json('{"entities": [{"name": "无锡市扬名')
    assert b["entities"][0]["name"] == "无锡市扬名"


def test_close_unterminated_leaves_valid_json_untouched():
    """合法 JSON 不得被改动——修复只作用于已判失败的输入。"""
    from fd_text_core.kg_lane import llm

    raw = '{"a": {"b": 2}}'
    assert llm._close_unterminated(raw) == raw


# ------------ 未闭合字符串截断修复（kg-lane-delivery-v1 任务 1.3，10-21）
def test_close_unterminated_string_mid_value():
    """字符串值中途截断：补闭引号，再按栈逆序补括号（`}]}` 而非 `]}}`）。"""
    from fd_text_core.kg_lane import llm
    import json

    raw = '{"entities": [{"name": "最高人'
    fixed = llm._close_unterminated(raw)
    assert fixed == '{"entities": [{"name": "最高人"}]}'
    assert json.loads(fixed)["entities"][0]["name"] == "最高人"


def test_close_unterminated_string_cut_right_after_backslash():
    """截断点恰在反斜杠后：直接补 `"` 会被吃成转义引号，字符串仍未闭合。

    钉死行为：悬挂反斜杠补成完整的 `\\\\`（字面反斜杠，**保留模型原内容**）
    再闭引号——`"foo\\` 修成 `"foo\\\\"`，解析值为 `foo\\`。
    """
    from fd_text_core.kg_lane import llm
    import json

    raw = '{"name": "foo\\'
    fixed = llm._close_unterminated(raw)
    assert fixed == '{"name": "foo\\\\"}'
    assert json.loads(fixed)["name"] == "foo\\"


def test_close_unterminated_string_backslash_pairs_stay_intact():
    """完整转义（`\\\\`）结尾的**已闭合**字符串不受影响：不额外补反斜杠。

    escaped 状态在消费第二个反斜杠时已复位，不能把成对转义误判成悬挂。
    """
    from fd_text_core.kg_lane import llm

    raw = '{"s": "already\\\\"}'
    assert llm._close_unterminated(raw) == raw


def test_close_unterminated_string_inside_nested_array():
    """嵌套数组里的字符串截断 + 悬挂转义：闭引号后括号仍按栈逆序 `]}]}`。"""
    from fd_text_core.kg_lane import llm
    import json

    raw = '{"rows": [[{"v": "x\\'
    fixed = llm._close_unterminated(raw)
    assert fixed == '{"rows": [[{"v": "x\\\\"}]]}'
    assert json.loads(fixed)["rows"][0][0]["v"] == "x\\"


def test_parse_llm_json_truncated_after_backslash():
    """端到端：反斜杠后截断的输入此前整条报废（unparseable），现在必须解析成功。"""
    parsed = llm.parse_llm_json('{"entities": [{"name": "无锡市扬名\\')
    assert parsed["entities"][0]["name"] == "无锡市扬名\\"


def test_parse_llm_json_truncated_key_name():
    """键名截断：单独形态（`{"na`）修不出合法值对，走显式报错契约；
    混合形态（前面有完整键值对）经截断重试收敛到可解析前缀。
    """
    with pytest.raises(ValueError):
        llm.parse_llm_json('{"na')

    # 键没闭合但前缀有完整成员：剥掉残缺成员后仍能还原前面的数据
    a = llm.parse_llm_json('{"a": 1, "na')
    assert a == {"a": 1}
    b = llm.parse_llm_json('{"entities": [{"name": "X"}, {"na')
    assert b["entities"] == [{"name": "X"}]


# ------------------ 超级边抑制与关系类型定向（kg-lane-retrieval-precision 2.2）
def _edge_t(uuid, fact, src, tgt, rel):
    return query.EdgeRecord(uuid=uuid, fact=fact, name=rel, source_node_uuid=src,
                            target_node_uuid=tgt, episodes=[])


class _TypedBackend(_FakeBackend):
    """带关系类型与度数的替身：HAS_DOC_TYPE 是超级边，REPEALS 是目标关系。"""

    def __init__(self, nodes, edges, degrees):
        super().__init__(nodes, edges)
        self.relation_degrees = degrees

    async def get_edges_touching_nodes(self, uuids, group_id=None, relation_types=None):
        self._check_group(group_id)
        if group_id not in (None, "g"):
            return []
        out = [e for e in self.edges
               if e.source_node_uuid in uuids or e.target_node_uuid in uuids]
        if relation_types:
            out = [e for e in out if e.name in set(relation_types)]
        return out


async def test_walk_suppresses_super_edges_and_records_them():
    """度数超阈值的关系默认不展开，且必须记录抑制原因（不静默丢弃）。"""
    nodes = [query.NodeRecord(uuid="n0", name="财政部令第113号"),
             query.NodeRecord(uuid="n1", name="《行政单位财务规则》")]
    edges = [
        _edge_t("e1", "文种是命令", "n0", "n1", "HAS_DOC_TYPE"),   # 超级边
        _edge_t("e2", "废止了71号版", "n0", "n1", "REPEALS"),      # 目标关系
    ]
    backend = _TypedBackend(nodes, edges, degrees={"HAS_DOC_TYPE": 212, "REPEALS": 9})
    q = query.KGLaneQuery(backend, group_id="g", super_edge_threshold=50)

    facts = await q.walk("财政部令第113号", max_hops=1)

    got = {f.relation for f in facts}
    assert "REPEALS" in got and "HAS_DOC_TYPE" not in got
    assert q.last_suppressed == [("HAS_DOC_TYPE", 212)]


async def test_walk_relation_types_overrides_suppression():
    """显式指定关系类型优先于度数抑制——调用方定向遍历时不受启发式干扰。"""
    nodes = [query.NodeRecord(uuid="n0", name="财政部令第113号"),
             query.NodeRecord(uuid="n1", name="《行政单位财务规则》")]
    edges = [_edge_t("e1", "文种是命令", "n0", "n1", "HAS_DOC_TYPE")]
    backend = _TypedBackend(nodes, edges, degrees={"HAS_DOC_TYPE": 212})
    q = query.KGLaneQuery(backend, group_id="g", super_edge_threshold=50)

    facts = await q.walk("财政部令第113号", max_hops=1,
                         relation_types=("HAS_DOC_TYPE",))

    assert {f.relation for f in facts} == {"HAS_DOC_TYPE"}
    assert q.last_suppressed == []


async def test_walk_never_returns_empty_due_to_full_suppression():
    """全部关系超阈值时放行而非返回空集——空结果无法与「拿错图」区分。"""
    nodes = [query.NodeRecord(uuid="n0", name="A"), query.NodeRecord(uuid="n1", name="B")]
    edges = [_edge_t("e1", "A 与 B", "n0", "n1", "ONLY_REL")]
    backend = _TypedBackend(nodes, edges, degrees={"ONLY_REL": 500})
    q = query.KGLaneQuery(backend, group_id="g", super_edge_threshold=50)

    facts = await q.walk("A", max_hops=1)
    assert facts, "全抑制退化为放行，绝不返回空集"
    assert q.last_suppressed  # 但抑制原因仍要如实记录


# ------------------ 实体身份含文号（kg-lane-retrieval-precision 1.1）
def test_merge_key_distinguishes_same_name_by_doc_number():
    """同名不同文号是同一法规的不同版本，不得合并——113 号与 71 号的实测教训。"""
    from fd_text_core.kg_lane import merge

    k113 = merge.merge_key("《行政单位财务规则》（财政部令第113号）")
    k71 = merge.merge_key("《行政单位财务规则》（财政部令第71号）")
    assert k113 != k71, "同名不同文号必须得到不同身份"

    # 同名同文号（仅书名号差异）应得到同一身份
    assert merge.merge_key("《行政单位财务规则》财政部令第113号") == \
           merge.merge_key("行政单位财务规则财政部令第113号")


def test_merge_idempotent_with_doc_numbers():
    from fd_text_core.kg_lane import merge

    sample = [
        "《行政单位财务规则》（财政部令第113号）",
        "《行政单位财务规则》（财政部令第71号）",
        "中华人民共和国刑法第四百二十四条",
        "《矿产资源目录》公告2026年第28号",
    ]
    assert merge.is_idempotent(sample)


def test_drop_incomplete_items_removes_edge_missing_relation_type():
    """模型漏 relation_type 时只丢那条边，不让整篇 episode 报废（10-22 实测 2 篇）。"""
    from pydantic import BaseModel

    from fd_text_core.kg_lane.llm import drop_incomplete_items

    class Edge(BaseModel):
        relation_type: str
        fact: str

    class ExtractedEdges(BaseModel):
        edges: list[Edge]

    data = {"edges": [
        {"relation_type": "REPEALS", "fact": "废止 71 号"},
        {"fact": "缺 relation_type 的坏边"},
        {"relation_type": "AMENDS", "fact": ""},
        {"relation_type": "EFFECTIVE", "fact": "自 2023-03-01 施行"},
    ]}
    out, dropped = drop_incomplete_items(data, ExtractedEdges)
    assert dropped == 2
    assert [e["relation_type"] for e in out["edges"]] == ["REPEALS", "EFFECTIVE"]


def test_drop_incomplete_items_noop_without_model():
    from fd_text_core.kg_lane.llm import drop_incomplete_items

    data = {"edges": [{"fact": "x"}]}
    out, dropped = drop_incomplete_items(data, None)
    assert dropped == 0 and out == data

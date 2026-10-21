"""KG 工具族挂载测试（全离线，fake query backend，不起真图）。

覆盖 kg-lane-delivery-v1 任务 4.1-4.3 的代码与测试部分：
  - 三条 KG 工具的 happy path（provenance 三元组 + 时效标注逐字段断言）
  - 图缺失显式结构化错误（FalkorDB 空图陷阱：不验图就会拿空集冒充「无事实」）
  - tool-surface manifest 双向一致 + selfcheck 期望面覆盖
  - 既有面零变化回归：挂载 KG 族后 tools/list 里既有 15 工具契约逐项不变，
    差异只允许是新增项
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastmcp import Client

from fd_text_core import kg_tools
from fd_text_core.annotations import KG_TOOLS
from fd_text_core.kg_lane import query
from fd_text_core.server import mcp

REPO = Path(__file__).resolve().parents[1]
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
UTC = timezone.utc


# --------------------------------------------------------------------- fake

def _edge(uuid, fact, rel, src, tgt, episodes=(), valid_at=None, invalid_at=None):
    return query.EdgeRecord(uuid=uuid, fact=fact, name=rel, source_node_uuid=src,
                            target_node_uuid=tgt, episodes=list(episodes),
                            valid_at=valid_at, invalid_at=invalid_at)


class FakeBackend:
    """离线替身：只有图存在（graph_exists）且 group_id 正确才返回数据。

    与内核测试的 _FakeBackend 同纪律：拿错图名 / 图不存在都表现为可区分的
    状态——本类里 graph_exists 是唯一真相源，挂载层必须先问它。
    """

    def __init__(self, existing_graphs=("legal",)):
        self.existing = set(existing_graphs)
        self.seen_groups: list[str | None] = []
        self.relation_degrees = {"AMENDS": 3, "CITES": 5, "HAS_DOC_TYPE": 212}
        self.nodes = [
            query.NodeRecord(uuid="n0", name="中华人民共和国刑法（2023修正）"),
            query.NodeRecord(uuid="n1", name="中华人民共和国刑法（2017修正）"),
            query.NodeRecord(uuid="n2", name="指导案例1号"),
        ]
        self.edges = [
            _edge("e1", "2023修正版自2024年3月1日起施行并替代2017修正版", "AMENDS",
                  "n0", "n1", episodes=["ep-amend"],
                  valid_at=datetime(2024, 3, 1, tzinfo=UTC)),
            _edge("e2", "指导案例1号援引2017修正版刑法", "CITES", "n2", "n1",
                  episodes=["ep-case"],
                  valid_at=datetime(2020, 1, 1, tzinfo=UTC),
                  invalid_at=datetime(2024, 3, 1, tzinfo=UTC)),
        ]
        self.episodes = {
            "ep-amend": {"name": "law:刑法:2023修正",
                         "source_description": "kg-lane:law",
                         "created_at": "2024-03-01T08:00:00+00:00"},
            "ep-case": {"name": "gc:1", "source_description": "kg-lane:law",
                        "created_at": "2021-05-01T08:00:00+00:00"},
        }

    def _gate(self, group_id):
        self.seen_groups.append(group_id)
        return group_id in self.existing

    async def graph_exists(self, group_id):
        return group_id in self.existing

    async def get_nodes_by_name(self, name, group_ids):
        if not self._gate(group_ids[0] if group_ids else None):
            return []
        return [n for n in self.nodes if n.name == name]

    async def get_nodes_by_uuids(self, uuids, group_id=None):
        if not self._gate(group_id):
            return []
        return [n for n in self.nodes if n.uuid in uuids]

    async def get_edges_touching_nodes(self, uuids, group_id=None, relation_types=None):
        if not self._gate(group_id):
            return []
        out = [e for e in self.edges
               if e.source_node_uuid in uuids or e.target_node_uuid in uuids]
        if relation_types:
            out = [e for e in out if e.name in set(relation_types)]
        return out

    async def search_edges(self, q, group_ids, limit):
        if not self._gate(group_ids[0] if group_ids else None):
            return []
        return self.edges[:limit]

    async def search_entities(self, name):  # pragma: no cover - 未被挂载层使用
        return await self.get_nodes_by_name(name, ["legal"])

    async def get_episode_names(self, uuids, group_id=None):
        return {u: self.episodes[u]["name"] for u in uuids if u in self.episodes}

    async def get_relation_degrees(self, group_id=None):
        return dict(self.relation_degrees)

    async def get_episodes(self, uuids, group_id=None):
        if not self._gate(group_id):
            return {}
        return {u: dict(self.episodes[u]) for u in uuids if u in self.episodes}


@pytest.fixture
def fake_backend(monkeypatch):
    """注入 fake 后端 + 保证随仓 KG 配置已注册（其他测试会 clear 配置注册表）。"""
    kg_tools.load_shipped_verticals()
    backend = FakeBackend()
    monkeypatch.setattr(kg_tools, "_backend_factory", lambda: backend)
    return backend


def _call(name: str, args: dict):
    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(name, args, raise_on_error=False)

    return asyncio.run(go())


def _assert_fact_contract(fact: dict) -> None:
    """引擎 provenance 契约 + 时效标注的逐字段断言（对齐 provenance.py 三元组）。"""
    assert fact["provenance"], f"事实缺 provenance：{fact}"
    for p in fact["provenance"]:
        assert set(p) == {"source", "stable_id", "as_of"}
        assert p["source"] and p["stable_id"] and p["as_of"], f"三元组有空洞：{p}"
    # 时效标注：键必须在场（值可为 None=开放端），且是 ISO 形态
    for k in ("valid_at", "invalid_at"):
        assert k in fact
        if fact[k] is not None:
            datetime.fromisoformat(fact[k])


# --------------------------------------------------------------- happy path

def test_kg_search_facts_happy_path(fake_backend):
    r = _call("kg_search_facts", {"domain": "legal", "query": "刑法 施行"})
    assert r.is_error is False
    data = r.data
    assert data["ok"] is True
    assert data["domain"] == "legal"
    assert data["graph"] == "legal", "group_id 必须按垂域配置路由（图名=group_id）"
    assert data["count"] == 2
    for f in data["facts"]:
        _assert_fact_contract(f)
    assert data["degraded"] == []
    # 溯源可复现：stable_id 回指图内 episode_id（入图时的稳定标识）
    provs = {p["stable_id"] for f in data["facts"] for p in f["provenance"]}
    assert provs == {"law:刑法:2023修正", "gc:1"}
    assert fake_backend.seen_groups and set(fake_backend.seen_groups) == {"legal"}


def test_kg_walk_happy_path_with_suppression(fake_backend):
    r = _call("kg_walk", {"domain": "legal", "start": "中华人民共和国刑法（2023修正）",
                          "max_hops": 2})
    assert r.is_error is False
    data = r.data
    assert data["count"] >= 1
    for f in data["facts"]:
        _assert_fact_contract(f)
    # 超级边抑制必须披露（HAS_DOC_TYPE 度数 212 超阈值 50），不静默丢弃
    assert {"relation": "HAS_DOC_TYPE", "degree": 212} in data["suppressed_relations"]
    rels = {f["relation"] for f in data["facts"]}
    assert "HAS_DOC_TYPE" not in rels
    assert "AMENDS" in rels


def test_kg_walk_relation_types_override(fake_backend):
    """显式指定关系类型优先于度数抑制——定向遍历不被启发式吞掉。"""
    r = _call("kg_walk", {"domain": "legal", "start": "指导案例1号",
                          "max_hops": 1, "relation_types": ["CITES"]})
    assert r.is_error is False
    assert {f["relation"] for f in r.data["facts"]} == {"CITES"}


def test_kg_facts_as_of_filters_expired_and_keeps_contract(fake_backend):
    as_of = "2025-01-01T00:00:00+00:00"
    r = _call("kg_facts_as_of", {"domain": "legal", "query": "刑法", "as_of": as_of})
    assert r.is_error is False
    data = r.data
    assert data["as_of"] == "2025-01-01T00:00:00+00:00"
    facts = {f["fact"] for f in data["facts"]}
    # e2 于 2024-03-01 失效：2025 时点不该在；e1 施行中：该在
    assert "指导案例1号援引2017修正版刑法" not in facts
    assert "2023修正版自2024年3月1日起施行并替代2017修正版" in facts
    for f in data["facts"]:
        _assert_fact_contract(f)
    # 早时点（2021）则 e2 仍有效
    r_old = _call("kg_facts_as_of", {"domain": "legal", "query": "刑法",
                                     "as_of": "2021-06-01T00:00:00Z"})
    assert "指导案例1号援引2017修正版刑法" in {f["fact"] for f in r_old.data["facts"]}


def test_kg_facts_as_of_naive_datetime_assumed_utc(fake_backend):
    """裸日期（无时区）按 UTC 解释——图内时间是 aware，混比会 TypeError。"""
    r = _call("kg_facts_as_of", {"domain": "legal", "query": "刑法", "as_of": "2025-01-01"})
    assert r.is_error is False
    assert {f["fact"] for f in r.data["facts"]} == {
        "2023修正版自2024年3月1日起施行并替代2017修正版"}


# ------------------------------------------------------------- 错误契约

def test_missing_graph_returns_structured_error_not_empty(fake_backend):
    """图缺失显式报错：gov 图未建 -> NOT_FOUND + missing_graph，不是空集。"""
    r = _call("kg_search_facts", {"domain": "gov", "query": "通知"})
    assert r.is_error is True, "图缺失必须是失败结果，不能是 ok:true 的空结果"
    err = r.structured_content["error"]
    assert err["code"] == "NOT_FOUND"
    assert err["missing_graph"] == "gov"
    assert err["vertical"] == "gov"
    assert "不存在或未建图" in err["message"]
    assert "kg_lane_run.py" in err["message"], "错误要指出缺失依赖的补建路径"
    assert "facts" not in r.structured_content, "错误载荷里不得混入结果字段冒充查询完成"
    # 三条工具同前置：walk / as_of 也必须先验图
    for tool, args in (("kg_walk", {"domain": "gov", "start": "x"}),
                       ("kg_facts_as_of", {"domain": "gov", "query": "x",
                                           "as_of": "2024-01-01"})):
        rr = _call(tool, args)
        assert rr.is_error is True
        assert rr.structured_content["error"]["code"] == "NOT_FOUND"
        assert rr.structured_content["error"]["missing_graph"] == "gov"


def test_unknown_vertical_lists_available(fake_backend):
    r = _call("kg_search_facts", {"domain": "nope", "query": "x"})
    assert r.is_error is True
    err = r.structured_content["error"]
    assert err["code"] == "NOT_FOUND"
    assert "legal" in err["message"] and "gov" in err["message"], "要列出可选垂域"


def test_invalid_as_of_rejected(fake_backend):
    r = _call("kg_facts_as_of", {"domain": "legal", "query": "x", "as_of": "不是时间"})
    assert r.is_error is True
    assert r.structured_content["error"]["code"] == "KG_INPUT_INVALID"
    assert "ISO8601" in r.structured_content["error"]["message"]


def test_backend_unavailable_reports_upstream(monkeypatch):
    kg_tools.load_shipped_verticals()
    monkeypatch.setattr(kg_tools, "_backend_factory", lambda: None)
    r = _call("kg_search_facts", {"domain": "legal", "query": "x"})
    assert r.is_error is True
    assert r.structured_content["error"]["code"] == "UPSTREAM_UNAVAILABLE"
    assert "图库" in r.structured_content["error"]["message"]


# --------------------------------------------- manifest 双向 + selfcheck 覆盖

@pytest.fixture(scope="module")
def runtime_tools() -> dict[str, dict]:
    async def go():
        async with Client(mcp) as c:
            out = {}
            for t in await c.list_tools():
                out[t.name] = {
                    "schema": t.inputSchema,
                    "annotations": {k: getattr(t.annotations, k) for k in HINTS},
                }
            return out

    return asyncio.run(go())


def test_manifest_declares_exactly_runtime_surface(runtime_tools):
    """manifest 双向一致：tool_groups/annotations/output_structure/count 对得上。"""
    manifest = json.loads((REPO / "tool-surface.json").read_text(encoding="utf-8"))
    declared = {t for g in manifest["tool_groups"] for t in g["tools"]}
    assert declared == set(runtime_tools), "manifest 工具集与运行时双向不一致"

    ann = manifest["tool_annotations"]
    assert set(ann) == set(runtime_tools)
    for name, hints in ann.items():
        for k in HINTS:
            assert hints.get(k) == runtime_tools[name]["annotations"][k], \
                f"{name}.{k}: manifest 与运行时不符"

    out = manifest["output_structure"]
    assert set(runtime_tools) <= set(out["declared"]) | set(out["undeclared"]), \
        "有工具既未声明输出结构、也未登记为未声明（静默空缺）"
    assert not set(out["declared"]) & set(out["undeclared"])
    assert manifest["registry"]["expected_tool_count"] == len(runtime_tools)

    # KG 族作为一个 tool_group 上牌且为 commercial
    kg_group = [g for g in manifest["tool_groups"] if g["group"] == "kg"]
    assert len(kg_group) == 1
    assert set(kg_group[0]["tools"]) == set(KG_TOOLS)
    assert kg_group[0]["level"] == "commercial"


def test_selfcheck_expected_surface_covers_kg_family(runtime_tools):
    """selfcheck 的期望面推导必须覆盖 KG 族——漏了会让 selfcheck 形同虚设。"""
    sys.path.insert(0, str(REPO / "scripts"))
    import selfcheck_surface as sc

    assert sc.expected_tools() == set(runtime_tools), \
        "selfcheck 期望面与运行时不一致（KG 族是否漏进 expected_tools？）"
    m = sc.load_manifest()
    declared = sc.manifest_tools(m)
    runtime_ann = {n: spec["annotations"] for n, spec in runtime_tools.items()}
    assert sc.check_annotations(m, declared, runtime_ann) == []
    assert sc.check_output_structure(m, declared) == []


# ------------------------------------------------- 既有面零变化回归

#: 挂载 KG 族**之前**（2026-10-21，kg-lane-delivery-v1 4.2 前）tools/list 里
#: 15 个既有工具的完整契约快照：inputSchema + 四项行为属性。挂载 KG 族后，
#: 这 15 项必须逐项不变——差异只允许是新增项（spec text-kg-lane
#: 「KG 工具挂载零触碰既有面」）。
_LEGACY_SNAPSHOT = json.loads(
    '{"gov_federated":{"annotations":{"destructiveHint":false,"idempotentHint":true,"openWorldHint":false,"readOnlyHint":true},"schema":{"additionalProperties":false,"properties":{"limit":{"default":10,"type":"integer"},"query":{"type":"string"}},"required":["query"],"type":"object"}},"gov_presets":{"annotations":{"destructiveHint":false,"idempotentHint":true,"openWorldHint":false,"readOnlyHint":true},"schema":{"additionalProperties":false,"properties":{},"type":"object"}},"gov_read":{"annotations":{"destructiveHint":false,"idempotentHint":true,"openWorldHint":false,"readOnlyHint":true},"schema":{"additionalProperties":false,"properties":{"doc_id":{"type":"integer"},"limit":{"default":4000,"type":"integer"},"offset":{"default":0,"type":"integer"}},"required":["doc_id"],"type":"object"}},"gov_search":{"annotations":{"destructiveHint":false,"idempotentHint":true,"openWorldHint":false,"readOnlyHint":true},"schema":{"additionalProperties":false,"properties":{"channels":{"default":"","type":"string"},"facets":{"anyOf":[{"additionalProperties":true,"type":"object"},{"type":"null"}],"default":null},"limit":{"default":20,"type":"integer"},"name":{"default":"","type":"string"},"preset":{"default":"","type":"string"},"query":{"type":"string"}},"required":["query"],"type":"object"}},"gov_semantic_search":{"annotations":{"destructiveHint":false,"idempotentHint":true,"openWorldHint":false,"readOnlyHint":true},"schema":{"additionalProperties":false,"properties":{"facets":{"anyOf":[{"additionalProperties":true,"type":"object"},{"type":"null"}],"default":null},"limit":{"default":20,"type":"integer"},"query":{"type":"string"}},"required":["query"],"type":"object"}},"papers_federated":{"annotations":{"destructiveHint":false,"idempotentHint":false,"openWorldHint":true,"readOnlyHint":true},"schema":{"additionalProperties":false,"properties":{"limit":{"default":10,"type":"integer"},"query":{"type":"string"}},"required":["query"],"type":"object"}},"papers_presets":{"annotations":{"destructiveHint":false,"idempotentHint":true,"openWorldHint":false,"readOnlyHint":true},"schema":{"additionalProperties":false,"properties":{},"type":"object"}},"papers_read":{"annotations":{"destructiveHint":false,"idempotentHint":true,"openWorldHint":false,"readOnlyHint":true},"schema":{"additionalProperties":false,"properties":{"doc_id":{"type":"integer"},"limit":{"default":4000,"type":"integer"},"offset":{"default":0,"type":"integer"}},"required":["doc_id"],"type":"object"}},"papers_search":{"annotations":{"destructiveHint":false,"idempotentHint":true,"openWorldHint":false,"readOnlyHint":true},"schema":{"additionalProperties":false,"properties":{"channels":{"default":"","type":"string"},"facets":{"anyOf":[{"additionalProperties":true,"type":"object"},{"type":"null"}],"default":null},"limit":{"default":20,"type":"integer"},"name":{"default":"","type":"string"},"preset":{"default":"","type":"string"},"query":{"type":"string"}},"required":["query"],"type":"object"}},"papers_semantic_search":{"annotations":{"destructiveHint":false,"idempotentHint":true,"openWorldHint":false,"readOnlyHint":true},"schema":{"additionalProperties":false,"properties":{"facets":{"anyOf":[{"additionalProperties":true,"type":"object"},{"type":"null"}],"default":null},"limit":{"default":20,"type":"integer"},"query":{"type":"string"}},"required":["query"],"type":"object"}},"screenplay_federated":{"annotations":{"destructiveHint":false,"idempotentHint":true,"openWorldHint":false,"readOnlyHint":true},"schema":{"additionalProperties":false,"properties":{"limit":{"default":10,"type":"integer"},"query":{"type":"string"}},"required":["query"],"type":"object"}},"screenplay_presets":{"annotations":{"destructiveHint":false,"idempotentHint":true,"openWorldHint":false,"readOnlyHint":true},"schema":{"additionalProperties":false,"properties":{},"type":"object"}},"screenplay_read":{"annotations":{"destructiveHint":false,"idempotentHint":true,"openWorldHint":false,"readOnlyHint":true},"schema":{"additionalProperties":false,"properties":{"doc_id":{"type":"integer"},"limit":{"default":4000,"type":"integer"},"offset":{"default":0,"type":"integer"}},"required":["doc_id"],"type":"object"}},"screenplay_search":{"annotations":{"destructiveHint":false,"idempotentHint":true,"openWorldHint":false,"readOnlyHint":true},"schema":{"additionalProperties":false,"properties":{"channels":{"default":"","type":"string"},"facets":{"anyOf":[{"additionalProperties":true,"type":"object"},{"type":"null"}],"default":null},"limit":{"default":20,"type":"integer"},"name":{"default":"","type":"string"},"preset":{"default":"","type":"string"},"query":{"type":"string"}},"required":["query"],"type":"object"}},"screenplay_semantic_search":{"annotations":{"destructiveHint":false,"idempotentHint":true,"openWorldHint":false,"readOnlyHint":true},"schema":{"additionalProperties":false,"properties":{"facets":{"anyOf":[{"additionalProperties":true,"type":"object"},{"type":"null"}],"default":null},"limit":{"default":20,"type":"integer"},"query":{"type":"string"}},"required":["query"],"type":"object"}}}'
)


def test_existing_surface_unchanged_after_kg_mount(runtime_tools):
    baseline = _LEGACY_SNAPSHOT
    for name, snap in baseline.items():
        assert name in runtime_tools, f"既有工具 {name} 从 tools/list 消失"
        got = runtime_tools[name]
        assert got["schema"] == snap["schema"], (
            f"{name} 的 inputSchema 变了——KG 挂载不得触碰既有工具契约"
        )
        assert got["annotations"] == snap["annotations"], (
            f"{name} 的行为属性变了——KG 挂载不得触碰既有工具契约"
        )
    # 差异只允许是新增项：多出来的必须恰好是 KG 族
    assert set(runtime_tools) - set(baseline) == set(KG_TOOLS)
    assert set(baseline) <= set(runtime_tools)

"""车道查询面：实体检索、多跳游走（带扇出上限）、时点过滤。

纯逻辑与图后端解耦：`QueryBackend` 是协议，测试注入 fake，
live 实现是 `GraphitiQueryBackend`。

**两条从试点血泪换来的规矩**：

1. **必须走 graphiti 的 `@traceable` 切图路径**。graphiti 用 `driver.clone(database=group_id)`
   按 group 切图；直接拿 `graphiti.driver` 会静默查 `default_db` 空图——
   试点时 `walk()` 因此长期返回 0 条（修好后同为 48 条）。任何新 backend
   必须验证「切图后的图里确实有数据」。
2. **多跳必须有扇出上限**。试点实测二跳扇出 2,539 条，无上限的结果不可消费。

时点过滤依赖 edge 的 valid_at/invalid_at；FalkorDB 把它们存成 ISO 字符串，
必须转回 datetime，否则比较会抛错或静默放行（试点第二个坑）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

DEFAULT_FANOUT_CAP = 200


@dataclass
class NodeRecord:
    uuid: str
    name: str
    group_id: str | None = None
    labels: list[str] = field(default_factory=list)
    summary: str | None = None


@dataclass
class EdgeRecord:
    uuid: str
    fact: str
    name: str
    source_node_uuid: str
    target_node_uuid: str
    episodes: list[str] = field(default_factory=list)
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    expired_at: datetime | None = None
    reference_time: datetime | None = None


class QueryBackend(Protocol):
    async def get_nodes_by_name(self, name: str, group_ids: list[str]) -> list[NodeRecord]: ...

    async def get_nodes_by_uuids(self, uuids: list[str]) -> list[NodeRecord]: ...

    async def get_edges_touching_nodes(self, uuids: list[str]) -> list[EdgeRecord]: ...

    async def search_edges(self, query: str, group_ids: list[str], limit: int) -> list[EdgeRecord]: ...

    async def get_episode_names(self, uuids: list[str]) -> dict[str, str]: ...


def parse_dt(value: Any) -> datetime | None:
    """图库时间字段可能是 ISO 字符串（driver 写入前序列化），转回 datetime。"""
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def fact_valid_at(edge: EdgeRecord, at: datetime) -> bool:
    """事实在 at 时刻为真：valid_at <= at < invalid_at；None 表示开放端。"""
    if edge.valid_at is not None and at < edge.valid_at:
        return False
    if edge.invalid_at is not None and at >= edge.invalid_at:
        return False
    return True


@dataclass
class FactView:
    fact: str
    relation: str
    subject: str
    object: str
    valid_at: datetime | None
    invalid_at: datetime | None
    expired_at: datetime | None
    episode_ids: list[str]
    provenance: list[dict[str, Any]] = field(default_factory=list)

    def validity_window(self) -> dict[str, datetime | None]:
        return {"valid_at": self.valid_at, "invalid_at": self.invalid_at,
                "expired_at": self.expired_at}

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact": self.fact,
            "relation": self.relation,
            "subject": self.subject,
            "object": self.object,
            "valid_at": self.valid_at.isoformat() if self.valid_at else None,
            "invalid_at": self.invalid_at.isoformat() if self.invalid_at else None,
            "expired_at": self.expired_at.isoformat() if self.expired_at else None,
            "episode_ids": self.episode_ids,
            "provenance": self.provenance,
        }


class KGLaneQuery:
    """车道查询业务面：每条返回的事实都带 provenance 与有效性窗口。"""

    def __init__(self, backend: QueryBackend, group_id: str,
                 provenance_index: dict[str, dict[str, Any]] | None = None,
                 corpus_index: dict[str, dict[str, Any]] | None = None,
                 fanout_cap: int = DEFAULT_FANOUT_CAP,
                 super_edge_threshold: int = 50):
        self.backend = backend
        self.group_ids = [group_id]
        self.provenance_index = provenance_index or {}
        self.corpus_index = corpus_index or {}
        self.fanout_cap = fanout_cap
        self.super_edge_threshold = super_edge_threshold
        # 最近一次 walk 被抑制的关系类型及其度数——供调用方/评测报告核查，
        # 不静默丢弃（spec 要求「标注抑制原因」）。
        self.last_suppressed: list[tuple[str, int]] = []

    def _provenance_for(self, edge: EdgeRecord) -> list[dict[str, Any]]:
        return [self.provenance_index[e] for e in edge.episodes if e in self.provenance_index]

    async def _ensure_provenance(self, edges: list[EdgeRecord]) -> None:
        """按需把图内 episode uuid 解析成语料 episode id 并回填 provenance。

        懒解析：一次查询只碰它真正用到的 episode。解析不到的 uuid 静默跳过
        （半写入残留），不伪造溯源。
        """
        missing = {u for e in edges for u in e.episodes if u not in self.provenance_index}
        if not missing:
            return
        resolver = getattr(self.backend, "get_episode_names", None)
        if resolver is None:
            return
        for uuid, name in (await resolver(sorted(missing), group_id=self.group_ids[0])).items():
            entry = self.corpus_index.get(name)
            if entry is not None:
                self.provenance_index[uuid] = entry

    async def _to_views(self, edges: list[EdgeRecord],
                        as_of: datetime | None = None) -> list[FactView]:
        if as_of is not None:
            edges = [e for e in edges if fact_valid_at(e, as_of)]
        if not edges:
            return []
        await self._ensure_provenance(edges)
        nodes = await self.backend.get_nodes_by_uuids(
            [u for e in edges for u in (e.source_node_uuid, e.target_node_uuid)],
            group_id=self.group_ids[0],
        )
        names = {n.uuid: n.name for n in nodes}
        return [
            FactView(
                fact=e.fact,
                relation=e.name,
                subject=names.get(e.source_node_uuid, e.source_node_uuid),
                object=names.get(e.target_node_uuid, e.target_node_uuid),
                valid_at=e.valid_at,
                invalid_at=e.invalid_at,
                expired_at=e.expired_at,
                episode_ids=list(e.episodes),
                provenance=self._provenance_for(e),
            )
            for e in edges
        ]

    async def search_entities(self, name: str) -> list[NodeRecord]:
        return await self.backend.get_nodes_by_name(name, self.group_ids)

    async def search_facts(self, query: str, limit: int = 10,
                           as_of: datetime | None = None) -> list[FactView]:
        edges = await self.backend.search_edges(query, self.group_ids, limit)
        return await self._to_views(edges, as_of)

    async def walk(self, start_name: str, max_hops: int = 2,
                   as_of: datetime | None = None,
                   fanout_cap: int | None = None,
                   relation_types: tuple[str, ...] | None = None) -> list[FactView]:
        """从起始实体多跳游走，收集沿途事实边（去重保序），受扇出上限约束。

        **超级边抑制**：度数超过 `super_edge_threshold` 的关系类型默认不展开——
        裸跳数遍历会被这类「分类标签式」关系吸走（gov 图实测 HAS_DOC_TYPE 连着
        全部公文，G-M2 因此返回 175 条同质文种事实而错过目标废止边）。被抑制的
        类型记录在 `self.last_suppressed`，不静默丢弃。

        `relation_types` 给定时只沿这些关系展开（且不再做度数抑制——显式指定
        优先于启发式）。
        """
        if max_hops < 1:
            raise ValueError("max_hops must be >= 1")
        cap = fanout_cap if fanout_cap is not None else self.fanout_cap
        start_nodes = await self.backend.get_nodes_by_name(start_name, self.group_ids)
        if not start_nodes:
            return []
        self.last_suppressed = []

        if relation_types is None:
            degrees = await self.backend.get_relation_degrees(self.group_ids[0])
            relation_types = tuple(
                t for t, c in sorted(degrees.items())
                if c <= self.super_edge_threshold
            )
            suppressed = sorted(
                (t, c) for t, c in degrees.items() if c > self.super_edge_threshold
            )
            self.last_suppressed = suppressed
            if not relation_types and suppressed:
                # 全部关系都被抑制时宁可放行超阈值关系也不返回空集——
                # 空结果无法与「拿错图」区分（试点最严重的缺陷形态）
                relation_types = tuple(t for t, _c in suppressed)

        frontier = {n.uuid for n in start_nodes}
        seen_edges: dict[str, EdgeRecord] = {}
        visited_nodes: set[str] = set(frontier)

        for _ in range(max_hops):
            if not frontier:
                break
            batch = await self.backend.get_edges_touching_nodes(
                sorted(frontier), group_id=self.group_ids[0],
                relation_types=relation_types or None,
            )
            next_frontier: set[str] = set()
            for e in batch:
                if e.uuid not in seen_edges:
                    seen_edges[e.uuid] = e
                for node_uuid in (e.source_node_uuid, e.target_node_uuid):
                    if node_uuid not in visited_nodes:
                        next_frontier.add(node_uuid)
            visited_nodes |= next_frontier
            frontier = next_frontier
            # 扇出上限在**整跳收集完之后**判断——提前 break 会丢掉本跳已到达的边
            # （试点二跳曾扇出 2,539 条，必须可截断，但截断不应丢整跳结果）。
            if len(seen_edges) >= cap:
                break

        edges = list(seen_edges.values())[:cap]
        return await self._to_views(edges, as_of)


class GraphitiQueryBackend:
    """graphiti 后端适配层。

    **切图必须走 graphiti 的公开查询入口**（其 `@traceable` 装饰器内部会
    `driver.clone(database=group_id)`）。直接拿 `graphiti.driver` 会落在
    `default_db` 空图上——这是试点最严重的缺陷，回归测试专门锁这一点。
    """

    def __init__(self, graphiti: Any):
        self.graphiti = graphiti

    def _driver_for(self, group_id: str):
        """取切到目标图的 driver。优先 clone；退化时才用原 driver（并记录）。"""
        driver = self.graphiti.driver
        clone = getattr(driver, "clone", None)
        if clone is None:
            return driver
        return clone(database=group_id)

    async def _query(self, cypher: str, group_id: str, **params):
        driver = self._driver_for(group_id)
        records, _, _ = await driver.execute_query(cypher, **params)
        return records

    async def get_nodes_by_name(self, name: str, group_ids: list[str]) -> list[NodeRecord]:
        from graphiti_core.nodes import EntityNode

        out: list[NodeRecord] = []
        for gid in group_ids:
            driver = self._driver_for(gid)
            for n in await EntityNode.get_by_names(driver, [name], [gid]):
                out.append(NodeRecord(uuid=n.uuid, name=n.name, group_id=n.group_id,
                                      labels=list(n.labels or []), summary=n.summary))
        return out

    async def get_nodes_by_uuids(self, uuids: list[str], group_id: str | None = None) -> list[NodeRecord]:
        from graphiti_core.nodes import EntityNode

        gid = group_id or (self.graphiti.driver.default_group_id
                           if hasattr(self.graphiti.driver, "default_group_id") else "default_db")
        driver = self._driver_for(gid)
        nodes = await EntityNode.get_by_uuids(driver, uuids)
        return [NodeRecord(uuid=n.uuid, name=n.name, group_id=n.group_id,
                           labels=list(n.labels or []), summary=n.summary) for n in nodes]

    async def get_edges_touching_nodes(self, uuids: list[str],
                                       group_id: str | None = None,
                                       relation_types: tuple[str, ...] | None = None) -> list[EdgeRecord]:
        gid = group_id or self._default_group()
        # 语义关系名存在边的 name **属性**里（type(r) 恒为 RELATES_TO）——
        # 实测 gov 图 3139 条边全是同一个 Cypher 类型，按 type 过滤等于没滤
        name_filter = " AND r.name IN $relation_types" if relation_types else ""
        records = await self._query(
            f"""
            MATCH (n:Entity)-[r:RELATES_TO]-(m:Entity)
            WHERE n.uuid IN $uuids{name_filter}
            RETURN r.uuid AS uuid, r.fact AS fact, r.name AS name,
                   r.source_node_uuid AS source_node_uuid,
                   r.target_node_uuid AS target_node_uuid,
                   r.episodes AS episodes, r.valid_at AS valid_at,
                   r.invalid_at AS invalid_at, r.expired_at AS expired_at,
                   r.reference_time AS reference_time
            """,
            gid, uuids=uuids, **({"relation_types": list(relation_types)} if relation_types else {}),
        )
        return [
            EdgeRecord(
                uuid=rec["uuid"], fact=rec["fact"], name=rec["name"],
                source_node_uuid=rec["source_node_uuid"],
                target_node_uuid=rec["target_node_uuid"],
                episodes=list(rec["episodes"] or []),
                # 不映射时效字段会让 as_of 过滤变成空操作——试点第 2 个坑
                valid_at=parse_dt(rec.get("valid_at")),
                invalid_at=parse_dt(rec.get("invalid_at")),
                expired_at=parse_dt(rec.get("expired_at")),
                reference_time=parse_dt(rec.get("reference_time")),
            )
            for rec in records
        ]

    async def get_relation_degrees(self, group_id: str | None = None) -> dict[str, int]:
        """各语义关系在图内的总度数——超级边抑制的依据（一次查询，代价可控）。

        按 `r.name` 分组而非 `type(r)`：graphiti 把语义关系名写在边的 name 属性里，
        Cypher 类型恒为 RELATES_TO，按 type 分组只会得到一个无差别的大数。
        """
        gid = group_id or self._default_group()
        records = await self._query(
            "MATCH ()-[r:RELATES_TO]->() RETURN r.name AS t, count(*) AS c",
            gid,
        )
        return {rec["t"]: int(rec["c"]) for rec in records}

    async def search_edges(self, query: str, group_ids: list[str], limit: int) -> list[EdgeRecord]:
        edges = await self.graphiti.search(query, group_ids=group_ids, num_results=limit)
        return [
            EdgeRecord(
                uuid=e.uuid, fact=e.fact, name=e.name,
                source_node_uuid=e.source_node_uuid,
                target_node_uuid=e.target_node_uuid,
                episodes=list(e.episodes or []),
                valid_at=parse_dt(e.valid_at), invalid_at=parse_dt(e.invalid_at),
                expired_at=parse_dt(e.expired_at), reference_time=parse_dt(e.reference_time),
            )
            for e in edges
        ]

    async def get_episode_names(self, uuids: list[str],
                                group_id: str | None = None) -> dict[str, str]:
        gid = group_id or self._default_group()
        records = await self._query(
            "MATCH (e:Episodic) WHERE e.uuid IN $uuids RETURN e.uuid AS uuid, e.name AS name",
            gid, uuids=uuids,
        )
        return {rec["uuid"]: rec["name"] for rec in records}

    def _default_group(self) -> str:
        driver = self.graphiti.driver
        return getattr(driver, "default_group_id", None) or "default_db"

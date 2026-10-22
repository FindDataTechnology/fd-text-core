"""KG 车道查询工具族的挂载层（kg-lane-delivery-v1 D4）。

只做转发与契约适配，查询逻辑零新写——全部转发 `kg_lane/query.py` 的既有
查询面（search_facts / walk / 时点过滤）。三件适配：

1. **group_id 路由**：`domain` 参数 -> 垂域 KG 配置声明的图名
   （`kg_lane/config.py` 注册表 + `kg_lane/verticals/*.kg.json`）。
   「每次后端调用显式带 group_id」的纪律由 KGLaneQuery 维持，挂载层只选图名。
2. **provenance 契约**：每条事实补 `source`/`stable_id`/`as_of` 三元组
   （对齐 `provenance.py` 引擎契约）+ `valid_at`/`invalid_at` 时效标注。
   三元组从图内 episode 节点解析（name=episode_id 是稳定标识、created_at
   是索引时点）；解析不出的事实不伪造溯源，进 `degraded` 如实披露。
3. **图缺失显式报错**：FalkorDB 对不存在的图名不报错只返空集——转发前
   先 `graph_ready()` 验图，缺失时返回结构化错误（NOT_FOUND + 缺失依赖
   说明），绝不返回空集冒充「无事实」。

不设运行时开关：KG 工具族随服务常驻 tools/list（工具面单一真源原则，
同 verticals/registry 的设计）；图库未配置/不可达时工具如实报
UPSTREAM_UNAVAILABLE，而不是从工具面上消失。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

KG_DOMAINS_HINT = "已配置垂域图见 fd_text_core/kg_lane/verticals/*.kg.json"


def _ok(payload: dict) -> dict:
    """与 server._ok 同构（挂载层不 import server，避免环）。"""
    return {"ok": True, **payload}


def load_shipped_verticals() -> list[str]:
    """把随仓 KG 垂域配置装进 kg_lane 配置注册表（幂等，replace 语义）。

    server 模块可能被反复 import（测试收集、多进程 fork），register 的
    「拒重复」语义会炸，故统一走 replace——重复加载结果不变。
    """
    from pathlib import Path

    from .kg_lane import config as kg_cfg

    root = Path(kg_cfg.__file__).parent / "verticals"
    loaded: list[str] = []
    for p in sorted(root.glob("*.kg.json")):
        cfg = kg_cfg.load(p)
        kg_cfg.replace(cfg)
        loaded.append(cfg.name)
    return loaded


#: 查询后端装配口。默认从环境装配 FalkorDB 后端；装配不出（graphiti 未安装、
#: 图库不可达）返回 None，由工具层如实报 UPSTREAM_UNAVAILABLE。测试注入 fake
#: 后端时 monkeypatch 本属性（返回值只需满足 query.QueryBackend 协议）。
_backend_factory: Any = None  # 延迟到首次调用时绑定默认实现
_factory_error: str = ""


def _default_backend_factory():
    """默认装配：只读查询后端，不需要 LLM/嵌入凭据（隔离不变量：零 LLM）。"""
    global _factory_error
    try:
        from graphiti_core import Graphiti  # noqa: F401
        from graphiti_core.driver.falkordb_driver import FalkorDriver

        from .kg_lane.lane import parse_uri
        from .kg_lane.query import GraphitiQueryBackend
    except ModuleNotFoundError as exc:
        _factory_error = f"graphiti 未安装（{exc}）——查询后端不可用"
        return None
    uri = (os.environ.get("LEXKG_FALKORDB_URI") or "").strip() or "bolt://127.0.0.1:6889"
    host, port = parse_uri(uri)
    try:
        # 查询-only 装配：不接 LLM/嵌入凭据（隔离不变量：挂载层零 LLM）。
        # cross_encoder 必须显式给 noop——graphiti 构造期会急切装配官方
        # reranker 并要求 OPENAI_API_KEY（试点教训，见 lane.build_graphiti）；
        # llm_client/embedder 传 None 交由其「用到才解析」路径，若某版本在
        # 构造期强求，捕获后如实降级（真实图联调时在此补 stub）。
        from .kg_lane.lane import _NoopCrossEncoder

        creds = {}
        if (os.environ.get("LEXKG_FALKORDB_USERNAME") or "").strip():
            creds["username"] = os.environ["LEXKG_FALKORDB_USERNAME"].strip()
        if (os.environ.get("LEXKG_FALKORDB_PASSWORD") or "").strip():
            creds["password"] = os.environ["LEXKG_FALKORDB_PASSWORD"].strip()
        graphiti = Graphiti(graph_driver=FalkorDriver(host=host, port=port, **creds),
                            llm_client=None, embedder=None,
                            cross_encoder=_NoopCrossEncoder())
        return GraphitiQueryBackend(graphiti)
    except Exception as exc:  # noqa: BLE001 - 装配失败必须降级为显式错误，不崩服务
        _factory_error = f"图库后端装配失败：{type(exc).__name__}: {exc}"
        return None


def _backend_or_error(_err):
    """取后端；取不到时返回 (None, 结构化错误)。"""
    global _backend_factory, _factory_error
    factory = _backend_factory or _default_backend_factory
    try:
        backend = factory()
    except Exception as exc:  # noqa: BLE001 - 连接失败等：显式报上游不可用
        return None, _err(
            "UPSTREAM_UNAVAILABLE",
            f"KG 图库后端不可用：{type(exc).__name__}: {exc}",
        )
    if backend is None:
        return None, _err(
            "UPSTREAM_UNAVAILABLE",
            f"KG 图库后端未装配（{_factory_error or 'LEXKG_FALKORDB_URI 未配置或 graphiti 缺失'}）；"
            f"{KG_DOMAINS_HINT}",
        )
    return backend, None


def _resolve_vertical(domain: str, _err):
    """domain -> (group_id, walk 阈值)；未注册垂域返回结构化错误（点名可选集）。"""
    from .kg_lane import config as kg_cfg

    try:
        cfg = kg_cfg.get(domain)
    except kg_cfg.ConfigError as exc:
        return None, None, _err(
            "NOT_FOUND",
            f"未注册 KG 垂域: {domain}（{exc}）；"
            f"可选={kg_cfg.list_verticals() or '（无，先提交 *.kg.json 配置）'}",
        )
    return cfg.group_id or cfg.name, cfg.walk_super_edge_threshold, None


def _build_query(backend, group_id: str, super_edge_threshold: int):
    from .kg_lane.query import KGLaneQuery

    try:
        fanout = int(os.environ.get("LEXKG_FANOUT_CAP") or 200)
    except ValueError:
        fanout = 200
    return KGLaneQuery(backend, group_id=group_id, fanout_cap=fanout,
                       super_edge_threshold=super_edge_threshold)


def _triplet(entry: dict[str, Any] | None) -> dict[str, str] | None:
    """对齐引擎 provenance 契约：三元组齐备才有效（缺一项即 None，不伪造）。"""
    if not entry:
        return None
    out = {
        "source": str(entry.get("source") or ""),
        "stable_id": str(entry.get("stable_id") or ""),
        "as_of": str(entry.get("as_of") or ""),
    }
    if not all(out.values()):
        return None
    return out


async def _decorate_facts(views: list, query_surface, domain: str,
                          degraded: list[str]) -> list[dict[str, Any]]:
    """FactView -> 事实 dict：补 provenance 三元组，保留时效标注。

    episode 节点解析：name=episode_id（稳定标识）、source_description=采集源、
    created_at=索引时点。车道侧语料索引（corpus_index）若在挂载层可得会优先，
    这里拿不到（批跑器持有），故全部从图内 episode 节点解析——图是入图事实的
    亲历者，不靠外部索引也不至于伪造。
    """
    need = sorted({u for v in views for u in v.episode_ids})
    episodes: dict[str, dict[str, Any]] = {}
    if need:
        try:
            episodes = await query_surface.backend.get_episodes(
                need, group_id=query_surface.group_ids[0])
        except Exception as exc:  # noqa: BLE001 - 溯源解析失败不阻断事实返回，进 degraded
            degraded.append(f"provenance: episode 解析失败 {type(exc).__name__}: {exc}")
    out: list[dict[str, Any]] = []
    for v in views:
        d = v.to_dict()  # 已含 fact/relation/subject/object 与 valid_at/invalid_at/expired_at
        provs: list[dict[str, str]] = []
        for p in d.get("provenance") or []:
            t = _triplet(p)
            if t and t not in provs:
                provs.append(t)
        for uuid in v.episode_ids:
            ep = episodes.get(uuid) or {}
            created = ep.get("created_at")
            t = _triplet({
                "source": ep.get("source_description") or f"kg-lane:{domain}",
                "stable_id": ep.get("name") or uuid,
                "as_of": created.isoformat() if isinstance(created, datetime)
                else (str(created) if created else ""),
            })
            if t and t not in provs:
                provs.append(t)
        d["provenance"] = provs
        if not provs:
            degraded.append(
                f"provenance: fact {v.fact[:30]!r} 无可解析溯源（episodes={v.episode_ids}）")
        out.append(d)
    return out


def register_kg_tools(mcp_instance, _err) -> None:
    """在引擎单 MCP 服务上挂载 KG 查询工具族（server.py 唯一调用点）。"""
    from .annotations import annotations_for

    load_shipped_verticals()

    async def _surface(domain: str, _err):
        """公共前置：路由 + 验图。返回 (query_surface, group_id, error)。"""
        group_id, threshold, err_res = _resolve_vertical(domain, _err)
        if err_res is not None:
            return None, None, err_res
        backend, err_res = _backend_or_error(_err)
        if err_res is not None:
            return None, None, err_res
        surface = _build_query(backend, group_id, threshold)
        # FalkorDB 空图陷阱：不存在的图名不报错只返空集，必须先验图存在性
        try:
            ready = await surface.graph_ready()
        except Exception as exc:  # noqa: BLE001 - 验图本身连不上：上游不可用
            return None, None, _err("UPSTREAM_UNAVAILABLE",
                                    f"KG 图库不可达（验图失败）：{type(exc).__name__}: {exc}")
        if not ready:
            return None, None, _err(
                "NOT_FOUND",
                f"垂域 {domain} 的图不存在或未建图（group_id={group_id}）——"
                f"缺失依赖：FalkorDB 图库中无该图（FalkorDB 对不存在的图名只返空集不报错，"
                f"故查询前显式验证）。请先以该垂域配置跑 KG 车道建图"
                f"（scripts/kg_lane_run.py --vertical {domain}）；{KG_DOMAINS_HINT}",
                missing_graph=group_id, vertical=domain,
            )
        return surface, group_id, None

    @mcp_instance.tool(
        name="kg_search_facts",
        description="KG 车道：按自然语言查询事实（实体/关系检索），每条带 provenance 与时效标注",
        annotations=annotations_for("kg_search_facts"),
    )
    async def kg_search_facts(domain: str, query: str, limit: int = 10) -> dict:
        surface, group_id, err_res = await _surface(domain, _err)
        if err_res is not None:
            return err_res
        facts = await surface.search_facts(query, limit=limit)
        degraded: list[str] = []
        rows = await _decorate_facts(facts, surface, domain, degraded)
        return _ok({"domain": domain, "graph": group_id, "facts": rows,
                    "count": len(rows), "degraded": degraded})

    @mcp_instance.tool(
        name="kg_walk",
        description="KG 车道：从起始实体多跳游走（扇出受限、超级边抑制），收集沿途事实",
        annotations=annotations_for("kg_walk"),
    )
    async def kg_walk(domain: str, start: str, max_hops: int = 2,
                      relation_types: list[str] | None = None) -> dict:
        surface, group_id, err_res = await _surface(domain, _err)
        if err_res is not None:
            return err_res
        facts = await surface.walk(
            start, max_hops=max_hops,
            relation_types=tuple(relation_types) if relation_types else None,
        )
        degraded: list[str] = []
        rows = await _decorate_facts(facts, surface, domain, degraded)
        return _ok({
            "domain": domain, "graph": group_id, "facts": rows, "count": len(rows),
            "degraded": degraded,
            # 超级边抑制必须如实披露（不静默丢弃），供调用方定向补查
            "suppressed_relations": [
                {"relation": t, "degree": c} for t, c in surface.last_suppressed
            ],
        })

    @mcp_instance.tool(
        name="kg_facts_as_of",
        description="KG 车道：时点过滤查询——只返回 as_of 时刻为真的事实（valid_at<=as_of<invalid_at）",
        annotations=annotations_for("kg_facts_as_of"),
    )
    async def kg_facts_as_of(domain: str, query: str, as_of: str,
                             limit: int = 10) -> dict:
        surface, group_id, err_res = await _surface(domain, _err)
        if err_res is not None:
            return err_res
        try:
            at = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        except ValueError:
            return _err(
                "KG_INPUT_INVALID",
                f"as_of 不是合法 ISO8601 时间: {as_of!r}（示例：2024-06-01 或 2024-06-01T00:00:00+08:00）",
            )
        # 裸日期按 UTC 解释：图内时效字段是 aware（lane 序列化带 offset），
        # naive 与 aware 直接比较会 TypeError 而不是过滤
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        facts = await surface.search_facts(query, limit=limit, as_of=at)
        degraded: list[str] = []
        rows = await _decorate_facts(facts, surface, domain, degraded)
        return _ok({"domain": domain, "graph": group_id, "as_of": at.isoformat(),
                    "facts": rows, "count": len(rows), "degraded": degraded})

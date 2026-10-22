"""车道装配：把垂域配置 + 内核 + 图库驱动组装成可跑的管线。

设计原则：**垂域零改动**。装配只读配置，不认识任何具体垂域。
图库沿用 falkordblite（嵌入式，零 docker）——定型（server / Neo4j / PG 边表）
按试点结论推迟到 lane 规模确定之后，本模块不锁死。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import config as cfg_mod
from . import cost as cost_mod
from . import embedding as emb_mod
from . import llm as llm_mod
from . import merge as merge_mod


@dataclass
class LaneSettings:
    """车道运行参数：从环境读，也可显式给（测试/跑批）。"""

    llm: llm_mod.LLMSettings
    embedding: emb_mod.EmbeddingSettings
    falkor_uri: str = "bolt://127.0.0.1:6889"
    falkor_username: str = ""
    falkor_password: str = ""
    cost_ledger_path: str = "data/kg_cost_ledger.jsonl"
    semaphore_limit: int = 5
    fanout_cap: int = 200


def settings_from_env(env: dict[str, str] | None = None) -> LaneSettings:
    """从环境变量构造。缺关键项显式报错，不静默回落默认端点。"""
    import os

    e = dict(os.environ) if env is None else env

    def need(key: str) -> str:
        value = (e.get(key) or "").strip()
        if not value:
            raise cfg_mod.ConfigError(f"missing required env var {key}")
        return value

    return LaneSettings(
        llm=llm_mod.LLMSettings(
            base_url=need("LEXKG_LLM_BASE_URL"),
            api_key=need("LEXKG_LLM_API_KEY"),
            model=(e.get("LEXKG_LLM_MODEL") or "").strip() or llm_mod.DEFAULT_MODEL,
            max_tokens=int(e.get("LEXKG_LLM_MAX_TOKENS") or llm_mod.DEFAULT_MAX_TOKENS),
            structured_mode=(e.get("LEXKG_LLM_STRUCTURED_MODE") or "json_object").strip(),
        ),
        embedding=emb_mod.EmbeddingSettings(
            base_url=need("LEXKG_EMBED_BASE_URL"),
            api_key=need("LEXKG_EMBED_API_KEY"),
            model=(e.get("LEXKG_EMBED_MODEL") or "").strip() or emb_mod.DEFAULT_MODEL,
            dim=int(e.get("LEXKG_EMBED_DIM") or emb_mod.DEFAULT_DIM),
            provider_only=(e.get("LEXKG_EMBED_PROVIDER_ONLY") or "").strip(),
        ),
        falkor_uri=(e.get("LEXKG_FALKORDB_URI") or "").strip() or "bolt://127.0.0.1:6889",
        falkor_username=(e.get("LEXKG_FALKORDB_USERNAME") or "").strip(),
        falkor_password=(e.get("LEXKG_FALKORDB_PASSWORD") or "").strip(),
        cost_ledger_path=(e.get("LEXKG_COST_LEDGER_PATH") or "").strip()
        or "data/kg_cost_ledger.jsonl",
        semaphore_limit=int(e.get("LEXKG_SEMAPHORE_LIMIT") or 5),
        fanout_cap=int(e.get("LEXKG_FANOUT_CAP") or 200),
    )


def parse_uri(uri: str) -> tuple[str, int]:
    """bolt://host:port -> (host, port)。"""
    rest = uri.split("://", 1)[-1]
    host, _, port = rest.partition(":")
    return host, int(port or 6379)


def build_graphiti(settings: LaneSettings, *, llm_client: Any = None,
                   embedder: Any = None, cross_encoder: Any = None,
                   ledger: cost_mod.CostLedger | None = None,
                   config: cfg_mod.KGVerticalConfig | None = None) -> Any:
    """装配 Graphiti 实例。cross_encoder 必须显式给（否则 graphiti 会急切构造
    OpenAI 官方 reranker，要求 OPENAI_API_KEY，跑批时会莫名失败）。

    `ledger` 给定时自动套记账层（LLM token + 嵌入条数）；不给则完全不碰
    记账，离线装配与单测照常。

    `config` 给定时把垂域的别名词表带给归并层。归并层本身**无条件套上**
    （规则归并与垂域无关；词表只影响别名映射）——任何装配路径都不得
    静默绕过确定性归并，否则「同名不同文号」与「条级节点」两类缺陷
    会以旧形态复发。
    """
    from graphiti_core import Graphiti
    from graphiti_core.driver.falkordb_driver import FalkorDriver

    host, port = parse_uri(settings.falkor_uri)
    # 凭据只在显式配置时下发（本地嵌入式实例无鉴权）；远端实例必须带 requirepass
    creds = {}
    if settings.falkor_username:
        creds["username"] = settings.falkor_username
    if settings.falkor_password:
        creds["password"] = settings.falkor_password
    driver = FalkorDriver(host=host, port=port, **creds)

    if llm_client is None:
        llm_client = llm_mod.build_llm_client(settings.llm, ledger=ledger)
    if ledger is not None:
        # 必须两层：底层截 usage、外层按提示词判 operation。只包一层时所有
        # token 都落到 "other"，分项账单（5.1）就失去意义。
        llm_client = cost_mod.CostRecordingLLMClient(llm_client, ledger)
    llm_client = MergingLLMClient(
        llm_client,
        merger_for(config) if config is not None else merge_mod.default_merger(),
    )
    if embedder is None:
        embedder = emb_mod.build_embedder(settings.embedding)
    if ledger is not None:
        embedder = cost_mod.LedgerEmbedder(embedder, ledger, settings.embedding.model)
    if cross_encoder is None:
        cross_encoder = _NoopCrossEncoder()

    return Graphiti(
        graph_driver=driver,
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=cross_encoder,
        max_coroutines=settings.semaphore_limit,
    )


def merger_for(config: cfg_mod.KGVerticalConfig) -> merge_mod.EntityMerger:
    """垂域配置 → 归并器：别名先映射再进归并（任务 2.2）。"""
    return merge_mod.default_merger(dict(config.alias_lexicon))


try:  # graphiti 构造期对 cross_encoder 做 isinstance 校验（pydantic），必须真继承
    from graphiti_core.cross_encoder.client import CrossEncoderClient as _CrossEncoderBase
except ModuleNotFoundError:  # pragma: no cover - 无 graphiti 环境

    class _CrossEncoderBase:  # type: ignore[no-redef]
        async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
            raise NotImplementedError


class _NoopCrossEncoder(_CrossEncoderBase):  # type: ignore[misc,valid-type]
    """禁用重排：试点期不上 cross-encoder（少一个外部依赖），
    图游走 + 嵌入已够撑试点查询面。分数恒为 0，调用方不得依赖排序结果。"""

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        return [(p, 0.0) for p in passages]


try:  # graphiti 构造期对 llm_client 做 isinstance 校验（GraphitiClients 为
    # pydantic 模型），必须真继承 LLMClient，鸭子类型会被直接拒。
    from graphiti_core.llm_client.client import LLMClient as _LLMClientBase
except ModuleNotFoundError:  # pragma: no cover - 无 graphiti 环境

    class _LLMClientBase:  # type: ignore[no-redef]
        async def generate_response(self, messages: list[Any], **kwargs: Any) -> Any:
            raise NotImplementedError


def _delegate_to(attr: str):
    def __getattr__(self, name: str) -> Any:
        return getattr(getattr(self, attr), name)
    return __getattr__


class MergingLLMClient(_LLMClientBase):  # type: ignore[misc,valid-type]
    """确定性归并层的接线点（kg-lane-delivery 2.1）：包在 LLM 客户端最外层。

    **为什么是这个钩子**（graphiti 0.31.1 源码实证，不改库源码）：

    graphiti 的 `add_episode` 内部流是：`extract_nodes`（LLM 抽取实体）
    → `resolve_extracted_nodes`（与图内既有节点做 LLM 去重/合并）
    → `extract_edges`（LLM 抽边，端点按**名字精确匹配**节点表，
      匹配不上就 materialize 一个悬挂节点）
    → 落库。全部中间产物都是 graphiti 内部局部变量，没有回调/中间件
    接口；驱动层在 0.31.1 已泛化成裸 Cypher（run/execute_write），在
    那里拦截等于解析查询文本，不可行。

    唯一既在「抽取产物已成形、入图写边之前」又归我们所有的单点，是
    `clients.llm_client`：所有抽取调用都经
    `generate_prompt_response` → `llm_client.generate_response(...,
    prompt_name=...)`（graphiti_types.py:162，prompt_name 恒传），响应是
    已解析的 dict。本包装在响应返回 graphiti 之前对其做确定性改写：

    - `extract_nodes.*`（含 combined 路径）响应的 `extracted_entities[*].name`
      → `EntityMerger.canonical`（别名→canonical_name）。改写发生在
      graphiti 自己的去重**之前**，所以去重看到的是确定性身份：同名
      实体在 `_collapse_exact_duplicate_extracted_nodes` 里按名字精确
      合并，文号留在名字里保证「同名不同文号不合并」。
    - `extract_edges.*`（含 combined）响应的 `edges[*]` 端点名同步改写
      （否则 LLM 回显原始名会 materialize 出未归并的悬挂节点），
      端点拆出的条号转移到该边的 `fact` 属性标记 `（article：第X条）`
      上——条级信息上边、不再产生条级实体节点。

    批跑（kg_lane_run）与增量（KGLane.add_episode）共用同一个
    Graphiti 实例，故单点覆盖两条写入路径。幂等：改写只做名字映射与
    条号标记，重复执行结果不变（merge.is_idempotent 语义在管线级成立）。
    """

    def __init__(self, inner: Any, merger: merge_mod.EntityMerger):
        self._inner = inner
        self._merger = merger

    # 父类字段（config/tracer 等）透传给内层——内层持有真实配置。
    __getattr__ = _delegate_to("_inner")

    async def generate_response(self, messages: list[Any], response_model: Any = None,
                                max_tokens: int | None = None, model_size: Any = None,
                                group_id: str | None = None, prompt_name: str | None = None,
                                **kwargs: Any) -> Any:
        forward: dict[str, Any] = {
            "response_model": response_model,
            "max_tokens": max_tokens,
            "group_id": group_id,
            "prompt_name": prompt_name,
            **kwargs,
        }
        if model_size is not None:
            forward["model_size"] = model_size
        response = await self._inner.generate_response(messages, **forward)
        return self._rewrite(prompt_name or "", response)

    async def _generate_response(self, *args: Any, **kwargs: Any) -> Any:
        return await self._inner._generate_response(*args, **kwargs)

    # 响应键与 prompt_name 前缀都来自钉死的 0.31.1 源码（prompts/names.py、
    # prompts/extract_nodes.py、prompts/extract_edges.py）；键不存在的响应
    # 原样透传（去重/摘要等其他 LLM 调用不受影响）。
    def _rewrite(self, prompt_name: str, response: Any) -> Any:
        if not isinstance(response, dict):
            return response
        nodes = prompt_name.startswith("extract_nodes") or \
            prompt_name.startswith("extract_nodes_and_edges")
        edges = prompt_name.startswith("extract_edges") or \
            prompt_name.startswith("extract_nodes_and_edges")
        if nodes and isinstance(response.get("extracted_entities"), list):
            response = {**response,
                        "extracted_entities": self._merger.rewrite_entity_payload(
                            response["extracted_entities"])}
        if edges and isinstance(response.get("edges"), list):
            response = {**response,
                        "edges": self._merger.rewrite_edge_payload(response["edges"])}
        return response


def episode_kwargs(cfg: cfg_mod.KGVerticalConfig, *, name: str, body: str,
                   reference_time: Any, source_description: str = "") -> dict[str, Any]:
    """把配置翻译成 graphiti `add_episode` 的实参。

    **不传 uuid**：试点实测传入即走 `get_by_uuid` 路径，FalkorDB 下连接被服务端
    关闭（两轮复现）。断点续跑由跑批器的 state 文件承担，不依赖图内 uuid。
    """
    try:  # graphiti 未装时（仅跑单测/静态装配）用等价枚举替身
        from graphiti_core.nodes import EpisodeType

        source = EpisodeType.text
    except ModuleNotFoundError:  # pragma: no cover - 无 graphiti 环境
        source = "text"

    return {
        "name": name,
        "episode_body": body,
        "source_description": source_description or f"kg-lane:{cfg.corpus.domain}",
        "reference_time": reference_time,
        "source": source,
        "group_id": cfg.group_id or cfg.name,
        "entity_types": _entity_type_models(cfg),
        "custom_extraction_instructions": build_extraction_instructions(cfg),
    }


def _entity_type_models(cfg: cfg_mod.KGVerticalConfig) -> dict[str, type]:
    """动态生成空实体模型（属性只放在 docstring）。

    为什么是空模型：试点实测 FalkorDB 对**非空 attributes** 写入边时会抛
    "Property values can only be of primitive types"。类型语义全部由 docstring
    承载（graphiti 会把 docstring 注入抽取 prompt），实证抽取质量不受影响。
    """
    from pydantic import BaseModel

    models: dict[str, type] = {}
    for spec in cfg.entity_types:
        models[spec.name] = type(spec.name, (BaseModel,), {"__doc__": spec.description})
    return models


def build_extraction_instructions(cfg: cfg_mod.KGVerticalConfig) -> str:
    """垂域抽取指令 = 本体说明 + 垂域自定义指令。"""
    lines = [
        f"你正在从{cfg.language}文本中抽取实体与关系。实体类型限定为：",
    ]
    for spec in cfg.entity_types:
        lines.append(f"- {spec.name}：{spec.description}")
    lines.append("")
    lines.append(cfg.extraction_instructions.strip())
    return "\n".join(lines)


def build_corpus_index(episodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{episode_id: provenance} —— 让查询结果能回指语料原始记录。

    图里的 episode 节点名就是我们传入的 `name`（episode_id），所以这张表
    是 uuid -> episode 名 -> 语料记录这条链的最后一跳。不给它，
    FactView.provenance 会静默为空——试点第 2 个缺陷就是这个。
    """
    out: dict[str, dict[str, Any]] = {}
    for ep in episodes:
        ep_id = ep.get("episode_id") or ep.get("name")
        if not ep_id:
            continue
        prov = dict(ep.get("provenance") or {})
        prov.setdefault("episode_id", ep_id)
        if ep.get("kind"):
            prov.setdefault("kind", ep["kind"])
        out[str(ep_id)] = prov
    return out


def build_lane(cfg: cfg_mod.KGVerticalConfig, settings: LaneSettings, *,
               graphiti: Any = None) -> "KGLane":
    """装配一条可跑的垂域车道。"""
    cfg.validate()
    ledger = cost_mod.CostLedger(settings.cost_ledger_path).load_existing()
    graphiti = graphiti or build_graphiti(settings, ledger=ledger, config=cfg)
    return KGLane(config=cfg, settings=settings, graphiti=graphiti, ledger=ledger)


@dataclass
class KGLane:
    """一条垂域车道：配置 + 图 + 账本。"""

    config: cfg_mod.KGVerticalConfig
    settings: LaneSettings
    graphiti: Any
    ledger: cost_mod.CostLedger
    corpus_index: dict[str, dict[str, Any]] = field(default_factory=dict)

    async def add_episode(self, *, name: str, body: str, reference_time: Any,
                          source_description: str = "") -> Any:
        self.ledger.begin_episode(name)
        kwargs = episode_kwargs(self.config, name=name, body=body,
                                reference_time=reference_time,
                                source_description=source_description)
        return await self.graphiti.add_episode(**kwargs)

    def query_surface(self):
        from .query import GraphitiQueryBackend, KGLaneQuery

        return KGLaneQuery(
            GraphitiQueryBackend(self.graphiti),
            group_id=self.config.group_id or self.config.name,
            corpus_index=self.corpus_index,
            fanout_cap=self.settings.fanout_cap,
            super_edge_threshold=self.config.walk_super_edge_threshold,
        )

    def summarize_costs(self) -> dict[str, Any]:
        return cost_mod.summarize(self.ledger)

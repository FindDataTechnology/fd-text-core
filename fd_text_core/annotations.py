"""工具行为属性（mcp-tool-annotations）。

工具面由 `server._register(domain)` 按垂域循环生成（当前 gov/papers/screenplay
× 5 个动词 = 15 个工具），外加 KG 车道查询工具族 3 件（`kg_tools.py`，见
KG_TOOLS）。这里按「垂域 × 动词」声明四项提示，注册时逐工具取
副本——同一份 dict 被多个工具共享时，任何一处后续改动都会泄漏到别的工具。

**只读**：检索、预设枚举、切块读取、外部联邦检索都只查询既有数据，不写库、
不落缓存、不改索引，故 `readOnlyHint=True`、`destructiveHint=False` 全为真
（本服务没有删除/改写接口）。

**幂等**：同一查询在同一语料版本上返回同一批行，故 `idempotentHint=True`。
唯一例外是 `papers_federated`：它真的出网调 arXiv / OpenAlex / Semantic
Scholar，外部结果集会随时间变化，重复同一入参可能拿到不同结果，故标非幂等。
gov/screenplay 的 federated 没有 connector，恒返回 NOT_SUPPORTED，仍算幂等。

**开放世界**：判据是「结果是否取决于一个不可枚举且会变化的外部实体集合」。
本服务的本地检索面是封闭、版本化的语料（PostgreSQL 的 chunks + pgvector 向量），
同一语料版本上同一查询恒得同一批行——连数据库不等于开放世界，所以
`openWorldHint=False`。这里还有一个容易误判的点：`*_search` 会调外部嵌入网关
把查询串变成向量，但嵌入模型是固定且版本化的，同一文本恒得同一向量，检索结果
仍来自那批固定 point，故仍是 False。**不要**因为「出网了」就把它改成 True。

真正符合开放世界定义的是 `papers_federated`：arXiv/OpenAlex/S2 的实体集合
不可枚举且持续变化，故 `openWorldHint=True`。该判断不写死域名，而是按
connector 模块是否存在推导——某个垂域将来接了外部 connector，它的 federated
会自动变成开放世界，selfcheck 会立刻因 manifest 未同步而失败，逼着声明与实现
一起改。
"""
from __future__ import annotations

import importlib.util
from typing import Any

from .verticals.registry import list_verticals

#: `_register` 生成的动词集（与 server.py 的五个 @mcp.tool 一一对应）。
VERBS = ("search", "presets", "semantic_search", "federated", "read")

#: KG 车道查询工具族（kg-lane-delivery-v1 D4）：引擎级新增族，不进 `<domain>_*`
#: 命名空间；工具面固定三件，与 kg_lane 查询面一一对应。只读、幂等、封闭世界
#: （图是车道批处理产出的版本化工件，同一图版本上同一查询恒得同一批事实）。
KG_TOOLS = ("kg_search_facts", "kg_walk", "kg_facts_as_of")

#: 只读 + 幂等 + 封闭语料。
READ_ONLY: dict[str, Any] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

#: 只读但出网：外部结果集不可枚举且会变化，故非幂等、开放世界。
EXTERNAL_READ_ONLY: dict[str, Any] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}


def _has_external_connector(domain: str) -> bool:
    """垂域是否真有外部 connector（决定 federated 是否出网）。

    与 server 里 federated 工具的动态 import 同一判据：模块存在即可能出网。
    用 find_spec 而不是 import——只问「有没有」，不触发模块副作用。
    """
    try:
        return importlib.util.find_spec(
            f"{__package__}.verticals.{domain}.connectors"
        ) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _build() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for domain in list_verticals():
        for verb in VERBS:
            external = verb == "federated" and _has_external_connector(domain)
            out[f"{domain}_{verb}"] = dict(EXTERNAL_READ_ONLY if external else READ_ONLY)
    # KG 工具族：只读 + 幂等 + 封闭世界（版本化图工件，不出网、不查开放集合）。
    for name in KG_TOOLS:
        out[name] = dict(READ_ONLY)
    return out


#: 工具名 → 四项提示。按注册表推导而非抄死 15 个名字：新增垂域时工具面自动
#: 扩张，漏同步 manifest 由 selfcheck 报出（工具数与属性双向比对）。
TOOL_ANNOTATIONS: dict[str, dict[str, Any]] = _build()


def annotations_for(name: str) -> dict[str, Any]:
    """取属性副本；未登记即报错（沉默的默认值会让漏登记的工具蒙混过关）。"""
    try:
        return dict(TOOL_ANNOTATIONS[name])
    except KeyError:
        raise KeyError(
            f"工具 {name!r} 未在 annotations.TOOL_ANNOTATIONS 登记行为属性"
        ) from None

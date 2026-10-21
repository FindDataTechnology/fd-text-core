"""工具行为属性契约（mcp-tool-annotations）。

工具面由 `server._register(domain)` 循环生成，故本文件不写死 15 个名字，而是
按「已加载垂域 × 动词集」推导——新增垂域却漏了属性，计数断言会立刻变红。

三条断言，缺一条客户端就会误判自动批准策略：

1. **齐备**：每个工具在 `tools/list` 里都带四项提示，且都不是 None。
2. **与实际副作用一致**：本服务全部只读（检索/预设/读取/外部联邦都只查不写），
   故 `readOnlyHint=True`、`destructiveHint=False` 必须全覆盖。
3. **开放世界按真实数据源判定**：本地检索面是封闭、版本化的语料，同一语料版本
   上同一查询恒得同一批行，故 `openWorldHint=False`——连数据库、调固定嵌入模型
   都不改变这条判断（嵌入模型固定，同一文本恒得同一向量）。唯一例外是
   `papers_federated`：它真的出网查 arXiv/OpenAlex/Semantic Scholar，外部实体
   集合不可枚举且持续变化，故 `openWorldHint=True` 且非幂等。本文件按「垂域目录
   里有没有 connectors.py」独立推导该例外，而不是复用实现里的判断函数——否则
   实现改错时测试会跟着一起错。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastmcp import Client

from fd_text_core.annotations import TOOL_ANNOTATIONS, VERBS, annotations_for
from fd_text_core.server import mcp
from fd_text_core.verticals.registry import list_verticals

REPO = Path(__file__).resolve().parents[1]
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


@pytest.fixture(scope="module")
def runtime() -> dict[str, dict]:
    async def go():
        async with Client(mcp) as client:
            out: dict[str, dict] = {}
            for t in await client.list_tools():
                a = t.annotations
                assert a is not None, f"{t.name} 未声明行为属性"
                out[t.name] = {k: getattr(a, k) for k in HINTS}
            return out

    return asyncio.run(go())


def _has_connector(domain: str) -> bool:
    """独立判据：垂域目录里是否有 connectors.py（不复用实现里的 find_spec 逻辑）。"""
    return (REPO / "fd_text_core" / "verticals" / domain / "connectors.py").exists()


def test_all_four_hints_declared_on_every_tool(runtime):
    assert runtime, "运行时没有工具——注册循环未生效"
    for name, a in runtime.items():
        for k in HINTS:
            assert a[k] is not None, f"{name}.{k} 未声明"


def test_tool_count_matches_domain_loop(runtime):
    """计数 = 已加载垂域 × 每域动词数 + KG 工具族；新增垂域漏属性会在这里被抓住。"""
    from fd_text_core.annotations import KG_TOOLS

    domains = list_verticals()
    expected = {f"{d}_{v}" for d in domains for v in VERBS} | set(KG_TOOLS)
    assert len(runtime) == len(expected), (
        f"运行时 {len(runtime)} 个工具 ≠ {len(domains)} 垂域 × {len(VERBS)} 动词 + KG 族 {len(KG_TOOLS)}"
    )
    assert set(runtime) == expected


def test_every_tool_is_read_only(runtime):
    for name, a in runtime.items():
        assert a["readOnlyHint"] is True, f"{name} 只读却未标只读"
        assert a["destructiveHint"] is False, f"{name} 无删除/改写接口，不应标破坏性"


def test_open_world_matches_real_data_source(runtime):
    """只有真出网查外部开放集合的 federated 才是开放世界。"""
    for name, a in runtime.items():
        domain, _, verb = name.rpartition("_")
        external = verb == "federated" and _has_connector(domain)
        assert a["openWorldHint"] is external, (
            f"{name}: openWorldHint={a['openWorldHint']}，期望 {external}"
            f"（判据：垂域 {'有' if _has_connector(domain) else '无'} connectors.py）"
        )
        assert a["idempotentHint"] is not external, (
            f"{name}: 外部结果集随时间变化，重复同一入参不得标幂等"
        )


def test_local_retrieval_is_closed_world(runtime):
    """封闭语料 ≠ 开放世界：连 PG/pgvector、调固定嵌入模型都不改变这条判断。"""
    local = {n: a for n, a in runtime.items() if not n.endswith("_federated")}
    assert local, "本地检索面为空"
    for name, a in local.items():
        assert a["openWorldHint"] is False, (
            f"{name}: 数据源是版本化封闭语料，标 openWorld 会让调用方误以为结果不可复现"
        )
        assert a["idempotentHint"] is True, f"{name}: 只读查询应可安全重放"


def test_registry_covers_every_runtime_tool(runtime):
    assert set(runtime) == set(TOOL_ANNOTATIONS), (
        "运行时与登记表不一致：新增工具必须在 annotations.TOOL_ANNOTATIONS 登记"
    )


def test_manifest_annotations_match_runtime(runtime):
    """manifest 是上牌面：属性缺失或与运行时不符都算未上牌。"""
    manifest = json.loads((REPO / "tool-surface.json").read_text(encoding="utf-8"))
    declared = manifest.get("tool_annotations")
    assert isinstance(declared, dict), "manifest 缺 tool_annotations"
    assert set(declared) == set(runtime), "manifest 工具集与运行时不一致"
    for name, a in runtime.items():
        for k in HINTS:
            assert declared[name].get(k) == a[k], f"{name}.{k}: manifest 与运行时不符"


def test_unknown_tool_raises_instead_of_defaulting():
    """未登记即报错——沉默的默认值会让漏登记的工具蒙混过关。"""
    with pytest.raises(KeyError):
        annotations_for("no_such_tool")


def test_annotations_are_copies():
    a = annotations_for("gov_search")
    a["readOnlyHint"] = False
    assert annotations_for("gov_read")["readOnlyHint"] is True

"""错误码契约（mcp-error-semantics 组 4）。

本服务早在契约之前就有错误码（`_err(code, message)`），但它们返回的是
`{"ok": False, ...}` —— 在协议上那是一次**成功**调用（`isError=false`），
只有读载荷的人才知道它失败了。平台侧按 isError 判定失败，因此"配置缺失"
与"上游不可达"在平台看来都是成功。

现在 `_err` 返回 `ToolResult(is_error=True)`：`isError` 与结构化错误体同时
到达。`ok` 字段保留，既有消费方按它分支的逻辑不变。
"""
from __future__ import annotations

import asyncio

import pytest

from fd_text_core.server import mcp

#: 本服务的码 → mcp-error-semantics 四类的对应关系。
#: 大写拼写保留（早于契约、已上牌），语义必须落在四类之内。
CODE_CLASS = {
    "UPSTREAM_UNAVAILABLE": "upstream_unavailable",
    "NOT_FOUND": "not_found",
    "NOT_SUPPORTED": "not_wired",
    "FACET_INVALID": "invalid_input",
    "PRESET_INVALID": "invalid_input",
}


def _call(name: str, args: dict):
    async def go():
        async with Client_(mcp) as c:
            return await c.call_tool(name, args, raise_on_error=False)

    from fastmcp import Client as Client_

    return asyncio.run(go())


def test_every_emitted_code_maps_into_the_four_classes():
    """源码里出现的每个码都必须能归入四类之一。

    归不进去的码说明它表达了一件四类之外的事——那要么该并进现有类，
    要么该在契约里新增一类，而不是悄悄多一个没人处理的分支。
    """
    import pathlib
    import re

    pkg = pathlib.Path(__file__).resolve().parents[1] / "fd_text_core"
    emitted: set[str] = set()
    for py in pkg.rglob("*.py"):
        emitted |= set(re.findall(r'_err\(\s*"([A-Z_]+)"', py.read_text(encoding="utf-8")))
    unmapped = emitted - set(CODE_CLASS)
    assert unmapped == set(), (
        f"这些码没有归入四类：{sorted(unmapped)}——在 CODE_CLASS 里登记其语义"
    )


def test_not_supported_is_an_error_result_not_a_success():
    """未接线的能力必须是失败结果（isError），不能是 ok:false 的成功载荷。"""
    r = _call("gov_federated", {"query": "x"})
    assert r.is_error is True, "not_wired 必须是失败结果"
    assert r.structured_content["error"]["code"] == "NOT_SUPPORTED"
    # 既有消费方按 ok 分支，这个字段不能丢
    assert r.structured_content["ok"] is False


def test_error_body_still_carries_code_and_message():
    r = _call("gov_federated", {"query": "x"})
    err = r.structured_content["error"]
    assert err["code"] and err["message"]
    assert "connector" in err["message"], "消息要指出缺什么"


def test_successful_call_is_not_an_error_result(monkeypatch):
    """成功路径不能被错误包装误伤。"""
    # gov_presets 只读本地注册表，不需要 DB
    r = _call("gov_presets", {})
    assert r.is_error is False
    assert r.data.get("ok") is True

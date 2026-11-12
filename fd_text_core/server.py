"""单 MCP 服务：垂域注册 -> 自动挂载 <domain>_* 工具。

加垂域不部署新服务（spec text-query-surface）：工具由注册表的 Vertical 生成，
垂域只提供 facet 词表/分词配置/预设包。
"""
from __future__ import annotations

import os

from fastmcp import FastMCP

from .annotations import annotations_for
from .db import UpstreamUnavailable
from .querylib import FacetError, hybrid_search, read_chunks
from . import wellknown
from .verticals.registry import list_verticals

mcp = FastMCP("fd-text-core", instructions="通用文本引擎：按垂域命名空间提供检索与读取工具。")

wellknown.register(mcp)  # /.well-known/line-manifest.json（公开、免鉴权、零网络）


def _embedder_or_none():
    """没有嵌入凭据就返回 None——由查询面如实降级，而不是崩在工具里。"""
    from .embedding import embedder_from_env

    return embedder_from_env()


def _ok(payload: dict) -> dict:
    return {"ok": True, **payload}


def _err(code: str, message: str, **extra):
    """失败结果：`isError=true` 且带结构化错误体。

    **为什么用 ToolResult 而不是返回 `{"ok": False, ...}`**：后者在协议上是一次
    **成功**调用（isError=false），只有读载荷的人才知道它失败了。平台侧按
    isError 判定失败（凭据失效、上游不可用都依赖这个信号），模型侧也更容易
    把"成功但 ok=false"当成拿到了数据。

    **为什么保留 `ok` 字段**：既有消费方（pack 技能与测试）按 `ok` 分支，
    去掉它会让兼容性无声破裂。两个信号同时给：协议层的 isError 给平台，
    `ok`/`error` 给调用方代码。

    码用大写常量（UPSTREAM_UNAVAILABLE 等）：本服务早于统一的错误码契约，
    保留原拼写以免破坏已上牌的消费方；语义与 mcp-error-semantics 的四类
    一一对应（UPSTREAM_UNAVAILABLE / NOT_FOUND / FACET_INVALID|PRESET_INVALID
    → invalid_input / NOT_SUPPORTED → not_wired）。
    """
    from fastmcp.tools.base import ToolResult

    return ToolResult(
        structured_content={"ok": False,
                            "error": {"code": code, "message": message, **extra}},
        is_error=True,
    )


def _register(domain: str) -> None:
    """为一个垂域注册 <domain>_search / _presets / _semantic_search / _federated / _read。

    行为属性逐工具取副本（annotations.annotations_for）：同一份 dict 若被多个
    工具共享，后续任何一处改动都会泄漏到别的工具上。
    """

    @mcp.tool(
        name=f"{domain}_search",
        description=f"{domain} 垂域：结构化 + 语义混合检索（可带 preset 用预设）",
        annotations=annotations_for(f"{domain}_search"),
    )
    def search(
        query: str,
        limit: int = 20,
        facets: dict | None = None,
        channels: str = "",
        preset: str = "",
        name: str = "",
    ) -> dict:
        # channels 默认空串（而非 "hybrid"）：空=未表态，用预设预置或引擎默认；
        # 若默认写 "hybrid" 会把预设的 channels 静默覆盖掉。
        try:
            if preset:
                from .presets import PresetError, run_preset

                kw: dict = {"name": name}
                if facets:
                    kw["facets"] = facets
                if channels:
                    kw["channels"] = channels
                try:
                    res = run_preset(
                        domain, preset, query, embedder=_embedder_or_none(),
                        limit=limit or None, **kw,
                    )
                except PresetError as exc:
                    return _err("PRESET_INVALID", str(exc))
            else:
                res = hybrid_search(
                    domain, query, embedder=_embedder_or_none(), limit=limit,
                    facets=facets, channels=channels or "hybrid",
                )
        except FacetError as exc:
            return _err("FACET_INVALID", str(exc))
        except UpstreamUnavailable as exc:
            return _err("UPSTREAM_UNAVAILABLE", f"PG 不可达：{exc}")
        return _ok({
            "results": res["results"], "count": res["count"],
            "degraded": res["degraded"], "facets_used": res["facets_used"],
            **({"preset": res["preset"]} if res.get("preset") else {}),
        })

    @mcp.tool(
        name=f"{domain}_presets",
        description=f"{domain} 垂域：列出可用检索预设",
        annotations=annotations_for(f"{domain}_presets"),
    )
    def presets_tool() -> dict:
        from .presets import PresetError, list_presets

        try:
            return _ok({"presets": list_presets(domain), "count": len(list_presets(domain))})
        except PresetError as exc:
            return _err("PRESET_INVALID", str(exc))

    @mcp.tool(
        name=f"{domain}_semantic_search",
        description=f"{domain} 垂域：纯向量语义检索",
        annotations=annotations_for(f"{domain}_semantic_search"),
    )
    def semantic_search(query: str, limit: int = 20, facets: dict | None = None) -> dict:
        emb = _embedder_or_none()
        if emb is None:
            return _err(
                "UPSTREAM_UNAVAILABLE",
                "嵌入通道未配置（缺 FDTEXT_EMBEDDING_API_BASE/KEY）；"
                f"关键词检索仍可用：{domain}_search with channels='keyword'",
            )
        try:
            res = hybrid_search(domain, query, embedder=emb, limit=limit, facets=facets,
                                channels="vector")
        except FacetError as exc:
            return _err("FACET_INVALID", str(exc))
        except UpstreamUnavailable as exc:
            return _err("UPSTREAM_UNAVAILABLE", f"PG 不可达：{exc}")
        return _ok({"results": res["results"], "count": res["count"], "degraded": res["degraded"]})

    @mcp.tool(
        name=f"{domain}_federated",
        description=f"{domain} 垂域：外部开放检索编排（arXiv/OpenAlex/Semantic Scholar，非本地语料）",
        annotations=annotations_for(f"{domain}_federated"),
    )
    def federated(query: str, limit: int = 10) -> dict:
        """外部联邦检索。仅元数据与摘要，绝不返回全文成品（版权红线）。"""
        try:
            from importlib import import_module

            federated_search = import_module(
                f"fd_text_core.verticals.{domain}.connectors"
            ).federated_search
        except (ImportError, AttributeError):
            return _err("NOT_SUPPORTED", f"垂域 {domain} 未提供外部 connector")
        import os

        res = federated_search(
            query, limit,
            openalex_mailto=os.environ.get("OPENALEX_MAILTO", ""),
            openalex_key=os.environ.get("OPENALEX_API_KEY", ""),
            s2_key=os.environ.get("S2_API_KEY", ""),
        )
        return _ok({
            "hits": res.hits,
            "count": len(res.hits),
            "degraded": res.degraded,
            "note": "全文请循 hits[].url 至原始出版方获取；本服务不分发全文",
        })

    @mcp.tool(
        name=f"{domain}_read",
        description=f"{domain} 垂域：按 doc_id 偏移读取切块",
        annotations=annotations_for(f"{domain}_read"),
    )
    def read(doc_id: int, offset: int = 0, limit: int = 4000) -> dict:
        try:
            got = read_chunks(doc_id, offset=offset, limit=limit)
        except UpstreamUnavailable as exc:
            return _err("UPSTREAM_UNAVAILABLE", f"PG 不可达：{exc}")
        if got is None:
            return _err("NOT_FOUND", f"文档不存在或非当前版本: {doc_id}")
        return _ok({
            "doc_id": doc_id,
            "title": got["document"]["title"],
            "provenance": got["document"]["provenance"],
            "chunks": got["chunks"],
            "text": got["text"],
            "next_offset": got["next_offset"],
        })


for _d in list_verticals():
    _register(_d)

# KG 车道查询工具族（kg-lane-delivery-v1 D4）：引擎级新增族，实现全部转发
# kg_lane/query.py 既有查询面，不触碰上面 <domain>_* 既有工具的契约与行为。
from .kg_tools import register_kg_tools

register_kg_tools(mcp, _err=_err)


def main() -> None:  # pragma: no cover
    serve_http(int(os.environ.get("PORT", "8000")))


def serve_http(port: int) -> None:  # pragma: no cover - 传输层
    """HTTP 面：配了 ``MCP_BEARER_TOKEN`` 才套共享 bearer 门，否则原样跑。

    未配置分支保持 ``mcp.run(transport="http")``——与接入前逐字节一致。这是
    「开关关闭时行为不变」唯一可靠的实现方式：不是挂一个内部空转的中间件。
    """
    from .auth import BearerAuthMiddleware, configured_token

    if not configured_token():
        mcp.run(transport="http", host="0.0.0.0", port=port)
        return

    import uvicorn
    from starlette.middleware import Middleware

    uvicorn.run(
        mcp.http_app(middleware=[Middleware(BearerAuthMiddleware)]),
        host="0.0.0.0",
        port=port,
    )


if __name__ == "__main__":  # pragma: no cover
    main()

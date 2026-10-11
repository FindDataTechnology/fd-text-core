"""单 MCP 服务：垂域注册 -> 自动挂载 <domain>_* 工具。

加垂域不部署新服务（spec text-query-surface）：工具由注册表的 Vertical 生成，
垂域只提供 facet 词表/分词配置/预设包。
"""
from __future__ import annotations

import os
from typing import Any

from fastmcp import FastMCP

from .db import UpstreamUnavailable
from .querylib import FacetError, hybrid_search, read_chunks
from .verticals.registry import list_verticals

mcp = FastMCP("fd-text-core", instructions="通用文本引擎：按垂域命名空间提供检索与读取工具。")


def _embedder_or_none():
    """没有嵌入凭据就返回 None——由查询面如实降级，而不是崩在工具里。"""
    from .embedding import Embedder

    base = os.environ.get("FDTEXT_EMBEDDING_API_BASE", "")
    key = os.environ.get("FDTEXT_EMBEDDING_API_KEY", "")
    model = os.environ.get("FDTEXT_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")
    dim = int(os.environ.get("FDTEXT_EMBEDDING_DIM", "1024"))
    if not (base and key):
        return None
    return Embedder(base, key, model, dimensions=dim)


def _ok(payload: dict) -> dict:
    return {"ok": True, **payload}


def _err(code: str, message: str, **extra) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, **extra}}


def _register(domain: str) -> None:
    """为一个垂域注册 <domain>_search / <domain>_semantic_search / <domain>_read。"""

    @mcp.tool(name=f"{domain}_search", description=f"{domain} 垂域：结构化 + 语义混合检索（可带 preset 用预设）")
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

    @mcp.tool(name=f"{domain}_presets", description=f"{domain} 垂域：列出可用检索预设")
    def presets_tool() -> dict:
        from .presets import PresetError, list_presets

        try:
            return _ok({"presets": list_presets(domain), "count": len(list_presets(domain))})
        except PresetError as exc:
            return _err("PRESET_INVALID", str(exc))

    @mcp.tool(name=f"{domain}_semantic_search", description=f"{domain} 垂域：纯向量语义检索")
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

    @mcp.tool(name=f"{domain}_read", description=f"{domain} 垂域：按 doc_id 偏移读取切块")
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


def main() -> None:  # pragma: no cover
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":  # pragma: no cover
    main()

"""垂域检索预设包：把常用检索意图固化为参数化查询。

设计要点（text-query-presets design §2）：预设是**声明式参数的命名封装**，
不是存储的查询语句。执行走既有 hybrid_search 同一条路径，facet 照过
_validate_facets——预设不得成为绕过词表校验或 RRF 融合的后门。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 注意：不在模块顶层 import registry——registry 底部会 load_verticals() 导入
# 垂域模块，而垂域模块要 from presets import Preset，形成循环导入
# （实测：ImportError partially initialized module）。registry 访问一律下沉到
# 函数体内，模块顶层只留数据结构定义。


def _vertical(domain: str):
    from .verticals.registry import get as get_vertical

    try:
        return get_vertical(domain)
    except KeyError:
        raise PresetError(f"未注册垂域: {domain}") from None


class PresetError(ValueError):
    """预设名不存在或参数非法。"""


@dataclass(frozen=True)
class Preset:
    """一个检索预设。params 只允许声明式参数（facets/channels/limit）。"""

    name: str
    description: str
    params: dict = field(default_factory=dict)
    query_template: str | None = None  # 如 "{name}"；None 则用调用方 query

    def render_query(self, query: str, kwargs: dict) -> str:
        """模板占位替换（仅 {key} 字符串替换，无表达式求值）。

        未知占位符原样保留——宁可查询里留个 {foo} 字面量被分词过滤，
        也不做静默清空（静默会让调用方以为模板生效了）。
        """
        if not self.query_template:
            return query
        out = self.query_template
        for k, v in kwargs.items():
            out = out.replace("{" + k + "}", str(v))
        return out


def list_presets(domain: str) -> list[dict]:
    """枚举垂域预设。无预设垂域返回空列表（不是错误）。"""
    v = _vertical(domain)
    return [
        {"name": p.name, "description": p.description,
         "params": dict(p.params), "query_template": p.query_template}
        for p in v.presets.values()
    ]


def _merge_params(preset: Preset, kwargs: dict) -> dict:
    """调用方参数覆盖预置；facet 为逐键合并（design §4）。"""
    merged: dict[str, Any] = {}
    for k, v in preset.params.items():
        merged[k] = dict(v) if isinstance(v, dict) else v
    for k, v in kwargs.items():
        if k == "facets" and isinstance(v, dict) and isinstance(merged.get("facets"), dict):
            m = dict(merged["facets"])
            m.update(v)
            merged["facets"] = m
        elif k in ("name", "query"):  # 模板变量，不作为检索参数
            continue
        else:
            merged[k] = v
    return merged


def run_preset(
    domain: str,
    preset_name: str,
    query: str,
    *,
    embedder=None,
    limit: int | None = None,
    **kwargs,
) -> dict:
    """执行预设：合并参数后走既有 hybrid_search（不新增检索路径）。"""
    from .querylib import hybrid_search

    v = _vertical(domain)
    preset = v.presets.get(preset_name)
    if preset is None:
        avail = sorted(v.presets)
        raise PresetError(
            f"垂域 {domain} 无预设「{preset_name}」，可用={avail}"
            if avail
            else f"垂域 {domain} 未定义任何预设"
        )

    merged = _merge_params(preset, kwargs)
    q = preset.render_query(query, kwargs)
    res = hybrid_search(
        domain,
        q,
        embedder=embedder,
        limit=int(limit or merged.get("limit") or 20),
        facets=merged.get("facets"),
        channels=merged.get("channels", "hybrid"),
    )
    res["preset"] = preset_name
    return res


def presets_manifest(domain: str) -> dict:
    """产出谦面市场同构的最小 manifest（本期只落文件，不发布）。"""
    v = _vertical(domain)
    return {
        "schema": "facet-preset-suite/v1",
        "id": f"text-{domain}-presets",
        "brand_name": v.display or domain,
        "description": f"{v.display or domain}垂域检索预设包（{len(v.presets)} 个预设）",
        "mcp_servers": ["fd-text-core"],
        "presets": [
            {"name": p.name, "description": p.description,
             "params": dict(p.params), "query_template": p.query_template}
            for p in v.presets.values()
        ],
    }

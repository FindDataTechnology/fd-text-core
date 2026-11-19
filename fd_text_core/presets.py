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


#: 落盘 manifest 里由**市场侧手写**的信封键：生成器既不产出、也不该覆盖它们。
#: 2026-11-15 审计实测 `deploy/presets/screenplay-presets.json` 比生成器多出
#: `visibility` / `commercial_ready` / `commercial_blockers` / `tags` 四个键，
#: description 也更长（带数据来源与商业阻塞原因）。
MARKET_ENVELOPE = ("visibility", "commercial_ready", "commercial_blockers", "tags")


def merge_manifest(domain: str, existing: dict | None = None) -> dict:
    """把**代码里的**预置刷进已落盘的 manifest，同时保留市场侧手写的信封。

    ## 为什么需要这一步

    审计 `2026-10-11-text-query-presets` 时发现：`presets_manifest()` 在整个仓里
    **只有一个调用方——测试**，而 `deploy/presets/screenplay-presets.json` 是
    手工维护的。两者之间没有任何联系 ⇒ 谁在代码里加/改一个预置，落盘那份就静默
    落后，而它正是给市场看的形态。`presets` 载荷必须由代码拥有，信封键必须由人
    拥有，这个函数就是那条分界线。

    代码侧拥有的键：`schema` / `id` / `brand_name` / `description` / `mcp_servers`
    / `presets`；`existing` 里其余键（`MARKET_ENVELOPE` 及以后可能新增的市场字段）
    原样带过，**不用一份白名单去删它们**——那样每加一个市场字段都要改这里。
    """
    out = dict(existing or {})
    out.update(presets_manifest(domain))
    return out


def _main(argv: list[str] | None = None) -> int:
    """`python -m fd_text_core.presets <domain> <path> [--write]`

    不带 `--write` 时只报差异（人读），带则落盘。**默认不写**：这个文件是给市场看
    的形态，改它应当是一次显式动作。
    """
    import argparse
    import json
    from pathlib import Path

    ap = argparse.ArgumentParser(description="刷新落盘的预设 manifest（保留市场信封）")
    ap.add_argument("domain")
    ap.add_argument("path")
    ap.add_argument("--write", action="store_true", help="真的落盘（缺省只报差异）")
    a = ap.parse_args(argv)

    from .verticals.registry import load_verticals

    load_verticals()
    p = Path(a.path)
    existing = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    merged = merge_manifest(a.domain, existing)

    if existing == merged:
        print(f"已是最新：{p}（{len(merged['presets'])} 个预置）")
        return 0
    if existing is None:
        print(f"新建：{p}")
    else:
        old = [x["name"] for x in existing.get("presets", [])]
        new = [x["name"] for x in merged["presets"]]
        print(f"预置差异：落盘 {old} → 代码 {new}")
        dropped = sorted(set(merged) ^ set(existing))
        if dropped:
            print(f"信封键差异：{dropped}")
    if a.write:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已写入：{p}")
        return 0
    print("（未写入，加 --write 才落盘）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())


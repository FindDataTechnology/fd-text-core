"""垂域注册表：四件配置的注册点。

接入一个垂域 = 在本模块注册 ① 域解析器 ② 切块策略 ③ facet 词表 ④ 预设包，
外加分区一行迁移。引擎核心模块不得被改动——CI 有路径越界检查守着。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..chunking import SlidingWindow


@dataclass(frozen=True)
class Vertical:
    name: str
    strategy: Callable  # 切块策略 callable(str) -> [(start, end, section_path)]
    facets: dict[str, frozenset]  # facet 名 -> 允许取值集合
    parser: Callable | None = None  # 域解析器：干净文本 -> (结构树, 元数据)
    tsv_config: str = "simple"  # 中文垂域接入时切 zhparser
    language: str = "multi"
    display: str = ""


_REGISTRY: dict[str, Vertical] = {}


def register(v: Vertical) -> None:
    if v.name in _REGISTRY:
        raise ValueError(f"垂域已注册: {v.name}")
    _REGISTRY[v.name] = v


def get(name: str) -> Vertical:
    if name not in _REGISTRY:
        raise KeyError(f"未注册垂域: {name}；已注册={sorted(_REGISTRY)}")
    return _REGISTRY[name]


def get_strategy(name: str) -> Callable:
    return get(name).strategy


def list_verticals() -> list[str]:
    return sorted(_REGISTRY)


def clear() -> None:  # 测试用
    _REGISTRY.clear()


def _default_paper_facets() -> dict:
    return {
        "cat": frozenset({"q-fin", "cs.CL", "econ.GN", "stat.ML"}),
        "origin": frozenset({"crawl", "upload"}),
    }


def _default_screenplay_facets() -> dict:
    return {
        "tradition": frozenset({"western-classic", "chinese-classic", "modern-public-domain"}),
        "language": frozenset({"en", "zh"}),
    }


def bootstrap_defaults() -> None:
    """注册内置垂域默认项。垂域可用 register 覆盖同名项以替换策略/词表。"""
    for v in (
        Vertical(
            name="papers",
            strategy=SlidingWindow(target=1200, max_size=1800),
            facets=_default_paper_facets(),
            tsv_config="simple",
            language="multi",
            display="论文",
        ),
        Vertical(
            name="screenplay",
            strategy=SlidingWindow(target=800, max_size=1400),
            facets=_default_screenplay_facets(),
            tsv_config="simple",  # 接入时切 zhparser（实例装 raditional 扩展，schema 不变）
            language="zh",
            display="剧本",
        ),
    ):
        if v.name not in _REGISTRY:
            register(v)


bootstrap_defaults()

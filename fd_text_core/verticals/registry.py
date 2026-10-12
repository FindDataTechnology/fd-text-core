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
    tsv_config: str = "simple"  # 直通 PG 分词配置；cjk=True 时写入路径换引擎分词
    cjk: bool = False  # 启用引擎原生 CJK 分词（unigram+bigram，见 tokenize 模块）
    t2s: bool = True  # cjk 垂域下是否把繁体折简（默认开——繁体底本+简体查询是常态；
    #                  训诂/版本比对类垂域可关，关闭时写入与查询同时不归一）
    presets: dict = field(default_factory=dict)  # 检索预设包（见 presets 模块）
    language: str = "multi"
    display: str = ""


_REGISTRY: dict[str, Vertical] = {}


def register(v: Vertical) -> None:
    if v.name in _REGISTRY:
        raise ValueError(f"垂域已注册: {v.name}")
    _REGISTRY[v.name] = v


def replace(v: Vertical) -> None:
    """垂域模块以自身权威配置覆盖 bootstrap 默认项。

    bootstrap 只为「无垂域目录也能跑」提供兜底；一旦垂域模块存在，
    其配置应当胜出（register 拒绝重复注册是为了防 silent 覆盖）。
    """
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
            # 中文由引擎原生 CJK 分词承担（unigram+bigram+繁简归一，零扩展依赖）。
            # zhparser 在引擎 PG 实例不可得（无扩展 + 宿主无管理通道），保留为可选
            # 升级路径：将来装上后改本配置即可，引擎 schema 与迁移链零变更。
            tsv_config="simple",
            language="zh",
            display="剧本",
        ),
    ):
        if v.name not in _REGISTRY:
            register(v)


def load_verticals() -> list[str]:
    """导入全部内置垂域模块，使其权威配置经 replace() 生效。

    为什么必须有这一步：bootstrap_defaults() 注册的是「无垂域目录也能跑」的
    兜底配置（cjk=False 等），而垂域目录里的 replace() 才是权威配置——它只在
    模块被 import 时执行。此前没有任何地方 import 垂域模块，于是引擎**静默
    使用兜底配置**（实测：screenplay 的 cjk=True 从未生效，中文关键词检索
    0 命中）。隐式加载是这类失配的温床，故显式化并导出。

    返回成功加载的垂域名；单域导入失败不影响其他域（如实记录不吞掉）。
    """
    import importlib
    import sys

    loaded: list[str] = []
    for name in list_verticals():
        if name in _LOADED:
            loaded.append(name)
            continue
        try:
            importlib.import_module(f"{__package__}.{name}")
            _LOADED.add(name)
            loaded.append(name)
        except ModuleNotFoundError:
            # 兜底域（bootstrap 注册但无目录）：合法，保持兜底配置
            pass
        except Exception as exc:  # 垂域模块自身出错：如实报出，不静默
            print(f"[registry] 垂域 {name} 加载失败: {type(exc).__name__}: {exc}", file=sys.stderr)
    return loaded


_LOADED: set[str] = set()


bootstrap_defaults()
load_verticals()

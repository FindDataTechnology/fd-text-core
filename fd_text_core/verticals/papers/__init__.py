"""论文垂域：四件配置（解析器 + 切块策略 + facet 词表 + 预设包）。

接入只走注册，不改引擎核心。CI 有垂域越界检查守着。
"""
from __future__ import annotations

from ...chunking import SlidingWindow
from ...presets import Preset
from ..registry import Vertical, bootstrap_defaults, replace

#: 类目词表：**按实测语料分布定**（2026-11-13，fd-prod 60 篇，43 个类目）。
#: 之前只列了 8 个，于是 cs.CV（9 篇）、math.NA（13 篇）这类一查就被词表拒掉——
#: 用户看到的是"垂域无 facet 取值"，不是"没有这类论文"。语料扩容时须同步刷新
#: （刷新命令：select distinct jsonb_array_elements_text(metadata->'categories') …）。
FACETS = {
    "cat": frozenset({
        "cs.AI", "cs.AR", "cs.CC", "cs.CE", "cs.CL", "cs.CR", "cs.CV", "cs.DB",
        "cs.DC", "cs.DL", "cs.DM", "cs.DS", "cs.GT", "cs.IR", "cs.IT", "cs.LG",
        "cs.LO", "cs.MA", "cs.MS", "cs.NA", "cs.NI", "cs.RO", "cs.SD", "cs.SE",
        "cs.SI", "cs.SY", "econ.EM", "econ.GN", "eess.AS", "eess.IV", "eess.SY",
        "math.CO", "math.DS", "math.IT", "math.NA", "math.OC", "math.OA", "math.PR",
        "math.ST", "nlin.CD", "physics.geo-ph", "q-bio.NC", "q-fin", "q-fin.CP",
        "q-fin.EC", "q-fin.ST", "quant-ph", "stat.ME", "stat.ML", "stat.TH",
    }),
    "origin": frozenset({"crawl", "upload"}),
    "has_fulltext": frozenset({"true", "false"}),
}


class PaperParser:
    """域解析器：干净文本 -> 结构树 + 元数据（实现见 parser 模块）。"""

    def __call__(self, text: str):
        from .parser import parse_latex

        return parse_latex(text)


# 四件配置的第四件：检索预设包（声明式参数，执行走统一 hybrid_search，
# facet 照过词表校验）。论文场景的预设围绕「找方向 / 追方法 / 看最新」三问。
PRESETS = {
    "survey": Preset(
        name="survey",
        description="综述/方向概览：按类目宽召回，用于摸一个方向的整体面貌（配 facets 限定 cat）",
        params={"channels": "hybrid", "limit": 30},
    ),
    "method": Preset(
        name="method",
        description="方法检索：查某方法/技术（如 transformer / federated learning）在语料中的论文",
        params={"channels": "hybrid", "limit": 20},
    ),
    "similar": Preset(
        name="similar",
        description="相似论文：给一段摘要或题目，找语义最接近的已收录论文（纯向量通道）",
        params={"channels": "vector", "limit": 10},
    ),
    "recent": Preset(
        name="recent",
        description="最新入库：按标题关键词找近期论文（配合 facets 的 cat 限定领域）",
        params={"channels": "keyword", "limit": 20},
    ),
}


# 垂域自身持有权威配置：覆盖 bootstrap 里的默认项（bootstrap 可能先跑）
bootstrap_defaults()
replace(
    Vertical(
        name="papers",
        strategy=SlidingWindow(target=1200, max_size=1800),
        facets=FACETS,
        # 两处键位错配，不声明就恒空（2026-11-13 生产实测，均 0 命中）：
        #  · cat：arXiv 原始类目落在 metadata.categories（数组）里，要按包含匹配；
        #  · origin：取值在**表列** origin 上，metadata 里没有这个键。
        facet_paths={
            "cat": ("categories", "overlap"),
            "origin": ("origin", "column"),
        },
        parser=PaperParser(),
        tsv_config="simple",
        presets=PRESETS,
        language="multi",
        display="论文",
    )
)

"""论文垂域：四件配置（解析器 + 切块策略 + facet 词表 + 预设包）。

接入只走注册，不改引擎核心。CI 有垂域越界检查守着。
"""
from __future__ import annotations

from ...chunking import SlidingWindow
from ..registry import Vertical, bootstrap_defaults, replace

FACETS = {
    "cat": frozenset({"q-fin", "q-fin.CP", "q-fin.ST", "cs.CL", "cs.LG", "econ.GN", "econ.EM", "stat.ML"}),
    "origin": frozenset({"crawl", "upload"}),
    "has_fulltext": frozenset({"true", "false"}),
}


class PaperParser:
    """域解析器：干净文本 -> 结构树 + 元数据（实现见 parser 模块）。"""

    def __call__(self, text: str):
        from .parser import parse_latex

        return parse_latex(text)


# 垂域自身持有权威配置：覆盖 bootstrap 里的默认项（bootstrap 可能先跑）
bootstrap_defaults()
replace(
    Vertical(
        name="papers",
        strategy=SlidingWindow(target=1200, max_size=1800),
        facets=FACETS,
        parser=PaperParser(),
        tsv_config="simple",
        language="multi",
        display="论文",
    )
)

"""资讯垂域：四件配置（解析器 + 切块策略 + facet 词表 + 预设包）。

口径：资讯/公告是短文本，检索单位是**段落**而非章节。切块用滑窗保证无损
还原；facet 只收稳定的来源类型与语言，不把易变的站名写进词表。
"""
from __future__ import annotations

from ...chunking import SlidingWindow
from ...presets import Preset
from ..registry import Vertical, bootstrap_defaults, replace
from .facets import project_facets

FACETS = {
    "source_type": frozenset({"announcement", "ministry", "interaction", "news"}),
    "lang": frozenset({"zh", "en"}),
    # 正文是否可取：分析面据此决定能否做逐字证据引用
    "has_body": frozenset({"true", "false"}),
}

PRESETS = {
    "latest": Preset(
        name="latest",
        description="最新条目：按时间倒序取一批资讯（配合 facets 限定来源类型）",
        params={"channels": "keyword", "limit": 50},
    ),
    "by_entity": Preset(
        name="by_entity",
        description="实体相关：查某公司/行业的公告与互动问答",
        params={"channels": "hybrid", "limit": 30},
    ),
    "timeline": Preset(
        name="timeline",
        description="事件沿革：同一主题在不同时间的资讯，按时间排列",
        params={"channels": "hybrid", "limit": 40},
    ),
    "similar": Preset(
        name="similar",
        description="相似段落：给一段文本，找语义相近的资讯段落（纯向量通道）",
        params={"channels": "vector", "limit": 10},
    ),
}


class NewsParser:
    """域解析器：干净文本 -> 结构树 + 元数据（实现见 parser 模块）。"""

    def __call__(self, text: str):
        from .parser import parse_news

        return parse_news(text)


bootstrap_defaults()
replace(
    Vertical(
        name="news",
        strategy=SlidingWindow(target=800, max_size=1500),
        facets=FACETS,
        facet_projector=project_facets,
        parser=NewsParser(),
        cjk=True,
        presets=PRESETS,
        tsv_config="simple",
        language="zh",
        display="资讯",
    )
)

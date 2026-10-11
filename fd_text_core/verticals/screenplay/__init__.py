"""剧本垂域：四件配置（解析器 + 切块策略 + facet 词表 + 预设包）。

接入只走注册，不改引擎核心。CI 有路径越界检查守着。
"""
from __future__ import annotations

from ...chunking import SlidingWindow
from ...presets import Preset
from ..registry import Vertical, bootstrap_defaults, replace

FACETS = {
    "tradition": frozenset({"western-classic", "chinese-classic", "modern-public-domain"}),
    "language": frozenset({"en", "zh"}),
    "period": frozenset({"ancient", "early-modern", "modern"}),
}

# 四件配置的第四件：检索预设包。参数只允许声明式（facets/channels/limit），
# 执行统一走 hybrid_search，facet 照过词表校验（见 presets 模块）。
PRESETS = {
    "structure": Preset(
        name="structure",
        description="结构分析：按场景/折单元检索，用于摸清剧目骨架（幕-场-说话人层级）",
        params={"channels": "keyword", "limit": 50},
    ),
    "character": Preset(
        name="character",
        description="角色检索：查某角色的台词与出场段落（name 传角色名，如 竇娥 / Hamlet）",
        params={"channels": "hybrid", "limit": 30},
        query_template="{name}",
    ),
    "scene": Preset(
        name="scene",
        description="场景检索：查完整场景单元内容（配 facets 限定 tradition/language 更准）",
        params={"channels": "hybrid", "limit": 20},
    ),
    "similar": Preset(
        name="similar",
        description="相似段落：给一段参考文本，找语义相近的其他段落（纯向量通道）",
        params={"channels": "vector", "limit": 10},
    ),
}


class ScreenplayParser:
    """域解析器：干净文本 -> 结构树 + 元数据（实现见 parser 模块）。"""

    def __call__(self, text: str):
        from .parser import parse_play

        return parse_play(text)


# 垂域自身持有权威配置（bootstrap 只是「无垂域目录也能跑」的兜底）
bootstrap_defaults()
replace(
    Vertical(
        name="screenplay",
        # 场景单元优先由解析器产出结构，这里给引擎层兜底策略
        strategy=SlidingWindow(target=800, max_size=1400),
        facets=FACETS,
        parser=ScreenplayParser(),
        # 引擎原生 CJK 分词（unigram+bigram）：实测 simple 直通把整段中文
        # 当一个 token、中文查询 0 命中；zhparser 在实例不可得
        # （见 text-cjk-tokenization）。将来装 zhparser 只需 cjk=False + 切配置。
        cjk=True,
        presets=PRESETS,
        tsv_config="simple",
        language="zh",
        display="剧本",
    )
)

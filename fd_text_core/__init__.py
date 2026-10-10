"""fd-text-core — 通用文本处理引擎（纯基础设施，无产品面）。

垂域以四件配置接入：域解析器 + 切块策略 + facet 词表 + 检索预设包。
接入新垂域不得修改本引擎核心模块，详见 verticals/REGISTRY.md。
"""

__version__ = "0.1.0"

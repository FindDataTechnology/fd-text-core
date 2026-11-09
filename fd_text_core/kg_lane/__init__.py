"""KG 车道内核：垂域无关的时序知识图谱能力。

分工：
  - 本包只放**垂域无关**的机制（容错解析、嵌入、成本、查询、评测）。
  - 垂域差异（实体类型本体、抽取指令、episode 粒度、语料范围）走配置面（见 config.py）。
  - 引擎核心（`fd_text_core/*.py` 十个模块）**零改动**：CI 的 CORE_MODULES 路径检查
    与 BANNED_IMPORTS（含 openai）守着这条线。

设计依据是 2026-10 法律试点（512/512 episode 零失败）的实测，五个模块各自
封装的都是试点里真踩过的坑，不是推测。
"""
from __future__ import annotations

import os

# graphiti 遥测默认关闭（须早于 import；见试点）
os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")

__all__ = ["config", "cost", "embedding", "eval", "llm", "query"]

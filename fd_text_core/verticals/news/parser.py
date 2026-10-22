"""资讯正文的最小解析：标题 + 段落 + 元数据。

不做语义抽槽（主体/金额/期限留给分析层与 fd-sentiment），只保证两件事：
- 段落边界来自原文空行，切块后拼接可逐字还原；
- 标题与正文分开，检索结果能先给出标题。
"""
from __future__ import annotations

import re

_BLANK = re.compile(r"\n\s*\n+")
_WS = re.compile(r"[ \t　]+")


def clean_news_text(raw: str) -> str:
    """去行尾空白、压缩行内空白、统一空行；不删除正文内容。"""
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WS.sub(" ", line).rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def parse_news(raw: str, *, title: str | None = None) -> tuple[dict, dict]:
    """返回 ``(结构树, 元数据)``。

    结构树：``{"title": str, "paragraphs": [str, ...]}``
    元数据：``{"char_count": int, "paragraph_count": int}``
    """
    text = clean_news_text(raw)
    paragraphs = [p.strip() for p in _BLANK.split(text) if p.strip()]
    if not paragraphs:
        return {"title": (title or "").strip(), "paragraphs": []}, {
            "char_count": 0,
            "paragraph_count": 0,
        }
    if title:
        head = title.strip()
    else:
        # 无显式标题时，首段短于 120 字且无句末标点才当作标题，否则整篇都是正文。
        first = paragraphs[0]
        head = first if len(first) <= 120 and not first.endswith(("。", "！", "？", "；")) else ""
        if head:
            paragraphs = paragraphs[1:]
    return {"title": head, "paragraphs": paragraphs}, {
        "char_count": len(text),
        "paragraph_count": len(paragraphs),
    }

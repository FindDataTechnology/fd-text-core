"""CJK 分词：引擎原生 unigram + bigram（零第三方依赖）。

为什么不用 zhparser：实测引擎实例的 `pg_available_extensions` 无该扩展、
宿主无管理通道（2026-10-11）。为什么不用 pg_trgm：字符级近似与 tsvector
的 rank 体系脱节，属「能跑但不好」。bigram 是搜索引擎处理 CJK 的经典做法
（Lucene CJKAnalyzer 同思路）：无词典、对未登录词天然友好，token 边界与
用户查询里的连续两字对齐。

**同构不变量**：写入与查询必须用同一对函数产 token。写入切 bigram、查询
按空白切词，则两边 token 永不相交、检索静默返回空——这是最难排查的一类
故障，本模块的存在就是为了让两边只有一个来源。

zhparser 将来若装机：垂域把 cjk 配置关掉、tsv_config 切过去即可，
引擎代码零改动（text-cjk-tokenization design §5）。
"""
from __future__ import annotations

import re

# CJK 统一表意文字 + 扩展 A + 兼容表意 + 假名/谚文（后两者未实测，标注见 design）
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]+")
# 非 CJK 词：字母数字（含内部连字符/撇号，如 "well-known"）
_WORD = re.compile(r"[A-Za-z0-9]+(?:['\-][A-Za-z0-9]+)*")
# tsquery 安全过滤：只保留字词字符。产出的 token 直接进 to_tsquery，
# 过滤是防注入面（`&|!():*` 等元字符不得穿过）
_SAFE = re.compile(r"[\W_]+", re.UNICODE)


def _safe(tok: str) -> str:
    return _SAFE.sub("", tok)


def cjk_tokens(text: str) -> list[str]:
    """写入侧分词：CJK 段产出 unigram + bigram，非 CJK 段产出整词。

    unigram 一并产出是为了单字查询与短词召回；代价是索引略大。
    """
    if not text:
        return []
    out: list[str] = []
    for run in _CJK.findall(text):
        out.extend(run)  # unigram
        out.extend(run[i : i + 2] for i in range(len(run) - 1))  # bigram
    out.extend(_WORD.findall(text))
    return [t for t in out if t]


def cjk_tokens_text(text: str) -> str:
    """写入侧：token 以空格连接，喂 `to_tsvector('simple', ...)`。

    simple 配置按空白切分且不做词干化/停用词，正好承接我们的 token。
    """
    return " ".join(cjk_tokens(text))


def cjk_tsquery(query: str) -> str:
    """查询侧：CJK 段取 bigram（单字取 unigram），非 CJK 词原样，OR 连接。

    召回优先：多字查询拆成多个 bigram 后 OR，任一命中即召回，
    排序交给 `ts_rank_cd`（按匹配 token 数）与 RRF 融合。
    """
    if not query:
        return ""
    parts: list[str] = []
    for run in _CJK.findall(query):
        if len(run) == 1:
            parts.append(run)
        else:
            parts.extend(run[i : i + 2] for i in range(len(run) - 1))
    parts.extend(_WORD.findall(query))
    seen: dict[str, None] = {}
    for p in parts:
        s = _safe(p)
        if s:
            seen.setdefault(s, None)
    return " | ".join(seen)

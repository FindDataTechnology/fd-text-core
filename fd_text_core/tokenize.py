"""CJK 分词：引擎原生 unigram + bigram（零第三方依赖）。

为什么不用 zhparser：实测引擎实例的 `pg_available_extensions` 无该扩展、
宿主无管理通道（2026-10-11）。为什么不用 pg_trgm：字符级近似与 tsvector
的 rank 体系脱节，属「能跑但不好」。bigram 是搜索引擎处理 CJK 的经典做法
（Lucene CJKAnalyzer 同思路）：无词典、对未登录词天然友好，token 边界与
用户查询里的连续两字对齐。

**同构不变量（双维度）**：写入与查询必须用同一对函数产 token 且同一套字符
归一。写入切 bigram、查询按空白切词，则两边 token 永不相交、检索静默返回空；
同理写入保留繁体、查询输入简体（`竇`≠`窦` 是不同码点）也永不相交——这是最
难排查的一类故障（0 命中而非报错），本模块的存在就是为了让两边只有一个来源。

**归一位置**：繁→简折简发生在分词**内部**（见 `fold_t2s` 的调用点），因此写入
与查询自动共用同一条 (归一∘切分) 路径，不需要调用方各自记住折简。

zhparser 将来若装机：垂域把 cjk 配置关掉、tsv_config 切过去即可，
引擎代码零改动（text-cjk-tokenization design §5）。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

# CJK 统一表意文字 + 扩展 A + 兼容表意 + 假名/谚文（后两者未实测，标注见 design）
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]+")
# 非 CJK 词：字母数字（含内部连字符/撇号，如 "well-known"）
_WORD = re.compile(r"[A-Za-z0-9]+(?:['\-][A-Za-z0-9]+)*")
# tsquery 安全过滤：只保留字词字符。产出的 token 直接进 to_tsquery，
# 过滤是防注入面（`&|!():*` 等元字符不得穿过）
_SAFE = re.compile(r"[\W_]+", re.UNICODE)

_T2S_PATH = Path(__file__).parent / "data" / "t2s.json"


@lru_cache(maxsize=1)
def _t2s_map() -> dict[str, str]:
    """繁→简单字映射（OpenCC TSCharacters，Apache-2.0；数据文件头部有来源信息）。

    查表开销 O(1)/字符；映射在进程内只读一次（lru_cache），不存在运行时
    被上游改动的风险——表随仓版本走。文件缺失时**如实返回空表**：归一失效
    会让繁简互查退化为 0 命中，但不会崩溃或产生错误结果；调用方可据此告警。
    """
    try:
        doc = json.loads(_T2S_PATH.read_text(encoding="utf-8"))
        return doc.get("map", {})
    except (OSError, ValueError):
        return {}


def fold_t2s(text: str) -> str:
    """把文本中的繁体字符折为简体（按码点查表，未命中字符原样通过）。

    幂等：表的目标侧是简体字，再查一次不命中（`fold_t2s(fold_t2s(x)) == fold_t2s(x)`）。
    非汉字符（ASCII、标点、假名等）不在表中，逐字原样通过——英文路径零影响。
    """
    if not text:
        return text
    tmap = _t2s_map()
    if not tmap:
        return text
    # 纯 ASCII 快路径：中文垂域里英文块不小，避免无谓逐字扫描
    if text.isascii():
        return text
    return "".join(tmap.get(ch, ch) for ch in text)


def _safe(tok: str) -> str:
    return _SAFE.sub("", tok)


def _prep(text: str, t2s: bool) -> str:
    """归一入口：写入与查询两侧都必须经此（同构不变量的单点）。

    `t2s=False` 时直通（版本学/训诂比对类垂域）。两侧必须同取一个值——
    一侧折简另一侧不折，token 依然不相交（正是本模块要消灭的失配形态）。
    """
    return fold_t2s(text) if t2s else text


def cjk_tokens(text: str, *, t2s: bool = True) -> list[str]:
    """写入侧分词：CJK 段产出 unigram + bigram，非 CJK 段产出整词。

    unigram 一并产出是为了单字查询与短词召回；代价是索引略大。
    归一在切分**之前**（`_prep`），使写入侧与查询侧共用同一条
    (归一∘切分) 路径——繁简互查因此与普通查询走同一个 token 空间。
    """
    if not text:
        return []
    text = _prep(text, t2s)
    out: list[str] = []
    for run in _CJK.findall(text):
        out.extend(run)  # unigram
        out.extend(run[i : i + 2] for i in range(len(run) - 1))  # bigram
    out.extend(_WORD.findall(text))
    return [t for t in out if t]


def cjk_tokens_text(text: str, *, t2s: bool = True) -> str:
    """写入侧：token 以空格连接，喂 `to_tsvector('simple', ...)`。

    simple 配置按空白切分且不做词干化/停用词，正好承接我们的 token。
    """
    return " ".join(cjk_tokens(text, t2s=t2s))


def cjk_tsquery(query: str, *, t2s: bool = True) -> str:
    """查询侧：CJK 段取 bigram（单字取 unigram），非 CJK 词原样，OR 连接。

    召回优先：多字查询拆成多个 bigram 后 OR，任一命中即召回，
    排序交给 `ts_rank_cd`（按匹配 token 数）与 RRF 融合。
    与写入侧同构：先 `_prep` 归一，再切分——简体查询因此能命中繁体语料。
    """
    if not query:
        return ""
    query = _prep(query, t2s)
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

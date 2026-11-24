"""资讯垂域的 facet 投影：source_type / lang / has_body。

为什么住这里（change: unify-facet-projection-at-source D4）：此前 source→source_type
的映射表硬编码在 `scripts/load_news_spool.py::_facet_source_type` 里——那是 news
的知识住进了加载器。它曾导致 govcn 被当 announcement 跳过不嵌（2026-11-23 实测：
14831 篇全被 skip）。搬回垂域后，加载器只调投影，不认识任何源名。

生产者（fd-info）现在把 source_type 随 spool 行落盘（见 info-stream 能力），投影优先
用它；旧 spool 行无该字段时走下面的兜底映射（向后兼容，逻辑住垂域不在加载器）。
"""
from __future__ import annotations

#: 源名 → source_type 兜底（旧 spool 行无 source_type 字段时用）。
#: **govcn 必须是 policy**：政策文件是正文语料、要嵌向量（语义检索用），落
#: announcement 基类会被 EMBED_SKIP_SOURCE_TYPES 跳过不嵌——2026-11-23 实测 14831 篇
#: 全被当 announcement 跳过。这条血泪知识住这里，因为「哪类要嵌」是 news/引擎策略，
#: 不是加载器的事。
_SOURCE_TYPE_FALLBACK = {
    "cninfo": "announcement",
    "cn-gov": "ministry",
    "sse-interaction": "interaction",
    "news-rss": "news",
    "govcn": "policy",
}


def project_facets(record: dict) -> dict:
    """一行记录 → {source_type, lang, has_body}（news 垂域三件 facet）。

    record 键（缺键按 None/空处理）：source / source_type / content / lang / ...
    """
    source = str(record.get("source") or "").strip()
    source_type = str(record.get("source_type") or "").strip()
    if not source_type:
        source_type = _SOURCE_TYPE_FALLBACK.get(source, "announcement")
    lang = str(record.get("lang") or "zh")
    # has_body：正文在 content 列**或有冷存指针**即可取（长正文落冷存，引擎按 offset 取回）。
    # 只看 content 会把冷存文档误判 has_body=false（news 实测 118 篇长正文在冷存）。
    has_body = bool((record.get("content") or "").strip() or record.get("cold_block_key"))
    return {
        "source_type": source_type,
        "lang": lang,
        "has_body": "true" if has_body else "false",
    }

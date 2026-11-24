"""公文垂域的 facet 投影：从一行原始记录算出 issuer/doc_type/period/sections。

为什么住这里（change: unify-facet-projection-at-source D1/D3）：facet 怎么算是**垂域
自己的知识**——公文词表、机关归一、文种判定、落款抽取都在本包内。加载器、回填脚本、
质量门一律经注册表按域名取投影调用，不认识任何公文规则。此前这段逻辑散在
`scripts/load_news_spool.py`、`scripts/load_gov.py`、旧的两个回填脚本各写一遍，
于是各漏一遍（govcn 14831 篇入库后 facet 全空、period=2024 命中 0）。

纯函数、确定、无 IO：同一输入永远产出同一 dict —— 这是「增量入库与存量回填用同一投影、
产物逐键相等」这条不变量的前提（design D5/D7）。
"""
from __future__ import annotations

import re

from .parser import (
    _detect_doc_type,
    _issuer_from_signature,
    _issuer_from_title,
    _normalize_issuer,
    _split_sections,
)

FACETS = {
    # 发文机关：**按实测语料分布定**（2026-10-13，209 篇）。词表只收「本部委级」机关名——
    # 解析器已把「财政部办公厅」「财政部会计司」这类内部机构归一到主体机关
    # （见 parser._normalize_issuer），故这里不需要列司/处级取值。
    # 「其他」承接实测出现的联合发文与非部委主体（部委联合发文/企业/法院等），
    # 它们是真实数据的一部分，如实归入而不臆造机关名。
    "issuer": frozenset(
        {
            "自然资源部",
            "财政部",
            "国家发展改革委",
            "生态环境部",
            "交通运输部",
            "水利部",
            "国家能源局",
            "国家数据局",
            "国家税务总局",
            "中国人民银行",
            "市场监管总局",
            "最高人民法院",
            "最高人民检察院",
            "国家核安全局",
            "国家外汇管理局",
            "其他",
        }
    ),
    # 文种：按《党政机关公文处理工作条例》主要类别 + 实测补入的「公示」
    # （部委公告栏里「XX结果的公示」占相当比例，非法定文种但文类标记稳定）
    "doc_type": frozenset(
        {"通知", "公告", "批复", "函", "意见", "决定", "命令", "通报", "报告", "请示", "纪要", "公示",
         "复函", "条例", "办法", "规定", "细则", "方案", "规划"}
    ),
    # 时段：**年份须覆盖语料实际跨度**（实测 2019-2026；词表窄于语料会让过滤静默漏掉旧文档）
    "period": frozenset({"2019", "2020", "2021", "2022", "2023", "2024", "2025", "2026"}),
}



def _facet_issuer(raw) -> str | None:
    """机关名归一到词表：词表内原样，词表外归「其他」，空为 None。"""
    if not raw:
        return None
    s = _normalize_issuer(str(raw))
    if not s:
        return None
    return s if s in FACETS["issuer"] else "其他"


def _facet_doc_type(raw) -> str | None:
    if not raw:
        return None
    s = str(raw).strip()
    return s if s in FACETS["doc_type"] else None


def _facet_period(raw) -> str | None:
    if not raw:
        return None
    m = re.match(r"(\d{4})", str(raw))
    return m.group(1) if m else None


def project_facets(record: dict) -> dict:
    """一行记录 → {issuer, doc_type, period, sections}（已归一，可直接落 metadata）。

    record 键（缺键按 None/空处理）：title / content / published / metadata（既有
    metadata，供幂等保留采集侧已抽项）/ source / source_type / domain。
    """
    title = record.get("title") or ""
    content = record.get("content") or ""
    existing = record.get("metadata") or {}

    # issuer：生产者已给(issuer_raw) > 正文落款 > 标题前缀 > None。
    # 落款优先于标题——gov.cn 标题 96% 是「中共中央 国务院 关于…」型不含机关名，
    # 标题只能抽到 4%，落款能到 ~40%（实测，见 parser._issuer_from_signature）。
    raw_issuer = (
        existing.get("issuer_raw")
        or existing.get("issuer")
        or _issuer_from_signature(content)
        or _issuer_from_title(title)
    )
    # doc_type：采集侧已抽则归一，否则从标题现算（条例/办法靠结尾词，纯标题即可）。
    doc_type = _facet_doc_type(existing.get("doc_type")) or _detect_doc_type(title)
    # period：成文日期优先（doc_date），回落发布时间，取年份。
    period = _facet_period(existing.get("doc_date") or existing.get("published") or record.get("published"))
    # sections：采集侧已抽就保留；否则从正文现算层级（govcn 走 news loader，正文已在）。
    sections = existing.get("sections")
    if not sections and content:
        sections = [[path, text] for path, text in _split_sections(content)] or None

    out = {"issuer": _facet_issuer(raw_issuer), "doc_type": doc_type, "period": period}
    if sections:
        out["sections"] = sections
    # issuer_raw 保留归一前原值以便追溯（不参与过滤）；有值才写。
    n = _normalize_issuer(str(raw_issuer)) if raw_issuer else None
    if n and n != out["issuer"]:
        out["issuer_raw"] = n
    return out

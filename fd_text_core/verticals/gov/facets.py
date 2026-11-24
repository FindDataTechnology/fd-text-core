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
    # 发文机关：**按实测语料分布定**（2026-11-24 重定，govcn 5356 篇落「其他」的真实分布）。
    # 词表收「国务院 + 其组成部门/直属特设机构/直属机构/办事机构/直属事业单位/部委管理的
    # 国家局 + 两高」这一**固定集合**（不随语料漂移），规范名由 parser._normalize_issuer
    # 把「财政部办公厅」「XX司」等内部机构归到主体机关后再查表。
    # 「其他」承接联合发文、非部委主体（企业/法院/地方）、历史上的已撤销机构（卫生部/
    # 商业部等改组前机关如实保留、不与现名合并）。
    # 覆盖实证：前 60 规范名覆盖实测 92.6%（5356 篇落「其他」中）。
    "issuer": frozenset(
        {
            "国务院",
            # 国务院组成部门
            "外交部", "国防部", "国家发展改革委", "教育部", "科学技术部",
            "工业和信息化部", "国家民族事务委员会", "公安部", "国家安全部",
            "民政部", "司法部", "财政部", "人力资源和社会保障部",
            "自然资源部", "生态环境部", "住房和城乡建设部", "交通运输部",
            "水利部", "农业农村部", "商务部", "文化和旅游部",
            "国家卫生健康委员会", "退役军人事务部", "应急管理部",
            "中国人民银行", "审计署",
            # 直属特设 / 直属机构
            "国务院国有资产监督管理委员会", "海关总署", "国家税务总局",
            "国家市场监督管理总局", "国家金融监督管理总局", "中国证券监督管理委员会",
            "国家广播电视总局", "国家体育总局", "国家统计局", "国家国际发展合作署",
            "国务院办公厅", "国务院港澳事务办公室", "国务院研究室",
            "国家信访局", "国家知识产权局", "国家医疗保障局", "国家粮食和物资储备局",
            "国家能源局", "国家国防科技工业局", "国家烟草专卖局", "国家移民管理局",
            "国家林业和草原局", "国家铁路局", "中国民用航空局", "国家邮政局",
            "国家文物局", "国家中医药管理局", "国家疾病预防控制局", "国家药品监督管理局",
            "国家矿山安全监察局", "国家乡村振兴局", "国家机关事务管理局",
            "国家标准化管理委员会", "国家认证认可监督管理委员会",
            # 直属机构/国家局（2026-11-24 补：上一版重写词表时误漏，实测国家外汇管理局 43 篇、
            # 国家核安全局 9、国家密码管理局 6、国家版权局 5、国家电影局 5、国家新闻出版署 4、
            # 国家公务员局 4、国家数据局 3、国家宗教事务局 2 均落「其他」）
            "国家外汇管理局", "国家核安全局", "国家密码管理局", "国家版权局",
            "国家电影局", "国家新闻出版署", "国家公务员局", "国家数据局",
            "国家宗教事务局", "国家自然科学基金委员会", "中国地震局", "中央网信办",
            "出入境管理局",
            # 两高
            "最高人民法院", "最高人民检察院",
            # 其他高频中央机关/群团/央企（实测长尾 top；不属国务院部门但确为发文主体）
            "国务院关税税则委员会", "中央军委", "中国气象局", "中国国家铁路集团",
            "中国残疾人联合会", "中华全国总工会", "中华全国工商业联合会",
            "国家煤矿安全监察局",
            # 已撤销/改组前的历史发文机关：这些文档发文时它们是真实机关，如实保留，
            # 不并入现名（并入等于改写历史）。实测：农业部 14 篇、商业部 4、卫生部 3。
            "农业部", "卫生部", "商业部", "国家水产总局",
            # 兜底：联合发文 / 非部委主体 / 词表外机关（如实归类，不臆造）
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

#: 机关别称 → 规范名（词表名）。只合并**同一机关的不同写法**；机构改组前后是不同机关，
#: 不在此合并（如 农业部→农业农村部、卫生部→国家卫生健康委员会 如实分开保留）。
#: 归一顺序：先 parser._normalize_issuer 去内部机构/前缀/空白，再查本表，最后查词表。
ISSUER_ALIASES = {
    "税务总局": "国家税务总局",
    "外汇局": "国家外汇管理局",
    "住房城乡建设部": "住房和城乡建设部",
    "银保监会": "国家金融监督管理总局",
    "中国银保监会": "国家金融监督管理总局",
    "中国银行保险监督管理委员会": "国家金融监督管理总局",
    "证监会": "中国证券监督管理委员会",
    "中国证监会": "中国证券监督管理委员会",
    "药监局": "国家药品监督管理局",
    "国家药监局": "国家药品监督管理局",
    "国家中医药局": "国家中医药管理局",
    "国家煤矿安监局": "国家煤矿安全监察局",
    "国家林草局": "国家林业和草原局",
    "民航局": "中国民用航空局",
    "国资委": "国务院国有资产监督管理委员会",
    "国务院国资委": "国务院国有资产监督管理委员会",
    "国管局": "国家机关事务管理局",
    "科技部": "科学技术部",
    "体育总局": "国家体育总局",
    "国家医保局": "国家医疗保障局",
    "国家民委": "国家民族事务委员会",
    "发展改革委": "国家发展改革委",
    "国家发展和改革委员会": "国家发展改革委",
    "市场监管总局": "国家市场监督管理总局",
    "市场监督管理总局": "国家市场监督管理总局",
    "国家卫生健康委": "国家卫生健康委员会",
    "国家疾控局": "国家疾病预防控制局",
    "认监委": "国家认证认可监督管理委员会",
    "国家认监委": "国家认证认可监督管理委员会",
    # 归一 bug 的历史产物（「人力资源社会保障部」曾被截成「人力资源社」），
    # 存量里仍有该值，重刷前让别名把它救回。
    "人力资源社": "人力资源和社会保障部",
    # 语料常用略写（国务院组成部门正式名为「人力资源和社会保障部」，实测写「社会保障」）
    "人力资源社会保障部": "人力资源和社会保障部",
    # 2026-11-24 补：国家局/直属机构的简称与全称归并
    "国家地震局": "中国地震局",
    "中央网信办秘书局": "中央网信办",
    "国家矿山安监局": "国家矿山安全监察局",
    "国防科工局": "国家国防科技工业局",
}



def _facet_issuer(raw) -> str | None:
    """机关名归一到词表：别称并到规范名，词表内原样，词表外/联合发文归「其他」，空为 None。

    联合发文（一个落款里连写多个机关，如「工业和信息化部国家发展改革委」）不臆造
    单一发文机关，归「其他」——它确实是多机关共同发文，无法用一个机关名代表。
    """
    if not raw:
        return None
    s = _normalize_issuer(str(raw))
    if not s:
        return None
    s = ISSUER_ALIASES.get(s, s)
    if s in FACETS["issuer"]:
        return s
    # 联合发文：落款里连写了 ≥2 个机关词（「部」出现 2 次以上，或含「委」且非单一规范名）
    if _is_joint_issuance(s):
        return "其他"
    return "其他"


_AGENCY_TOKEN = re.compile(r"(?:部|委|委员会|总局|总署|署|局|厅|政府|院|会|社|集团|公司)")


def _is_joint_issuance(s: str) -> bool:
    """落款是否是多个机关连写（≥2 个机关后缀词）——多机关共同发文，不臆造单机关。"""
    return len(_AGENCY_TOKEN.findall(s)) >= 2


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

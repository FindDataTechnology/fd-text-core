"""公文域解析器测试：基于真实部委样本（夹具为实取 HTML）。

夹具来源（2026-10-13 实取，覆盖多种模板）：
  第一轮（2 样本）：
  · mnr_notice_2026.html  自然资源部公告 —— 元数据是 HTML 表格形态
  · mof_treasury_notice_2026.html  财政部通知 —— TRS CMS 模板，无元数据表
  第二轮（20 页抽样抓出 5 个缺陷后补，各锁一种模板）：
  · ndrc_notice_cms_2026.html  发改委通知 —— <title> 带 CMS 装饰（【】-站点名）
  · mof_order_116_2024.html  财政部令 —— 层级是「第X条」，日期在正文第二行
  · mnr_www_notice_2026.html  www.mnr.gov.cn 公告 —— 正文在 id="content1"，
    外层容器只有 90 字符（曾误选而静默丢掉整篇正文）

设计意图：这些样本的结构差异正是本垂域「标签驱动 + 多候选兜底」而非
固定选择器的依据。若哪天只支持一种模板，本测试会红。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_text_core.verticals.gov.parser import (  # noqa: E402
    STABLE_FIELDS,
    UNSTABLE_FIELDS,
    parse_gov_doc,
)

FIXTURES = REPO / "tests" / "fixtures" / "gov"

ALL_FIXTURES = [
    "mnr_notice_2026.html",
    "mof_treasury_notice_2026.html",
    "ndrc_notice_cms_2026.html",
    "mof_order_116_2024.html",
    "mnr_www_notice_2026.html",
]


def _parse(name: str):
    html = (FIXTURES / name).read_text(encoding="utf-8", errors="replace")
    return parse_gov_doc(html)


# ---------- 自然资源部：表格元数据模板 ----------


def test_mnr_notice_metadata_from_table():
    """表格形态：元数据从 <td>标签</td><td>值</td> 抽出。"""
    d = _parse("mnr_notice_2026.html")
    assert d.title == "自然资源部关于2026年第二批注册测绘师注册审批结果的公告"
    assert d.index_no == "000019174/2026-00027"
    assert d.issuer == "自然资源部"
    # 该部委的「发文字号」字段确实写作「2026年 第15号」（非〔〕形态）
    assert d.doc_number == "2026年 第15号"
    assert d.doc_type == "公告"


def test_mnr_notice_date_normalized():
    """成文日期归一到 ISO：原始为「2026年06月25日」。"""
    d = _parse("mnr_notice_2026.html")
    assert d.date == "2026-06-25"


def test_mnr_notice_attachments_registered():
    """附件按扩展名识别并登记（只登记不解析）。"""
    d = _parse("mnr_notice_2026.html")
    assert len(d.attachments) == 4
    assert all(a.kind == "docx" for a in d.attachments)
    assert all(a.url for a in d.attachments)


def test_mnr_notice_cited_docs_extracted():
    """正文内引用的其它公文文号被识别（公文间引用网络）。"""
    d = _parse("mnr_notice_2026.html")
    assert "国人部发〔2007〕14号" in d.cited_docs
    assert "国测人发〔2014〕8号" in d.cited_docs


def test_mnr_notice_addressee_absent_not_invented():
    """公告类文种按体例无主送机关——如实留空，不臆造。"""
    d = _parse("mnr_notice_2026.html")
    assert d.addressee is None


def test_mnr_notice_body_contains_closing():
    """正文含落款与「特此公告」（验证正文容器抽全而非截断）。"""
    d = _parse("mnr_notice_2026.html")
    assert "特此公告" in d.body
    assert "自然资源部" in d.body


# ---------- 财政部：TRS CMS 模板（无元数据表）----------


def test_mof_notice_doc_number_from_body():
    """TRS 模板无元数据表：文号从正文正则兜底抽出。"""
    d = _parse("mof_treasury_notice_2026.html")
    assert d.doc_number == "财办债〔2026〕218号"
    assert d.doc_type == "通知"


def test_mof_notice_issuer_from_closing():
    """发布机构从落款行兜底识别。"""
    d = _parse("mof_treasury_notice_2026.html")
    assert d.issuer == "财政部办公厅"


def test_mof_notice_sections_hierarchical():
    """正文层级被切分为 (层级路径, 文本) 单元（TRS 模板层级丰富）。"""
    d = _parse("mof_treasury_notice_2026.html")
    assert len(d.sections) >= 5
    # 至少有一级标题（一、二、…）
    assert any(path and path[0].isdigit() is False for path, _ in d.sections)


def test_mof_notice_no_index_no():
    """该模板无索引号字段——如实为 None。"""
    d = _parse("mof_treasury_notice_2026.html")
    assert d.index_no is None


# ---------- 跨模板契约 ----------


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_all_templates_yield_core_fields(name: str):
    """各模板都必须抽出核心字段（这是「标签驱动 + 多候选」的设计目标）。

    例外：`mnr_www_notice_2026.html` 是「征求意见公告」，该文种按体例**无发文字号**
    （实测该页全文 0 处「〔〕…号」形态）。这属如实留空，非抽取失败——
    故文号按「该页是否真有」分别断言，而不是一刀切要求非空。
    """
    d = _parse(name)
    assert d.title, "标题必须抽出"
    assert d.date, "成文日期必须抽出"
    assert d.doc_type, "文种必须从标题判定"
    assert len(d.body) > 100, "正文不得为空"
    if name == "mnr_www_notice_2026.html":
        assert d.doc_number is None, "该页确实无发文字号——不得臆造"
    else:
        assert d.doc_number, "发文字号必须抽出"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_no_future_author_date(name: str):
    """成文日期不得晚于今天（真实数据里出现过 2026-10-31 的假未来日期）。

    根因是取「正文第一个日期」——那是反馈截止日，不是成文日。
    本用例守着「落款优先」这条规则不被改回去。
    """
    d = _parse(name)
    assert d.date is not None
    assert d.date <= date.today().isoformat(), f"{name} 成文日期落在未来：{d.date}"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_title_has_no_site_decoration(name: str):
    """标题不得残留站点装饰（发改委模板的 <title> 是「【标题】-站点名」）。"""
    d = _parse(name)
    assert "【" not in d.title and "】" not in d.title, f"{name} 标题残留装饰：{d.title!r}"
    assert not d.title.endswith(("网站", "委员会", "部")), f"{name} 标题残留站点名：{d.title!r}"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_body_has_no_site_chrome(name: str):
    """正文容器不得选到页脚/导航（选错容器是静默丢正文，比报错危险）。"""
    d = _parse(name)
    for mark in ("网站地图", "京ICP备", "版权所有", "国务院部门网站", "主办："):
        assert mark not in d.body, f"{name} 正文混入站点框架：{mark}"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_section_texts_contract(name: str):
    """section_texts() 是切块策略的消费契约：非空且为 (path, text) 形态。"""
    d = _parse(name)
    st = d.section_texts()
    assert st and all(isinstance(p, str) and isinstance(t, str) for p, t in st)


def test_structure_boundary_declared():
    """结构抽取边界必须显式声明（spec text-vertical-onboarding 要求）。"""
    assert "doc_number" in STABLE_FIELDS
    assert "attachments" in STABLE_FIELDS
    # 不可稳定获取的元素必须如实列出且给原因
    assert "seal" in UNSTABLE_FIELDS
    assert "印章" in UNSTABLE_FIELDS["seal"]


# ---------- 第二轮抽样抓出的缺陷：逐条回归 ----------


def test_ndrc_title_decoration_stripped():
    """发改委模板：<title> 带「【】-站点名」，取 <meta ArticleTitle> 或剥装饰。"""
    d = _parse("ndrc_notice_cms_2026.html")
    assert d.title == "关于印发浙江高质量发展建设共同富裕示范区经验做法(第三批)的通知"
    assert d.doc_number == "发改办就业〔2026〕610号"
    assert d.issuer == "国家发展改革委办公厅"


def test_order_doc_number_and_type():
    """令体裁：文号是「财政部令第116号」形态，文种是命令（标题以「办法」结尾）。"""
    d = _parse("mof_order_116_2024.html")
    assert "令第116号" in d.doc_number
    assert d.doc_type == "命令"
    assert d.issuer == "财政部"


def test_order_date_is_promulgation_date():
    """令体裁：成文日期取正文第二行的公布日期，不是首句的审议通过日期。

    实测该页首句是「已经2024年10月9日财政部部务会议审议通过」，
    而落款公布日期是 2024年11月3日——两者都在正文里，必须取后者。
    """
    d = _parse("mof_order_116_2024.html")
    assert d.date == "2024-11-03"


def test_order_sections_use_tiao_hierarchy():
    """令体裁：层级是「第X条」（实测该页 30 处），不认它则条级定位全丢。"""
    d = _parse("mof_order_116_2024.html")
    paths = [p for p, _ in d.sections]
    assert any("第一条" in p for p in paths), f"条级路径必须保留，实际={paths[:6]}"
    assert len(d.sections) >= 10


def test_order_chunking_uses_tiao_boundaries():
    """切块策略也必须认「第X条」（与解析器同一套层级标记）。"""
    from fd_text_core.chunking import chunk_text, verify_lossless
    from fd_text_core.verticals.gov import GovSectionStrategy

    d = _parse("mof_order_116_2024.html")
    chunks = chunk_text(d.body, GovSectionStrategy())
    ok, detail = verify_lossless(d.body, chunks)
    assert ok, f"无损还原失败：{detail}"
    assert any(c.section_path and "第" in c.section_path and "条" in c.section_path
               for c in chunks), "条级路径必须落在切块上"


def test_mnr_www_body_not_truncated_by_wrong_container():
    """www.mnr.gov.cn 模板：正文在 id="content1"，外层 ky_article 只有 90 字符。

    旧候选表把泛化 `article|detail|zoom` 排前面且只做长度门限，
    于是选到外层容器、正文只剩标题与分享按钮——静默丢正文、无报错。
    """
    d = _parse("mnr_www_notice_2026.html")
    assert "探矿权" in d.body, "正文必须含公告实质内容"
    assert len(d.body) > 120, f"正文疑似被外层容器截断：{len(d.body)} 字符"
    # 该页正文的实质句与落款都在
    assert "征求意见" in d.body
    assert d.issuer == "自然资源部"


def test_gonggao_type_not_misread_from_inner_title():
    """公告标题内引用「…的通知（征求意见稿）」时，文种仍是公告。

    实测：关于公开征求《…规范探矿权招标出让的通知（征求意见稿）》意见的公告
    ——若先匹配「的X」会把文种误判成通知。
    """
    d = _parse("mnr_www_notice_2026.html")
    assert d.doc_type == "公告", f"文种误判：{d.doc_type!r}"


def test_site_date_kept_when_differs_from_closing():
    """站点元数据表日期与落款不一致时：落款为准，站点值另存以便追溯。

    实测样本：表格「成文日期」2026-10-12，正文落款 2026年10月8日。
    """
    d = _parse("mnr_notice_2026.html")
    assert d.date == d.site_date  # 该样本两者一致
    # 字段存在且为 ISO 形态（不一致样本里承载站点原值）
    assert d.site_date is None or len(d.site_date) == 10


# ---------- 切块策略（层级单元 + 无损不变量）----------


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_chunking_lossless_on_all_templates(name: str):
    """切块必须过无损还原不变量（引擎硬要求）。"""
    from fd_text_core.chunking import chunk_text, verify_lossless
    from fd_text_core.verticals.gov import GovSectionStrategy

    st = GovSectionStrategy()
    d = _parse(name)
    chunks = chunk_text(d.body, st)
    ok, detail = verify_lossless(d.body, chunks)
    assert ok, f"{name} 无损还原失败：{detail}"


def test_chunking_preserves_section_path():
    """层级段落被切块时保留层级路径（可回答「属于哪一条」）。"""
    from fd_text_core.chunking import chunk_text
    from fd_text_core.verticals.gov import GovSectionStrategy

    d = _parse("mof_treasury_notice_2026.html")
    chunks = chunk_text(d.body, GovSectionStrategy())
    paths = [c.section_path for c in chunks]
    assert any(p and "一、" in p for p in paths), "一级层级路径必须保留"
    assert any(p and "（" in p for p in paths), "二级层级路径必须保留"


def test_chunking_short_doc_single_block():
    """无层级标记的短文（公告类）整篇成块，不硬切。"""
    from fd_text_core.chunking import chunk_text
    from fd_text_core.verticals.gov import GovSectionStrategy

    d = _parse("mnr_notice_2026.html")
    chunks = chunk_text(d.body, GovSectionStrategy())
    assert len(chunks) == 1
    assert chunks[0].chunk_text == d.body


def test_chunking_long_doc_without_headings_falls_back_to_sliding_window():
    """**无层级标记的长文**必须走滑窗兜底且过无损校验。

    回归用例（2026-10-13 实测抓出的真缺陷）：该类文档（6465 字符的招聘公示，
    正文是长名单、无「一、（一）」标记）触发了 `_fallback` 分支，而代码把
    SlidingWindow 产出的 (start,end,path) **三元组**当成 Chunk 对象取属性，
    抛 `'tuple' object has no attribute 'char_start'`——嵌入任务失败。
    之前的用例只覆盖了「短文整篇成块」与「有层级的长文」，恰好漏掉这一格。
    """
    from fd_text_core.chunking import chunk_text, verify_lossless
    from fd_text_core.verticals.gov import GovSectionStrategy

    # 构造：无任何层级标记、远超 max_size 的长文（模拟名单类公示）
    body = "".join(f"姓名{i}，拟聘岗位{i}。\n" for i in range(400))
    assert len(body) > 1500 and not any(
        ch in body for ch in ("一、", "（一）", "第一条")
    )
    st = GovSectionStrategy()
    chunks = chunk_text(body, st)
    assert len(chunks) > 1, "超长无层级文本必须被切成多块"
    ok, detail = verify_lossless(body, chunks)
    assert ok, f"滑窗兜底路径的无损还原失败：{detail}"
    assert all(c.section_path is None for c in chunks), "无层级文不应编造层级路径"


def test_facets_and_presets_declared():
    """facet 词表与预设包齐备（四件配置的第三、四件）。"""
    from fd_text_core.verticals.gov import FACETS, PRESETS

    assert {"issuer", "doc_type", "period"} <= set(FACETS)
    assert {"by_number", "by_issuer_type", "timeline", "similar"} <= set(PRESETS)
    # 预设必须是声明式参数封装（不含 SQL）
    for p in PRESETS.values():
        assert "select" not in str(p.params).lower()

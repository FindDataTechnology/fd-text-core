"""公文 facet 投影的测试（change: unify-facet-projection-at-source task 2.1/2.2）。"""
from __future__ import annotations

from fd_text_core.verticals.gov import FACETS
from fd_text_core.verticals.gov.facets import project_facets
from fd_text_core.verticals.gov.parser import _issuer_from_signature


def test_signature_extracts_agency_glued_to_date():
    assert _issuer_from_signature("市场监管总局2024年6月20日\n（此件公开发布）") == "市场监管总局"


def test_signature_extracts_agency_on_own_line():
    body = "特此公告。\n交通运输部\n2020年7月15日"
    assert _issuer_from_signature(body) == "交通运输部"


def test_signature_rejects_attachment_and_contact_lines():
    body = "附件：xxx.doc\n（联系人：张三，联系电话：010-1）"
    assert _issuer_from_signature(body) is None


def test_signature_does_not_mistake_citation_for_issuer():
    # 正文尾部只出现「引用他机关的文号」，不得判成发文机关
    body = "根据《财政部关于印发办法的通知》（财预〔2020〕1号）执行。"
    assert _issuer_from_signature(body) is None


def test_project_prefers_signature_over_title():
    rec = {
        "title": "中共中央 国务院 关于完善产权保护制度的意见",
        "content": "……\n市场监管总局2024年6月20日\n（此件公开发布）",
        "published": "2024-06-20T00:00:00Z",
    }
    out = project_facets(rec)
    # 落款「市场监管总局」经别称归一为规范名「国家市场监督管理总局」
    assert out["issuer"] == "国家市场监督管理总局"
    assert out["period"] == "2024"


def test_project_doc_type_from_title_endword():
    rec = {"title": "关于修改《中华人民共和国道路运输条例》的决定",
           "content": "", "published": "2020-01-01"}
    assert project_facets(rec)["doc_type"] == "决定"


def test_project_out_of_vocab_issuer_becomes_other_keeping_raw():
    rec = {"title": "某通知", "content": "某某企业集团\n2020年1月1日", "published": "2020-01-01"}
    out = project_facets(rec)
    assert out["issuer"] == "其他"
    assert out["issuer_raw"] == "某某企业集团"


def test_project_respects_existing_metadata():
    rec = {
        "title": "x",
        "content": "",
        "published": "2019-05-01",
        "metadata": {"issuer_raw": "财政部办公厅", "doc_type": "通知", "sections": [["一", "正文"]]},
    }
    out = project_facets(rec)
    assert out["issuer"] == "财政部"
    assert out["doc_type"] == "通知"
    assert out["sections"] == [["一", "正文"]]


def test_project_is_deterministic():
    rec = {"title": "财政部关于印发办法的通知", "content": "财政部\n2021年1月1日",
           "published": "2021-01-01"}
    assert project_facets(rec) == project_facets(rec)


def test_project_output_keys_subset_of_vocab_plus_sections():
    rec = {"title": "公告", "content": "", "published": "2022-02-02"}
    out = project_facets(rec)
    assert set(out) <= set(FACETS) | {"sections", "issuer_raw"}


def test_issuer_strips_prc_prefix():
    # 中华人民共和国住房和城乡建设部 -> 住房和城乡建设部（13 种前缀写法、130+ 篇）
    from fd_text_core.verticals.gov.facets import _facet_issuer

    assert _facet_issuer("中华人民共和国住房和城乡建设部") == "住房和城乡建设部"
    assert _facet_issuer("中华人民共和国商务部") == "商务部"


def test_issuer_aliases_merge_same_agency_spellings():
    from fd_text_core.verticals.gov.facets import _facet_issuer

    assert _facet_issuer("税务总局") == "国家税务总局"
    assert _facet_issuer("银保监会") == _facet_issuer("中国银行保险监督管理委员会") == "国家金融监督管理总局"
    assert _facet_issuer("证监会") == _facet_issuer("中国证监会") == "中国证券监督管理委员会"
    assert _facet_issuer("市场监管总局") == "国家市场监督管理总局"
    # 归一 bug 历史产物「人力资源社」+ 略写「人力资源社会保障部」都并到规范名
    assert _facet_issuer("人力资源社") == _facet_issuer("人力资源社会保障部") == "人力资源和社会保障部"


def test_issuer_does_not_merge_reorganized_agencies():
    # 农业部/卫生部 是改组前的真实发文机关，不与现名合并（如实保留，不改写历史）
    from fd_text_core.verticals.gov.facets import _facet_issuer

    assert _facet_issuer("农业部") == "农业部"
    assert _facet_issuer("卫生部") == "卫生部"


def test_joint_issuance_goes_to_other():
    # 多机关连写不臆造单机关，归「其他」
    from fd_text_core.verticals.gov.facets import _facet_issuer

    assert _facet_issuer("工业和信息化部国家发展改革委") == "其他"
    assert _facet_issuer("财政部办公厅住房城乡建设部") == "其他"


def test_central_agencies_present_in_vocab():
    """国务院直属机构/国家局必须在词表内——防「重写词表时误漏」（2026-11-24 实撞：
    国家外汇管理局 43 篇曾因重写词表漏收而落「其他」）。"""
    from fd_text_core.verticals.gov.facets import FACETS, _facet_issuer

    must = [
        "国家外汇管理局", "国家核安全局", "国家密码管理局", "国家版权局",
        "国家电影局", "国家新闻出版署", "国家公务员局", "国家数据局",
        "国家宗教事务局", "国家自然科学基金委员会", "中国地震局", "中央网信办",
        "出入境管理局",
    ]
    for name in must:
        assert name in FACETS["issuer"], f"{name} 应在 issuer 词表内"
        assert _facet_issuer(name) == name


def test_agency_aliases_resolve_to_vocab_entry():
    """每个别名目标都必须在词表内，否则别名等于无用（帮到一个仍落「其他」的名字）。"""
    from fd_text_core.verticals.gov.facets import FACETS, ISSUER_ALIASES

    for src, dst in ISSUER_ALIASES.items():
        assert dst in FACETS["issuer"], f"别名 {src}->{dst} 的目标不在词表内"

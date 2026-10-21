"""法规层确定性建图器测试（kg-lane-delivery-v1 任务 2.3，全离线、fixture 驱动）。

fixture `tests/fixtures/kg_statute/records.jsonl` 覆盖普查
（reports/kg-delivery-metadata-audit.md）实测到的全部形态：

- expiry 施行日（含 1899-12-31 脏值拒绝——坑 1）
- gzk document_no 公布括号施行句式（gzk/flk/debt26 三源 expiry 0% 填充）
- 尾部 strict/loose（含「日之日起施行」typo）
- 汉字数字年份（一九八七 / 二○一二——坑 3 扩展 1）
- 「…日实施」形态（施行/实施一字之差）
- 「自公布之日起施行」+publish 推导；publish 缺失时头部「通过日期」补
  （坑 3 扩展 2，~581 行）
- 废止：标题「关于废止…的决定」形、尾部「废止…《X》」形（普查 666 口径）、
  「《X》同时废止」前置形（扩展）、gzk 盲区（含废止字样无《》不建边——坑 2）
- 修正链：修改决定 / 修正案宿主 / （YYYY修正）版本标注
- 文号身份：同名不同文号不合并；标题令号 vs 元数据令号矛盾拒建（0 行实测
  但逻辑保留——判据沿用 gov parser：两侧均令式且数字冲突才判矛盾）
- 幂等：同输入两次构造快照相等；写入器（fake bolt）两次 upsert 图不变
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fd_text_core.kg_lane import statute_layer as sl  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/kg_statute/records.jsonl"


def load_fixture() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _load_cli():
    """按路径加载 CLI（site-packages 里的无关 `scripts` 包会遮蔽同名命名空间）。"""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "kg_statute_build.py"
    spec = importlib.util.spec_from_file_location("kg_statute_build_cli", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def rec(law_id: int) -> dict:
    return next(r for r in load_fixture() if r["id"] == law_id)


def parse(law_id: int) -> sl.ParsedStatute:
    return sl.parse_law_record(rec(law_id))


GROUP = "legal-statute-test"


# --------------------------------------------------------------- 日期与数字

def test_field_date_forms():
    assert sl.field_date("2024-02-01 00:00:00") == "2024-02-01"
    assert sl.field_date("2024/2/1") == "2024-02-01"
    assert sl.field_date("2024年2月1日") == "2024-02-01"
    assert sl.field_date("  2024-02-01 23:59:59.123 ") == "2024-02-01"
    assert sl.field_date("") is None
    assert sl.field_date(None) is None
    assert sl.field_date("not a date") is None


def test_field_date_rejects_dirty_1899():
    """普查坑：id=68011 的 expiry=1899-12-31 是数据脏值，必须拒绝。"""
    assert sl.field_date("1899-12-31 00:00:00") is None


def test_cn2int():
    assert sl.cn2int("1987") == 1987
    assert sl.cn2int("一九八七") == 1987
    assert sl.cn2int("二○一二") == 2012
    assert sl.cn2int("二〇〇七") == 2007
    assert sl.cn2int("十") == 10
    assert sl.cn2int("十二") == 12
    assert sl.cn2int("二十一") == 21
    assert sl.cn2int("廿三") is None


# ------------------------------------------------- 普查正则口径锁定（对账锚）

def test_audit_regexes_verbatim():
    """普查附录 B 是口径权威定义：正则被逐字锁定，改这里必须同步普查复算。"""
    assert sl.RE_ARABIC.pattern == (
        r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*起\s*施\s*行")
    assert sl.RE_LOOSE.pattern == (
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日[^\n。；]{0,15}?(?:起\s*)?施\s*行")
    assert sl.RE_SHISHI.pattern == (
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日[^\n。；]{0,10}?(?:起\s*)?(?:起)?实\s*施")
    assert sl.RE_PUB.pattern == (
        r"自\s*(?:公布|发布|印刷|印发|下达|通过)\s*(?:之\s*日(?:起)?)?\s*施\s*行")
    assert sl.RE_CN_YEAR.pattern == (
        r"自\s*([〇零一二三四五六七八九○0]{4})\s*年\s*([0-9〇零一二三四五六七八九十○]{1,3})\s*月"
        r"\s*([0-9〇零一二三四五六七八九十○]{1,3})\s*日\s*起\s*施\s*行")
    assert sl.RE_REPEAL_BOOK.pattern == (
        r"(?:同时\s*)?废\s*止\s*[^。；\n]{0,120}?《([^》]{2,60})》")


# --------------------------------------------------------------- 施行日链

def test_effective_expiry_field_is_effective_date():
    """坑 1：expiry 字段实为施行日（尚未生效的条例该值是未来日）。"""
    p = parse(101)
    assert p.eff.date == "2027-01-01"
    assert p.eff.source == "expiry"


def test_effective_expiry_dirty_value_falls_through_to_shishi():
    p = parse(102)  # 黑龙江省产品质量条例「自2001年12月1日起实施」
    assert p.eff.date == "2001-12-01"
    assert p.eff.source == "tail_shishi"


def test_effective_docno_gzk():
    """gzk 三源 expiry 0% 填充——施行日靠 document_no 公布括号。"""
    p = parse(103)
    assert p.eff.date == "2024-02-01"
    assert p.eff.source == "docno"
    assert p.identity_docno == "国家发展改革委令第6号"
    assert p.node_name == "企业投资项目核准和备案管理条例（国家发展改革委令第6号）"


def test_effective_tail_strict_and_typo():
    assert parse(104).eff == sl.EffectiveInfo("2011-06-01", "tail")
    assert parse(105).eff == sl.EffectiveInfo("2011-06-01", "tail")  # 「日之日起施行」typo


def test_effective_cn_year_variants():
    assert parse(106).eff == sl.EffectiveInfo("1987-04-01", "tail_cn_year")
    assert parse(107).eff == sl.EffectiveInfo("2012-01-01", "tail_cn_year")  # 二○一二


def test_effective_pub_derived():
    p = parse(108)  # 「自发布之日起施行」+ publish
    assert p.eff.date == "1999-12-15"
    assert p.eff.source == "pub_derived"


def test_effective_head_pass_extension():
    """坑 3 扩展 2：publish 缺失时头部「X年X月X日…通过」补（~581 行可回收）。"""
    p = parse(109)
    assert p.eff.date == "1991-05-17"
    assert p.eff.source == "head_pass_derived"


def test_effective_missing():
    p = parse(110)
    assert p.eff.date is None and p.eff.source is None


def test_effective_cn_year_form_only_no_date():
    """命中形态但日期不可解（月 13）→ 不猜日期，形态可得、无时效边。"""
    p = parse(124)
    assert p.eff.date is None
    assert p.eff.source == "tail_cn_year"
    assert p.eff.known_form is True


def test_effective_priority_expiry_over_tail():
    assert parse(116).eff.source == "expiry"  # 尾部也有施行句式，expiry 优先


# --------------------------------------------------------------- 废止边

def test_repeal_title_form_with_targets():
    p = parse(111)
    assert p.repeal_tags == ("repeal_title", "repeal_tail")
    assert "户外广告管理条例" in p.repeal_targets
    assert "呼和浩特市户外广告设置管理规定" in p.repeal_targets


def test_repeal_tail_audit_form():
    """普查 666 口径：尾部「（同时）废止…《X》」。"""
    p = parse(112)
    assert p.repeal_tags == ("repeal_tail",)
    assert p.repeal_targets == ("大连市城镇房产管理暂行规定",)


def test_repeal_pre_form_extension():
    """扩展：《X》…同时废止（书名号在「废止」之前，普查 §6 样本 id=66582 形态）。"""
    p = parse(113)
    assert p.repeal_tags == ("repeal_tail_pre",)
    assert p.repeal_targets == ("某省中外合资经营企业劳动管理暂行规定",)


def test_gzk_blindspot_no_repeal_edge():
    """坑 2：gzk 正文含「废止」字样但无《》目标——盲区不强判。"""
    p = parse(114)
    assert p.repeal_targets == ()
    assert p.repeal_tags == ()


# --------------------------------------------------------------- 修正链

def test_amend_modify_decision_links_corpus():
    p = parse(115)
    assert p.amend.kind == "modify_decision"
    assert p.amend.targets == ("抚顺市物业管理条例",)


def test_amend_bill_host():
    p = parse(117)
    assert p.amend.kind == "amendment_bill"
    assert p.amend.targets == ("中华人民共和国刑法",)


def test_amend_version_paren():
    p = parse(118)
    assert p.amend.kind == "version_paren"
    assert p.amend.version_year == 2018
    assert p.amend.base_name == "中华人民共和国个人所得税法"


# --------------------------------------------------------------- 文号身份

def test_docno_identity_same_title_different_docno():
    """spec/文号身份：同名不同文号是不同版本，节点不合并。"""
    p113, p71 = parse(119), parse(120)
    assert p113.identity_docno == "财政部令第113号"
    assert p71.identity_docno == "财政部令第71号"
    assert p113.node_name != p71.node_name
    plan = sl.build_statute_graph(load_fixture(), group_id=GROUP)
    names = {n.name for n in plan.nodes.values()}
    assert "行政单位财务规则（财政部令第113号）" in names
    assert "行政单位财务规则（财政部令第71号）" in names


def test_docno_title_inline_kept():
    """gzk 标题内嵌令号（太原市政府令形）：文号留在名字里参与身份。"""
    dn = sl.parse_doc_numbers("太原市爱国卫生管理办法（市政府令第94号）", None)
    assert dn.title_no == "市政府令第94号"
    assert dn.title_no_num == 94
    assert dn.title_has_any_no is True


def test_docno_order_number_variants():
    for docno, expect in [
        ("(2024年1月1日某机关令2024年第3号公布)", "某机关令2024年第3号"),
        ("(2020年3月2日某委令〔2020〕第5号公布)", "某委令2020年第5号"),
        # 前导日期的「日」不得卷进机关名（含「日」机关名如日照市不受影响）
        ("(2020年1月1日日照市人民政府令第1号公布)", "日照市人民政府令第1号"),
    ]:
        dn = sl.parse_doc_numbers("无文号标题", docno)
        assert dn.meta_no == expect, docno


def test_docno_contradiction_blocked_and_recorded():
    """矛盾判据（gov parser 纪律）：两侧均令式且数字冲突 → 以标题为准、
    不据矛盾元数据建边（施行日也不吃该 docno）、行进待修清单。"""
    p = parse(121)
    assert p.docno_contradiction is True
    assert p.identity_docno == "测试总局令第12号"  # 标题侧
    assert p.eff.source == "tail"  # docno 的 2024-03-01 被丢弃
    assert p.eff.date == "2024-05-01"
    plan = sl.build_statute_graph(load_fixture(), group_id=GROUP)
    assert plan.stats["identity"]["docno_contradictions"] == 1
    row = plan.contradictions[0]
    assert row["law_id"] == 121
    assert row["title_no"] == "测试总局令第12号"
    assert row["meta_no"] == "测试总局令第13号"


# --------------------------------------------------------------- 图构造

def _plan():
    return sl.build_statute_graph(load_fixture(), group_id=GROUP)


def _edges(plan, rel):
    return [e for e in plan.edges.values() if e.name == rel]


def _node_by_name(plan, name):
    return next(n for n in plan.nodes.values() if n.name == name)


def test_effective_self_edge_shape():
    plan = _plan()
    node = _node_by_name(plan, "太原市燃气管理条例")
    eff = _edges(plan, sl.REL_EFFECTIVE)
    mine = [e for e in eff if e.src == node.uuid]
    assert len(mine) == 1
    e = mine[0]
    assert e.dst == node.uuid  # 自环：施行窗口的最小确定性承载
    assert e.valid_at == "2027-01-01T00:00:00"
    assert "expiry字段" in e.fact
    assert e.episodes  # provenance 回指 episode


def test_undated_rows_get_node_but_no_temporal_edge():
    plan = _plan()
    for name in ("江西省劳动力市场管理条例", "某自治区草原管理条例"):
        node = _node_by_name(plan, name)
        assert node is not None
        assert not [e for e in plan.edges.values()
                    if e.name == sl.REL_EFFECTIVE and e.src == node.uuid]


def test_repeal_edges_and_corpus_backlink():
    plan = _plan()
    rep = _edges(plan, sl.REL_REPEALS)
    assert len(rep) == 4
    # 目标《户外广告管理条例》回链库内节点（in_corpus=1，非悬挂衍生节点）
    target = _node_by_name(plan, "户外广告管理条例")
    dec = _node_by_name(plan, "呼和浩特市人民代表大会常务委员会关于废止户外广告管理条例等2件地方性法规的决定")
    edge = next(e for e in rep if e.dst == target.uuid)
    assert edge.src == dec.uuid
    assert edge.valid_at == "2024-07-01T00:00:00"  # 决定的施行日（公布日推导）
    assert "废止《户外广告管理条例》" in edge.fact


def test_repeal_backfills_effective_invalid_at():
    """双时态窗口：被废止法的 EFFECTIVE 边拿到 invalid_at = 废止决定施行日。"""
    plan = _plan()
    target = _node_by_name(plan, "户外广告管理条例")
    e = next(e for e in plan.edges.values()
             if e.name == sl.REL_EFFECTIVE and e.src == target.uuid)
    assert e.valid_at == "1995-05-01T00:00:00"
    assert e.invalid_at == "2024-07-01T00:00:00"
    assert "2024-07-01起废止" in e.fact


def test_stub_nodes_for_out_of_corpus_targets():
    plan = _plan()
    stubs = [n for n in plan.nodes.values() if not n.props["in_corpus"]]
    stub_names = {n.name for n in stubs}
    assert stub_names == {
        "呼和浩特市户外广告设置管理规定", "大连市城镇房产管理暂行规定",
        "某省中外合资经营企业劳动管理暂行规定", "中华人民共和国刑法",
        "中华人民共和国个人所得税法", "某市制定地方性法规条例",
    }
    assert all(n.props["law_id"] == 0 for n in stubs)


def test_amend_edges():
    plan = _plan()
    assert len(_edges(plan, sl.REL_AMENDS)) == 3  # 115/117/123
    assert len(_edges(plan, sl.REL_VERSION_OF)) == 1  # 118
    # （2018年修正）版本节点 → 基础法 + version_year 属性
    node = _node_by_name(plan, "中华人民共和国个人所得税法(2018年修正)")
    assert node.props["version_year"] == 2018
    v = _edges(plan, sl.REL_VERSION_OF)[0]
    assert v.src == node.uuid
    assert v.dst == _node_by_name(plan, "中华人民共和国个人所得税法").uuid


def test_episodes_and_provenance_index():
    plan = _plan()
    assert len(plan.episodes) == 24
    ep_names = {p["name"] for p in plan.episodes.values()}
    assert "lawdb:lawv2:111" in ep_names
    prov = plan.provenance["lawdb:law:122"]
    assert prov["source_db"] == "law_db"
    assert prov["stable_id"] == "laws:122"


def test_decision_class_status_not_residual():
    """普查 §3 建模规则：决定类文书 status=未知是常态，不算残余。"""
    plan = _plan()
    res_ids = {r["law_id"] for r in plan.residual}
    assert 111 not in res_ids and 115 not in res_ids and 123 not in res_ids
    assert 110 in res_ids  # 非决定类的未知仍是残余


# --------------------------------------------------------------- 统计口径

def test_full_fixture_stats():
    """fixture 全量统计 = 逐项手算期望（与普查口径字段一一对应）。"""
    s = _plan().stats
    assert s["rows"] == 24
    assert s["effective"]["by_source"] == {
        "docno": 2, "expiry": 4, "head_pass_derived": 1, "missing": 1,
        "pub_derived": 7, "tail": 5, "tail_cn_year": 3, "tail_shishi": 1,
    }
    assert s["effective"]["dated"] == 22
    assert s["effective"]["form_only"] == 1
    assert s["effective"]["audit_chain_covered"] == 22  # 剔除扩展（head_pass）
    assert s["repeal"] == {
        "title_form_rows": 1, "tail_book_rows": 2, "tail_book_pre_ext_rows": 1,
        "audit_union_rows": 2, "edges": 4, "status_repealed_without_edge": 2,
    }
    assert s["amendment"]["audit_union_rows"] == 4
    assert s["amendment"]["modify_decision_rows"] == 2
    assert s["amendment"]["bill_rows"] == 1
    assert s["amendment"]["version_paren_rows"] == 1
    assert s["identity"] == {"nodes_with_docno": 5, "docno_contradictions": 1}
    assert s["graph"]["nodes"] == 30  # 24 库内 + 6 衍生
    assert s["graph"]["stub_nodes"] == 6
    assert s["graph"]["edges"] == 30
    assert s["graph"]["edges_by_relation"] == {
        "AMENDS": 3, "EFFECTIVE": 22, "REPEALS": 4, "VERSION_OF": 1}
    assert s["graph"]["episodes"] == 24
    assert s["residual"]["rows"] == 5
    assert s["residual"]["reasons"] == {
        "eff_form_only_no_date": 1, "no_anchor": 5, "no_eff_date": 1, "status_unknown": 1}
    assert s["llm_tokens"] == 0  # spec：法规层全量建图零 LLM 成本


def test_residual_rows_content():
    plan = _plan()
    by_id = {r["law_id"]: r for r in plan.residual}
    assert set(by_id) == {106, 107, 109, 110, 124}
    assert by_id[110]["missing"] == "no_eff_date;status_unknown;no_anchor"
    assert by_id[109]["missing"] == "no_anchor"  # 通过日扩展已救回施行日，锚仍缺
    assert by_id[124]["missing"] == "eff_form_only_no_date;no_anchor"


# --------------------------------------------------------------- 幂等

def test_build_plan_idempotent():
    """同输入构造两次，计划内容逐字节不变（批处理可重入的构造侧保证）。"""
    a = sl.build_statute_graph(load_fixture(), group_id=GROUP).snapshot()
    b = sl.build_statute_graph(load_fixture(), group_id=GROUP).snapshot()
    assert a == b


class FakeBoltWriter(sl.StatuteGraphWriter):
    """假 bolt 图：按 uuid 模拟 MERGE-SET upsert 语义（不解析 Cypher）。"""

    def __init__(self):
        super().__init__("bolt://fake:1", GROUP)
        self._graph = object()  # 绕过连接检查
        self.store: dict[str, dict[str, dict]] = {"nodes": {}, "episodes": {}, "edges": {}}

    def _run(self, cypher, params):
        target = {self.NODE_QUERY: "nodes", self.EPISODE_QUERY: "episodes",
                  self.EDGE_QUERY: "edges"}[cypher]
        for row in params["rows"]:
            self.store[target][row["uuid"]] = row  # MERGE-on-uuid + SET 覆盖


def test_writer_upsert_idempotent():
    """写入侧幂等：同计划两次 upsert，节点/边数量与内容不变。"""
    plan = _plan()
    w = FakeBoltWriter()
    counts1 = w.upsert_plan(plan)
    snap1 = copy.deepcopy(w.store)
    counts2 = w.upsert_plan(plan)
    assert counts1 == counts2
    assert w.store == snap1
    assert len(w.store["nodes"]) == 30
    assert len(w.store["edges"]) == 30
    assert len(w.store["episodes"]) == 24


def test_writer_requires_connect():
    w = sl.StatuteGraphWriter("bolt://fake:1", GROUP)
    with pytest.raises(RuntimeError, match="not connected"):
        w.upsert_plan(_plan())


# --------------------------------------------------------------- 纪律检查

def test_module_imports_no_heavy_deps():
    """图客户端/PG 客户端必须懒加载：import 模块本身不碰 falkordb/psycopg。"""
    code = (
        "import sys; import fd_text_core.kg_lane.statute_layer; "
        "assert 'falkordb' not in sys.modules and 'psycopg' not in sys.modules"
    )
    root = str(Path(__file__).resolve().parents[1])
    subprocess.run([sys.executable, "-c", code], cwd=root, check=True)


def test_fetch_sql_is_read_only():
    """纪律：law_db 只读——唯一 SQL 是 SELECT（无 INSERT/UPDATE/DELETE/DDL）。"""
    import re as _re

    FETCH_SQL = _load_cli().FETCH_SQL
    assert not _re.search(
        r"\b(insert|update|delete|drop|create|alter|truncate|grant)\b",
        FETCH_SQL, _re.IGNORECASE)
    assert "select" in FETCH_SQL.lower()


def test_parse_uri():
    assert sl.parse_uri("bolt://127.0.0.1:6889") == ("127.0.0.1", 6889)
    assert sl.parse_uri("bolt://h:6379") == ("h", 6379)


# --------------------------------------------------------------- CLI

def test_cli_dry_run_writes_reports(tmp_path, monkeypatch):
    cli = _load_cli()

    monkeypatch.setattr(cli, "fetch_records", lambda dsn, **kw: load_fixture())
    residual = tmp_path / "residual.tsv"
    report = tmp_path / "report.json"
    rc = cli.main([
        "--dsn", "postgresql://fake/law_db", "--group-id", GROUP, "--dry-run",
        "--residual-out", str(residual), "--report-out", str(report),
    ])
    assert rc == 0
    assert report.exists() and residual.exists()

    rep = json.loads(report.read_text(encoding="utf-8"))
    assert rep["mode"] == "dry-run"
    assert rep["group_id"] == GROUP
    assert rep["llm_tokens"] == 0
    assert rep["stats"]["rows"] == 24
    assert len(rep["known_limits"]) >= 3
    assert len(rep["contradictions"]) == 1
    assert rep["provenance_index"]["lawdb:law:122"]["stable_id"] == "laws:122"

    lines = residual.read_text(encoding="utf-8").splitlines()
    # 前 7 列与普查 residual.tsv 同构（law_id/title/src_db/status/missing/publish/type）
    assert lines[0].split("\t")[:7] == [
        "law_id", "title", "src_db", "status", "missing", "publish", "type"]
    assert len(lines) == 6  # header + 5 残余行
    row110 = next(line for line in lines if line.startswith("110\t"))
    assert "no_eff_date;status_unknown;no_anchor" in row110


def test_cli_seed_order_sql_params(monkeypatch):
    """order=seed 时 SQL 带 md5 种子参数（固定顺序抽样，可复算）。"""
    cli = _load_cli()

    captured: dict = {}

    class _Cur:
        def execute(self, sql, params):
            captured["sql"], captured["params"] = sql, params

        def fetchall(self):
            return []

        @property
        def description(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    # fetch_records 内部懒 import psycopg；patch 在 psycopg 包上即可拦截
    monkeypatch.setattr("psycopg.connect", lambda dsn: _Conn())
    cli.fetch_records("postgresql://fake/law_db", limit=5, order="seed", seed="kg-audit-seed42")
    assert "md5(%s || l.id::text)" in captured["sql"]
    assert captured["params"] == ["kg-audit-seed42", 5]

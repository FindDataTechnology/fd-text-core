"""load_news_spool 改调垂域投影的测试（change: unify-facet-projection-at-source task 4.1）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from load_news_spool import rows_to_docs  # noqa: E402


def _row(**kw):
    base = {
        "source": "govcn",
        "source_id": "u1",
        "title": "财政部关于印发新能源汽车产业发展规划的通知",
        "content": "各省、自治区…\n财政部\n2025年11月1日",
        "url": "http://x",
        "published_at": "2025-11-01T00:00:00Z",
        "domain": "gov",
    }
    base.update(kw)
    return base


def test_gov_domain_gets_gov_facets():
    docs, _ = rows_to_docs([_row()])
    md = docs[0].metadata
    assert docs[0].domain == "gov"
    assert md["doc_type"] == "通知"
    assert md["period"] == "2025"
    assert md["issuer"] == "财政部"


def test_news_domain_gets_news_facets():
    docs, _ = rows_to_docs([_row(source="cninfo", domain="news", title="某公司公告", content="正文")])
    md = docs[0].metadata
    assert docs[0].domain == "news"
    assert md["source_type"] == "announcement"
    assert md["lang"] == "zh"
    assert md["has_body"] == "true"


def test_govcn_news_fallback_keeps_policy():
    # 旧 spool 行无 source_type，且 domain 缺省（news）→ 兜底 govcn=policy（不被跳过不嵌）
    docs, _ = rows_to_docs([_row(domain=None, title="政策", content="正文")])
    assert docs[0].domain == "news"
    assert docs[0].metadata["source_type"] == "policy"


def test_no_hardcoded_source_type_table_in_loader():
    src = (Path(__file__).resolve().parents[1] / "scripts" / "load_news_spool.py").read_text(
        encoding="utf-8"
    )
    assert '"cninfo": "announcement"' not in src, "加载器不应再有硬编码 source_type 映射表"
    assert "_facet_source_type" not in src


def test_load_gov_has_no_private_facet_normalizers():
    src = (Path(__file__).resolve().parents[1] / "scripts" / "load_gov.py").read_text(encoding="utf-8")
    # facet 归一已下沉到垂域投影；load_gov 只保留检索头拼接
    for fn in ("def _facet_issuer", "def _facet_doc_type", "def _facet_period"):
        assert fn not in src, f"load_gov 不应再有私有归一函数 {fn}"

"""批量入库 vs 逐条入库的语义等价（ingest-batch-throughput 3.1/3.3/3.4）。

设计要点：**对照跑**。同一份混合输入（全新 / 已存在未变 / 已存在且变更 / 超冷存阈值 /
批内身份重复）分别喂给逐条路径与批量路径，比较三样东西逐字段一致：

  · `load_documents` 返回的 stats 四键
  · `corpus_documents` 的非 id 字段（status/content/content_hash/cold_block_key/origin/title）
  · `embed_tasks` 的 (domain, batch_key, state) 多重集

不上真库的用例没有意义（差异全在数据库往返与约束上），故缺 DSN 即 SKIPPED。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DOMAIN = "news"
SRC_SINGLE = "_t_eq_single"
SRC_BATCH = "_t_eq_batch"


def _url() -> str:
    return os.environ.get("FDTEXT_DATABASE_URL", "").strip()


@pytest.fixture()
def textcore_env(monkeypatch):
    monkeypatch.setenv("FDTEXT_S3_ENDPOINT", "http://test.invalid")
    monkeypatch.setenv("FDTEXT_S3_ACCESS_KEY", "test")
    monkeypatch.setenv("FDTEXT_S3_SECRET_KEY", "test")
    from fd_text_core import db

    db.reset_pool()
    yield
    db.reset_pool()


def _clean(ingest, source: str) -> None:
    ingest.execute(
        "DELETE FROM embed_tasks WHERE document_id IN"
        " (SELECT id FROM corpus_documents WHERE domain=%s AND source=%s)",
        (DOMAIN, source),
    )
    ingest.execute(
        "DELETE FROM corpus_chunks WHERE document_id IN"
        " (SELECT id FROM corpus_documents WHERE domain=%s AND source=%s)",
        (DOMAIN, source),
    )
    ingest.execute(
        "DELETE FROM corpus_documents WHERE domain=%s AND source=%s", (DOMAIN, source)
    )


def _snapshot(ingest, source: str) -> tuple[dict, list[tuple], list[tuple]]:
    docs = ingest.fetch_all(
        "SELECT source_id, status, title, content_hash, origin, cold_block_key,"
        " content, metadata::text AS md, provenance::text AS prov"
        " FROM corpus_documents WHERE domain=%s AND source=%s"
        " ORDER BY source_id, status",
        (DOMAIN, source),
    )
    tasks = ingest.fetch_all(
        "SELECT d.source_id, t.domain, t.batch_key, t.state FROM embed_tasks t"
        " JOIN corpus_documents d ON d.id = t.document_id"
        " WHERE d.domain=%s AND d.source=%s ORDER BY d.source_id, t.batch_key",
        (DOMAIN, source),
    )
    for row in docs:
        # provenance 里有抓取时间戳，每次运行都不同，比较时剔除
        row.pop("prov", None)
    return {}, [tuple(d.items()) for d in docs], [tuple(t.items()) for t in tasks]


def _mixed(ingest, source: str, cold_threshold: int) -> list:
    """同一批混合输入；source_id 相同，只有 source 不同，便于逐字段对照。"""
    return [
        ingest.Doc(domain=DOMAIN, source=source, source_id="new-1", title="新1",
                   content="全新正文1", metadata={"k": "v1"}),
        ingest.Doc(domain=DOMAIN, source=source, source_id="new-2", title="新2",
                   content="全新正文2", metadata={"k": "v2"}),
        ingest.Doc(domain=DOMAIN, source=source, source_id="dup-in-batch", title="重复A",
                   content="批内第一次", metadata={}),
        ingest.Doc(domain=DOMAIN, source=source, source_id="dup-in-batch", title="重复B",
                   content="批内第二次（应成为 current，前者 superseded）", metadata={}),
        ingest.Doc(domain=DOMAIN, source=source, source_id="cold", title="冷存",
                   content="冷" * cold_threshold, metadata={}),
    ]


def test_batch_matches_single_path_on_mixed_batch(textcore_env):
    """混合批次上，批量与逐条必须产出完全相同的行集与统计。"""
    if not _url():
        pytest.skip("缺 FDTEXT_DATABASE_URL（须指向可丢弃的测试库）")

    from fd_text_core import ingest

    def run(source: str, mode: str) -> tuple[dict, list, list]:
        _clean(ingest, source)
        docs = _mixed(ingest, source, ingest.COLD_STORE_MIN_CHARS)
        if mode == "single":
            stats = {"inserted": 0, "skipped": 0, "superseded": 0, "queued": 0}
            for d in docs:
                ingest._load_one(d, "eq-key", stats)
        else:
            stats = ingest.load_documents(docs, batch_key="eq-key")
        snap = _snapshot(ingest, source)
        # 再做一轮「全部已存在」的重放：两条路径在 dedup 上也必须一致
        docs2 = _mixed(ingest, source, ingest.COLD_STORE_MIN_CHARS)
        if mode == "single":
            stats2 = {"inserted": 0, "skipped": 0, "superseded": 0, "queued": 0}
            for d in docs2:
                ingest._load_one(d, "eq-key2", stats2)
        else:
            stats2 = ingest.load_documents(docs2, batch_key="eq-key2")
        return stats, snap, stats2

    try:
        s_stats, s_docs, s_stats2 = run(SRC_SINGLE, "single")
        b_stats, b_docs, b_stats2 = run(SRC_BATCH, "batch")
    finally:
        _clean(ingest, SRC_SINGLE)
        _clean(ingest, SRC_BATCH)

    assert s_stats == b_stats, (s_stats, b_stats)
    assert s_stats2 == b_stats2, (s_stats2, b_stats2)
    # source 名不同，把它从快照里剔掉再比
    def strip(rows):
        return [tuple(v for k, v in r if k != "source") for r in rows]

    assert strip(s_docs[1]) == strip(b_docs[1]), (s_docs[1], b_docs[1])
    assert strip(s_docs[2]) == strip(b_docs[2]), (s_docs[2], b_docs[2])


def test_batch_keeps_cold_store_threshold_boundary(textcore_env):
    """冷存阈值边界不得被批量化绕过（== 阈值要落冷存，-1 要进 content 列）。"""
    if not _url():
        pytest.skip("缺 FDTEXT_DATABASE_URL（须指向可丢弃的测试库）")

    from fd_text_core import ingest

    src = "_t_eq_cold"
    n = ingest.COLD_STORE_MIN_CHARS
    try:
        _clean(ingest, src)
        ingest.load_documents([
            ingest.Doc(domain=DOMAIN, source=src, source_id="at", title="a", content="x" * n),
            ingest.Doc(domain=DOMAIN, source=src, source_id="under", title="u", content="x" * (n - 1)),
        ])
        rows = {
            r["source_id"]: r
            for r in ingest.fetch_all(
                "SELECT source_id, content, cold_block_key FROM corpus_documents"
                " WHERE domain=%s AND source=%s",
                (DOMAIN, src),
            )
        }
        assert rows["at"]["cold_block_key"], "== 阈值必须落冷存"
        assert rows["at"]["content"] is None
        assert rows["under"]["cold_block_key"] is None
        assert rows["under"]["content"] == "x" * (n - 1)
    finally:
        _clean(ingest, src)

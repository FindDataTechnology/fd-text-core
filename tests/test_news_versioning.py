"""news 域正文版本化验证（change wire-info-stream-program 1.1.3）。

同 ``(domain='news', source, source_id)`` 连续三次内容变更走引擎 ingest：
旧版全部 superseded 且可查、最新版 current、无唯一键冲突
（uq_document_current_only 部分唯一索引下第三版不撞键——该缺陷的防复发
先例见 test_ingest_contract.py，本用例把同一纪律落到 news 域与完整
load_documents 路径上）。

DB 用例先例：缺 FDTEXT_DATABASE_URL 即 SKIPPED；URL 须指向测试库。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

IDENT = ("news", "_t_news_v", "ver1")  # (domain, source, source_id)


def _url() -> str:
    return os.environ.get("FDTEXT_DATABASE_URL", "").strip()


@pytest.fixture()
def textcore_env(monkeypatch):
    """db.get_pool 经 Settings.from_env 需要 S3 三件套；短正文全程不触冷存。"""
    monkeypatch.setenv("FDTEXT_S3_ENDPOINT", "http://test.invalid")
    monkeypatch.setenv("FDTEXT_S3_ACCESS_KEY", "test")
    monkeypatch.setenv("FDTEXT_S3_SECRET_KEY", "test")
    from fd_text_core import db

    db.reset_pool()
    yield
    db.reset_pool()


def test_three_content_changes_supersede_in_order(textcore_env):
    if not _url():
        pytest.skip("缺 FDTEXT_DATABASE_URL（须指向可丢弃的测试库）")

    from fd_text_core import ingest

    domain, source, sid = IDENT

    def doc(content: str) -> ingest.Doc:
        return ingest.Doc(domain=domain, source=source, source_id=sid,
                          title="版本化测试", content=content)

    try:
        s1 = ingest.load_documents([doc("第一版正文。")])
        assert s1 == {"inserted": 1, "skipped": 0, "superseded": 0, "queued": 1}, s1
        s2 = ingest.load_documents([doc("第二版正文，修订了金额。")])
        assert s2["inserted"] == 1 and s2["superseded"] == 1, s2
        # 第三次变更是历史缺陷的引爆点：修前撞 uq_document_current 唯一约束
        s3 = ingest.load_documents([doc("第三版正文，再次修订。")])
        assert s3["inserted"] == 1 and s3["superseded"] == 1, s3

        rows = ingest.fetch_all(
            "SELECT id, status, content FROM corpus_documents"
            " WHERE domain=%s AND source=%s AND source_id=%s ORDER BY id",
            (domain, source, sid),
        )
        assert len(rows) == 3, "三次变更=三行版本历史，不合并不丢版"
        assert [r["status"] for r in rows] == ["superseded", "superseded", "current"]
        assert [r["content"] for r in rows] == [
            "第一版正文。", "第二版正文，修订了金额。", "第三版正文，再次修订。",
        ], "旧版整行保留可查（含正文）"

        currents = [r for r in rows if r["status"] == "current"]
        assert len(currents) == 1
        assert currents[0]["id"] in ingest.current_document_ids(domain), \
            "检索面只见最新版"

        # 同内容重放：幂等跳过，不产生第四行、不撞键
        s4 = ingest.load_documents([doc("第三版正文，再次修订。")])
        assert s4["skipped"] == 1 and s4["inserted"] == 0, s4
        again = ingest.fetch_one(
            "SELECT count(*) AS n FROM corpus_documents"
            " WHERE domain=%s AND source=%s AND source_id=%s",
            (domain, source, sid),
        )
        assert again["n"] == 3
    finally:
        ingest.execute(
            "DELETE FROM corpus_documents WHERE domain=%s AND source=%s",
            (domain, source),
        )


def test_nul_in_body_ingests_after_entry_cleanup(textcore_env):
    """回归：带 NUL 的正文必须能入库（实录 fd-info-ship-spool 连续 13 天失败）。

    两段断言缺一不可：
      ① **先证明这条路本来会炸**——绕过引擎直接插含 NUL 的文本，真 psycopg/PG 抛
         `DataError: … cannot contain NUL (0x00) bytes`。否则这个用例只是"跑了没报错"，
         换个早就清洗过的实现也一样绿（自证陷阱，见 fake-objects-must-mirror-installed-source）。
      ② 再走 `load_documents` 真路径，断言入库成功且**库里存的是清洗后的正文**。
    """
    psycopg = pytest.importorskip("psycopg")
    if not _url():
        pytest.skip("缺 FDTEXT_DATABASE_URL（须指向可丢弃的测试库）")

    from fd_text_core import ingest

    dirty = "正文左\x00右"
    cleaned = "正文左右"

    # ① 证明这条路本来会炸（与 CronJob 日志里的报错同型）
    with psycopg.connect(_url().replace("postgresql://", "postgres://")) as c, c.cursor() as cur:
        with pytest.raises(psycopg.DataError):
            cur.execute("SELECT %s::text AS t", (dirty,))
        c.rollback()

    # ② 走引擎入口：能入库，且落库的是清洗后的正文
    domain, source, sid = "news", "_t_nul_spool", "nul1"
    try:
        stats = ingest.load_documents([
            ingest.Doc(domain=domain, source=source, source_id=sid,
                       title="标\x00题", content=dirty),
        ])
        assert stats["inserted"] == 1, stats
        row = ingest.fetch_one(
            "SELECT title, content FROM corpus_documents"
            " WHERE domain=%s AND source=%s AND source_id=%s AND status='current'",
            (domain, source, sid),
        )
        assert row["content"] == cleaned, "库里不得残留 NUL"
        assert row["title"] == "标题"
        # 同内容重放必须幂等（哈希在清洗之后算，否则每轮都判新版并无限堆版本）
        again = ingest.load_documents([
            ingest.Doc(domain=domain, source=source, source_id=sid,
                       title="标\x00题", content=dirty),
        ])
        assert again == {"inserted": 0, "skipped": 1, "superseded": 0, "queued": 0}, again
    finally:
        ingest.execute(
            "DELETE FROM corpus_documents WHERE domain=%s AND source=%s",
            (domain, source),
        )

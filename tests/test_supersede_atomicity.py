"""退役与写入必须同一个事务：中途失败不能把文档弄丢。

此前的顺序是「先退役（各自提交）→ 再算正文归处 → 再插入」。psycopg3 的
``with pool.connection()`` 在正常退出时提交，所以 ``_supersede`` 的三条
``execute()`` 是三个独立事务。于是这条路径上任何一处失败——冷存写失败、
唯一键冲突、连接抖动——都会留下
「旧版已 superseded + 切块与嵌入任务已删 + 新版没进来」：
文档从语料和向量检索里一起消失，且没有任何报错入口。

两条用例都用**真实可达的失败**，不用假的成功：

* 单条路径：第二版正文超过冷存阈值 → ``put_block`` 连对象存储失败（本机无 S3，
  生产上则是桶不可用/凭据过期）。
* 批量路径：最后一步入队失败（模拟）——它与正文、退役同事务，必须一起回滚。

只要 PG 就能跑；不需要对象存储（非冷存长文留在 content 列）。
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DOMAIN = "test"
SOURCE = "_t_supersede_tx"


def _url() -> str:
    return os.environ.get("FDTEXT_DATABASE_URL", "").strip()


pytestmark = pytest.mark.skipif(not _url(), reason="缺 FDTEXT_DATABASE_URL（指向测试库）")


@pytest.fixture()
def ingest(monkeypatch):
    monkeypatch.setenv("FDTEXT_DATABASE_URL", _url())
    # Settings.from_env 无条件是要求 S3 五项；给不可解析的假端点，让「冷存写失败」
    # 变成真实可达的失败路径（而不是本机缺配置这种不可比的错）。
    monkeypatch.setenv("FDTEXT_S3_ENDPOINT", "http://test.invalid")
    monkeypatch.setenv("FDTEXT_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("FDTEXT_S3_ACCESS_KEY", "test")
    monkeypatch.setenv("FDTEXT_S3_SECRET_KEY", "test")
    from fd_text_core import db
    from fd_text_core import ingest as mod

    db.reset_pool()
    yield mod
    from fd_text_core.db import execute

    execute("DELETE FROM embed_tasks WHERE domain=%s", (DOMAIN,))
    execute(
        "DELETE FROM corpus_chunks WHERE document_id IN"
        " (SELECT id FROM corpus_documents WHERE domain=%s AND source=%s)",
        (DOMAIN, SOURCE),
    )
    execute("DELETE FROM corpus_documents WHERE domain=%s AND source=%s", (DOMAIN, SOURCE))


def _doc(mod, source_id: str, body: str):
    return mod.Doc(
        domain=DOMAIN, source=SOURCE, source_id=source_id,
        title=source_id, content=body, metadata={}, origin="crawl",
    )


def _rows():
    from fd_text_core.db import fetch_all

    return fetch_all(
        "SELECT id, source_id, status, content_hash FROM corpus_documents"
        " WHERE domain=%s AND source=%s ORDER BY id",
        (DOMAIN, SOURCE),
    )


def _pending_tasks(doc_id: int) -> int:
    from fd_text_core.db import fetch_one

    row = fetch_one(
        "SELECT count(*) AS n FROM embed_tasks WHERE document_id=%s", (doc_id,)
    )
    return int(row["n"]) if row else 0


def test_cold_store_failure_leaves_previous_version_current(ingest):
    """冷存写失败时，旧版必须仍是 current，且它的嵌入任务不被删。"""
    ingest.load_documents([_doc(ingest, "doc1", "第一版正文")], batch_key="k1")
    first = _rows()
    assert len(first) == 1 and first[0]["status"] == "current"
    old_id = first[0]["id"]
    assert _pending_tasks(old_id) == 1, "第一版应有一条待办嵌入任务"

    # 第二版正文超冷存阈值 → _placement 走 put_block → 本机无对象存储，抛错。
    # 这是真实失败路径（生产上是桶不可用/凭据过期），不是我造的假错。
    long_body = "长" * (ingest.COLD_STORE_MIN_CHARS + 1)
    with pytest.raises(Exception):
        ingest.load_documents([_doc(ingest, "doc1", long_body)], batch_key="k2")

    after = _rows()
    current = [r for r in after if r["status"] == "current"]
    assert len(current) == 1, f"失败后应当仍有一个 current 版本，实际 {after}"
    assert current[0]["id"] == old_id, "旧版必须原样保留，不能被退役"
    assert not [r for r in after if r["status"] == "superseded"], "失败不该留下退役行"
    assert _pending_tasks(old_id) == 1, "旧版的待办嵌入任务不许被删——它仍然有效"


def test_last_statement_failure_rolls_back_the_supersede(ingest):
    """同事务的最后一步失败时，退役必须一起回滚。"""
    from fd_text_core import db

    ingest.load_documents([_doc(ingest, "doc2", "原始正文")], batch_key="k1")
    first = _rows()
    assert len(first) == 1
    old_id = first[0]["id"]

    real_cursor = db.cursor

    class _Boom(RuntimeError):
        pass

    class _Cur:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=None):
            if "INSERT INTO embed_tasks" in sql:
                raise _Boom("模拟入队失败")
            return self._inner.execute(sql, params or ())

        def fetchone(self):
            return self._inner.fetchone()

        def fetchall(self):
            return self._inner.fetchall()

    @contextmanager
    def _broken_cursor(commit: bool = False):
        with real_cursor(commit=commit) as inner:
            yield _Cur(inner)

    # ingest 里是 `from .db import cursor`，所以替换它自己模块上的名字
    ingest.cursor = _broken_cursor
    try:
        with pytest.raises(Exception):
            ingest.load_documents([_doc(ingest, "doc2", "第二版正文")], batch_key="k2")
    finally:
        ingest.cursor = real_cursor

    after = _rows()
    current = [r for r in after if r["status"] == "current"]
    assert len(current) == 1, f"入队失败后旧版应仍为 current，实际 {after}"
    assert current[0]["id"] == old_id
    assert _pending_tasks(old_id) == 1, "回滚后旧版待办任务仍在"

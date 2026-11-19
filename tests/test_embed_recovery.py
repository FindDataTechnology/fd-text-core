"""一次限流/5xx 之后，文档最终必须回到向量检索面。

审计（`docs/audit-ponytail/06-legal-report.md` §6）说：`run_once` 把每个失败都写成
`state='failed'`，而没有东西把任务从 failed 挪走，且内容未变的文档会被
`load_documents` 跳过，所以「一次 429/5xx = 这篇永久没向量」。

这条在 2026-11-08 17:00 的 `d752ea3` 之后**已不再成立**——那笔提交加了
`retry_rate_limited()` 并在 `run_once` 开头调用（`:292`）。本文件把这件事实钉住：
不是"代码里有这个函数"，而是"造一条 failed 任务，跑一轮，它真的被嵌进去了"。

不可重试的失败仍须保持 failed（脏文本每轮重烧 API 是另一种病）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# 垂域必须是已注册的（gov / news / papers / screenplay）：未注册域在取分词配置时
# 就抛 `未注册垂域`，任务会在阶段一再次落 failed——那是 fixture 的问题，不是被
# 测行为的问题。source 保持测试专属，清场按 (domain, source) 限定，不碰真实数据。
DOMAIN = "papers"
SOURCE = "_t_embed_recover"


def _url() -> str:
    return os.environ.get("FDTEXT_DATABASE_URL", "").strip()


pytestmark = pytest.mark.skipif(not _url(), reason="缺 FDTEXT_DATABASE_URL（指向测试库）")


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("FDTEXT_DATABASE_URL", _url())
    monkeypatch.setenv("FDTEXT_S3_ENDPOINT", "http://test.invalid")
    monkeypatch.setenv("FDTEXT_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("FDTEXT_S3_ACCESS_KEY", "test")
    monkeypatch.setenv("FDTEXT_S3_SECRET_KEY", "test")
    from fd_text_core import db

    db.reset_pool()
    yield
    from fd_text_core.db import execute

    execute("DELETE FROM embed_tasks WHERE domain=%s", (DOMAIN,))
    execute(
        "DELETE FROM corpus_chunks WHERE document_id IN"
        " (SELECT id FROM corpus_documents WHERE domain=%s AND source=%s)",
        (DOMAIN, SOURCE),
    )
    execute("DELETE FROM corpus_documents WHERE domain=%s AND source=%s", (DOMAIN, SOURCE))


class _StubEmbedder:
    """只实现 run_once 用到的两样：model 与 embed()."""

    model = "stub-model"

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        from fd_text_core.embedding import EmbedResult

        return EmbedResult(vectors=[[0.5] * 1024 for _ in texts])


def _seed_failed(error: str) -> tuple[int, int]:
    """建一篇文档 + 切块 + 一条 failed 任务；返回 (task_id, chunk_count)。"""
    from fd_text_core import ingest
    from fd_text_core.db import execute, fetch_one

    ingest.load_documents(
        [ingest.Doc(domain=DOMAIN, source=SOURCE, source_id="doc1",
                    title="doc1", content="正文" * 50, metadata={}, origin="crawl")],
        batch_key="seed",
    )
    doc = fetch_one(
        "SELECT id FROM corpus_documents WHERE domain=%s AND source=%s AND status='current'",
        (DOMAIN, SOURCE),
    )
    assert doc is not None
    execute("UPDATE corpus_chunks SET embedding=NULL WHERE document_id=%s", (doc["id"],))
    execute(
        "UPDATE embed_tasks SET state='failed', finished_at=now() - interval '20 minutes', error=%s"
        " WHERE document_id=%s",
        (error, doc["id"]),
    )
    task = fetch_one(
        "SELECT id FROM embed_tasks WHERE document_id=%s AND domain=%s", (doc["id"], DOMAIN)
    )
    n_chunks = fetch_one(
        "SELECT count(*) AS n FROM corpus_chunks WHERE document_id=%s", (doc["id"],)
    )["n"]
    return int(task["id"]), int(n_chunks)


def _state(task_id: int) -> str:
    from fd_text_core.db import fetch_one

    return fetch_one("SELECT state FROM embed_tasks WHERE id=%s", (task_id,))["state"]


def test_transient_failure_gets_retried_and_finishes(env):
    """429 / 5xx 的失败任务必须被回收重试，最终 finished（而不是永久 failed）。"""
    from fd_text_core import embed_worker as w

    task_id, n_chunks = _seed_failed(
        "EmbeddingUnavailable: 嵌入重试耗尽: 嵌入请求 status=503 body={'error': 'oops'}"
    )
    assert _state(task_id) == "failed"

    res = w.run_once(_StubEmbedder(), "stub-model", limit=10)

    assert res.reclaimed >= 0
    assert _state(task_id) == "finished", "可重试的失败必须被拉回并完成"
    assert res.embedded >= 1
    # 切块真的被写上了向量——这才是"回到检索面"的判据，不是任务状态好看
    from fd_text_core.db import fetch_one

    unembedded = fetch_one(
        "SELECT count(*) AS n FROM corpus_chunks"
        " WHERE document_id=%s AND embedding IS NULL",
        (fetch_one(
            "SELECT document_id FROM embed_tasks WHERE id=%s", (task_id,)
        )["document_id"],),
    )["n"]
    assert unembedded == 0, f"仍缺向量的切块数={unembedded}（应有 {n_chunks} 块全部补齐）"


def test_permanent_failure_stays_failed(env):
    """脏文本这类永久失败不许被反复重烧 API。"""
    from fd_text_core import embed_worker as w

    task_id, _ = _seed_failed("响应缺少第 3 条的向量")
    res = w.run_once(_StubEmbedder(), "stub-model", limit=10)

    assert _state(task_id) == "failed", "不可重试的失败必须保持 failed"
    assert res.retryable == 0

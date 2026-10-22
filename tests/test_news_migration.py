"""0009 news 分区迁移的升降级验证（change wire-info-stream-program 1.1.2）。

形态照 test_ingest_contract.py 的 DB 用例先例：psycopg 缺失或
FDTEXT_DATABASE_URL 未设置即 SKIPPED——CI 无网内通道，但测试库必须真实跑过。

升降级只动 0009 一个台阶（head ⇄ 0008）：downgrade 恰好执行 0009 的
downgrade（DROP TABLE IF EXISTS corpus_chunks_news），upgrade 回 head 恢复。
这一对操作就是被测对象本身，跑完库回到 head，不残留状态。

⚠ FDTEXT_DATABASE_URL 必须指向可丢弃的测试库（升降级会真实建删分区）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

NEWS = ("_t_mig_news", "n1")  # (source, source_id)：测试数据身份，用完即清


def _url() -> str:
    url = os.environ.get("FDTEXT_DATABASE_URL", "").strip()
    return url.replace("postgresql://", "postgres://")


def _alembic():
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    # script_location 绝对路径：不受调用方 CWD 影响
    cfg.set_main_option("script_location", str(REPO / "fd_text_core" / "migrations"))
    return command, cfg


def test_news_partition_upgrade_downgrade_roundtrip():
    """升→分区可写入可检索→降→干净回退→再升（幂等回路）。"""
    psycopg = pytest.importorskip("psycopg")
    if not _url():
        pytest.skip("缺 FDTEXT_DATABASE_URL（须指向可丢弃的测试库）")

    command, cfg = _alembic()
    with psycopg.connect(_url(), autocommit=True) as conn, conn.cursor() as cur:
        try:
            # ---- 升级到 head：全新库走全链 0001→0009，已有库幂等 ----
            command.upgrade(cfg, "head")

            # 分区存在，且 LIST 绑定正是 'news'
            cur.execute(
                """SELECT pg_get_expr(c.relpartbound, c.oid)
                   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname='public' AND c.relname='corpus_chunks_news'"""
            )
            bound = cur.fetchone()
            assert bound is not None, "corpus_chunks_news 分区应存在"
            assert bound[0] == "FOR VALUES IN ('news')", bound[0]

            # ---- 分区可写入（tsv 走引擎写入侧口径：cjk 分词后喂 simple） ----
            from fd_text_core.tokenize import cjk_tokens_text

            body = "公司发布公告全文内容"
            cur.execute(
                """INSERT INTO corpus_documents
                   (domain, source, source_id, title, content_hash, status, content)
                   VALUES ('news', %s, %s, '迁移测试公告', 'h_v1', 'current', %s)
                   RETURNING id""",
                (*NEWS, body),
            )
            doc_id = cur.fetchone()[0]
            cur.execute(
                """INSERT INTO corpus_chunks
                   (domain, document_id, chunk_no, chunk_text, char_start, char_end,
                    tsv, tsv_config)
                   VALUES ('news', %s, 1, %s, 0, 10,
                           to_tsvector('simple', %s), 'engine-cjk')
                   RETURNING id""",
                (doc_id, body, cjk_tokens_text(body)),
            )
            chunk_id = cur.fetchone()[0]

            # ---- 分区可检索：直查分区、经母表路由、全文通道三口径 ----
            cur.execute(
                "SELECT chunk_text FROM corpus_chunks_news WHERE document_id = %s",
                (doc_id,),
            )
            assert cur.fetchone()[0] == body

            cur.execute(
                "SELECT tableoid::regclass FROM corpus_chunks WHERE id = %s",
                (chunk_id,),
            )
            assert str(cur.fetchone()[0]) == "corpus_chunks_news", "news 行应路由进 news 分区"

            cur.execute(
                """SELECT chunk_text FROM corpus_chunks
                   WHERE domain = 'news'
                     AND tsv @@ plainto_tsquery('simple', '公告')"""
            )
            assert cur.fetchone() is not None, "tsv 全文检索应命中 news 分区行"

            # ---- 降一个台阶：0009 的 downgrade ----
            command.downgrade(cfg, "0008_eval_ndcg_variant")

            cur.execute("SELECT to_regclass('public.corpus_chunks_news')")
            assert cur.fetchone()[0] is None, "降级后 news 分区应消失"

            # 母表与其他垂域分区不受牵连（干净回退，只撤本垂域）
            for t in ("corpus_chunks", "corpus_chunks_papers",
                      "corpus_chunks_screenplay", "corpus_chunks_gov"):
                cur.execute("SELECT to_regclass(%s)", (f"public.{t}",))
                assert cur.fetchone()[0] is not None, f"{t} 不应被 0009 降级波及"

            cur.execute("SELECT version_num FROM alembic_version")
            assert cur.fetchone()[0] == "0008_eval_ndcg_variant"

            # 无 news 分区时，写入 news 域切块应被诚实拒绝（而非静默丢失）
            with pytest.raises(psycopg.errors.Error, match="no partition"):
                cur.execute(
                    """INSERT INTO corpus_chunks
                       (domain, document_id, chunk_no, chunk_text, char_start, char_end)
                       VALUES ('news', %s, 2, 'x', 0, 1)""",
                    (doc_id,),
                )

            # ---- 再升级：干净回退后可原路恢复（幂等回路闭环） ----
            command.upgrade(cfg, "head")
            cur.execute(
                "SELECT to_regclass('public.corpus_chunks_news'),"
                " (SELECT version_num FROM alembic_version)"
            )
            reg, ver = cur.fetchone()
            assert reg is not None and ver == "0009_news"
            # 降级丢弃的是分区行；再升级得到的是无残留的干净分区
            cur.execute(
                "SELECT count(*) FROM corpus_chunks_news WHERE document_id = %s",
                (doc_id,),
            )
            assert cur.fetchone()[0] == 0, "再升级不得复活已随分区删除的行"
            # 文档表不受 0009 波及（text-core 侧不删文档）
            cur.execute("SELECT title FROM corpus_documents WHERE id = %s", (doc_id,))
            assert cur.fetchone()[0] == "迁移测试公告"
            # 恢复后的分区再次可写可查
            cur.execute(
                """INSERT INTO corpus_chunks
                   (domain, document_id, chunk_no, chunk_text, char_start, char_end)
                   VALUES ('news', %s, 2, '恢复后可写', 0, 5)""",
                (doc_id,),
            )
            cur.execute(
                "SELECT chunk_text FROM corpus_chunks_news WHERE document_id = %s AND chunk_no = 2",
                (doc_id,),
            )
            assert cur.fetchone()[0] == "恢复后可写"
        finally:
            # 测试库自愈：清测试行；库留在 head
            cur.execute("DELETE FROM corpus_documents WHERE source = %s", (NEWS[0],))
            command.upgrade(cfg, "head")

"""0009 资讯垂域分区

新垂域 = 加一个分区（运维动作，非引擎代码改动）。接入 news 只新增本迁移，
引擎核心零 diff。

Revision ID: 0009_news
Revises: 0008_eval_ndcg_variant
Create Date: 2026-10-23
"""
from __future__ import annotations

from alembic import op

revision = "0009_news"
down_revision = "0008_eval_ndcg_variant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 幂等：分区已存在则跳过（重跑迁移不应报错）
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname='public' AND c.relname='corpus_chunks_news'
            ) THEN
                CREATE TABLE corpus_chunks_news
                    PARTITION OF corpus_chunks FOR VALUES IN ('news');
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS corpus_chunks_news")

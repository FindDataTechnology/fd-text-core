"""0002 剧本垂域分区

新垂域 = 加一个分区（运维动作，非引擎代码改动）。这是「垂域=配置」守门
不变量在存储层的体现：接入 screenplay 只新增本迁移，引擎核心零 diff。

Revision ID: 0002_screenplay
Revises: 0001_text_engine
Create Date: 2026-10-11
"""
from __future__ import annotations

from alembic import op

revision = "0002_screenplay"
down_revision = "0001_text_engine"
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
                WHERE n.nspname='public' AND c.relname='corpus_chunks_screenplay'
            ) THEN
                CREATE TABLE corpus_chunks_screenplay
                    PARTITION OF corpus_chunks FOR VALUES IN ('screenplay');
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS corpus_chunks_screenplay")

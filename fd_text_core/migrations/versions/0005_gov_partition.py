"""0005 公文垂域分区

新垂域 = 加一个分区（运维动作，非引擎代码改动）。这是「垂域=配置」守门
不变量在存储层的体现：接入 gov 只新增本迁移，引擎核心零 diff。

Revision ID: 0005_gov
Revises: 0004_hnsw
Create Date: 2026-10-13
"""
from __future__ import annotations

from alembic import op

revision = "0005_gov"
down_revision = "0004_hnsw_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 幂等：分区已存在则跳过（重跑迁移不应报错）。
    # 母表上的 HNSW 索引（0004 建）会自动为新区间建并挂上子索引——
    # **不要再显式为本分区建索引**，那会造出无法 DROP 的重复索引
    # （0004 的教训：重复索引占等量空间且 DROP 报「requires it」）。
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname='public' AND c.relname='corpus_chunks_gov'
            ) THEN
                CREATE TABLE corpus_chunks_gov
                    PARTITION OF corpus_chunks FOR VALUES IN ('gov');
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS corpus_chunks_gov")

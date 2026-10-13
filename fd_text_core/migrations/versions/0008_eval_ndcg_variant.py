"""eval_runs 升级：nDCG 三列 + variant（守门集/难题集隔离）。

为什么需要 variant（change eval-graded-harder-set）：难题集与守门集是两套
评测面，混比会把「换了评测面」伪装成趋势数字。variant 与两指纹同级参与
对比判定（`compare_with_previous`），跨变体即 not_comparable。

为什么需要 nDCG（同 change 动因）：三域守门集 MRR 全饱和（1.0），对重排类
改进零区分度；nDCG 按分级相关性加权，是难题集的主指标。

Revision ID: 0008_eval_ndcg_variant
Revises: 0007_drop_uq_document_current
"""
from __future__ import annotations

from alembic import op

revision = "0008_eval_ndcg_variant"
down_revision = "0007_drop_uq_document_current"


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'eval_runs' AND column_name = 'variant'
            ) THEN
                ALTER TABLE eval_runs ADD COLUMN variant TEXT NOT NULL DEFAULT 'default';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'eval_runs' AND column_name = 'keyword_ndcg'
            ) THEN
                ALTER TABLE eval_runs
                    ADD COLUMN keyword_ndcg DOUBLE PRECISION NOT NULL DEFAULT 0,
                    ADD COLUMN vector_ndcg  DOUBLE PRECISION NOT NULL DEFAULT 0,
                    ADD COLUMN hybrid_ndcg  DOUBLE PRECISION NOT NULL DEFAULT 0;
            END IF;
        END $$;
        """
    )
    # 对比查询按 (domain, variant) 取最近一次：补对应索引
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE tablename='eval_runs' AND indexname='ix_eval_runs_domain_variant_time'
            ) THEN
                CREATE INDEX ix_eval_runs_domain_variant_time
                    ON eval_runs (domain, variant, run_at DESC);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_eval_runs_domain_variant_time")
    op.execute(
        "ALTER TABLE eval_runs "
        "DROP COLUMN IF EXISTS keyword_ndcg, "
        "DROP COLUMN IF EXISTS vector_ndcg, "
        "DROP COLUMN IF EXISTS hybrid_ndcg, "
        "DROP COLUMN IF EXISTS variant"
    )

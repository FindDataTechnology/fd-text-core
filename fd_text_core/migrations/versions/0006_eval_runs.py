"""0006 检索评测结果表

评测结果落库使「换模型 / 换切块策略 / 改分词配置 / 加垂域」的检索回归可被
度量与对比（change text-retrieval-eval 的 design D3）。本表是**纯派生数据**
（每次跑分重算即可），故 downgrade 直接删表无数据丢失风险。

Revision ID: 0006_eval_runs
Revises: 0005_gov
Create Date: 2026-10-13
"""
from __future__ import annotations

from alembic import op

revision = "0006_eval_runs"
down_revision = "0005_gov"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_runs (
            id                   BIGSERIAL PRIMARY KEY,
            domain               TEXT NOT NULL,
            run_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
            k                    INT  NOT NULL,
            -- 双指纹：数据集与配置。二者相同才可判回归（design D3）
            dataset_fingerprint  TEXT NOT NULL,
            config_fingerprint   TEXT NOT NULL,
            judgment_strength    TEXT,
            n_queries            INT  NOT NULL,
            keyword_hits         INT  NOT NULL,
            keyword_mrr          DOUBLE PRECISION NOT NULL,
            vector_hits          INT  NOT NULL,
            vector_mrr           DOUBLE PRECISION NOT NULL,
            hybrid_hits          INT  NOT NULL,
            hybrid_mrr           DOUBLE PRECISION NOT NULL,
            by_intent            JSONB NOT NULL DEFAULT '{}'::jsonb,
            degraded             JSONB NOT NULL DEFAULT '[]'::jsonb
        )
        """
    )
    # 趋势查询形态固定为「某域按时间取最近几次」，故按 (domain, run_at DESC) 建索引
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_eval_runs_domain_time ON eval_runs (domain, run_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS eval_runs")

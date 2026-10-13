"""同一文档身份的版本退役支持任意次：删全列唯一约束，留部分唯一索引。

为什么删 `uq_document_current`（UNIQUE (domain, source, source_id, status)）：
它的唯一价值「同一身份最多一个 current」已由部分唯一索引
`uq_document_current_only`（WHERE status='current'）表达，两者职责重叠；
而全列约束额外引入「最多一个 superseded」——第三次版本变更即撞键
（gov 垂域实测：v1/v2/v3 依次摄取，第三次退役抛 UniqueViolation、整批失败）。

Revision ID: 0007_drop_uq_document_current
Revises: 0006_eval_runs
"""
from __future__ import annotations

from alembic import op

revision = "0007_drop_uq_document_current"
down_revision = "0006_eval_runs"


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_document_current'
                  AND conrelid = 'corpus_documents'::regclass
            ) THEN
                ALTER TABLE corpus_documents DROP CONSTRAINT uq_document_current;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # 回滚要求：届时库内同一身份不得已有两个 superseded 行（本迁移修掉的正是
    # 该形态）——否则约束重建失败。如实注明而非静默。
    op.create_unique_constraint(
        "uq_document_current",
        "corpus_documents",
        ["domain", "source", "source_id", "status"],
    )

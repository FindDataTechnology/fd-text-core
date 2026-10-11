"""0003 corpus_documents 增 content 列

修补缺陷：原表只存 cold_block_key（冷存指针），而摄取仅在正文 >= 20 万字时
才写冷存 -> 阈值以下文档既无正文也无指针，嵌入 worker 无法切块（实测 4 部
剧本里 3 部因此失败）。

约定：短正文存 content 列；长正文写冷存并存 cold_block_key。二者不得同时为空，
也不重复存大文本。

Revision ID: 0003_doc_content
Revises: 0002_screenplay
Create Date: 2026-10-11
"""
from __future__ import annotations

from alembic import op

revision = "0003_doc_content"
down_revision = "0002_screenplay"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE corpus_documents ADD COLUMN IF NOT EXISTS content TEXT")
    # 入口约束：正文与冷存指针不得同时为空（把该缺口钉死在数据库层）
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_document_has_body'
            ) THEN
                ALTER TABLE corpus_documents
                ADD CONSTRAINT ck_document_has_body
                CHECK (content IS NOT NULL OR cold_block_key IS NOT NULL) NOT VALID;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE corpus_documents DROP CONSTRAINT IF EXISTS ck_document_has_body")
    op.execute("ALTER TABLE corpus_documents DROP COLUMN IF EXISTS content")

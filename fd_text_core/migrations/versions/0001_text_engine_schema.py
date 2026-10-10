"""0001 文本引擎初始 schema

corpus_documents（去重/版本退役/中性状态词） + corpus_chunks（按 domain LIST 分区，
含 tsv/embedding 双通道）+ embed_tasks（claim-ledger 断点账本）+ anchor_embedding
（启动自检锚点）。全部 DDL 走迁移链——库外手工建表由 conformance 门判为漂移违规。

Revision ID: 0001_text_engine
Revises:
Create Date: 2026-10-11
"""
from __future__ import annotations

from alembic import op

revision = "0001_text_engine"
down_revision = None
branch_labels = None
depends_on = None

# 新垂域 = 加一个分区（迁移=运维动作，非代码改动）
INITIAL_DOMAINS = ("papers", "screenplay")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.execute(
        """
        CREATE TABLE corpus_documents (
            id            BIGSERIAL PRIMARY KEY,
            domain        TEXT NOT NULL,
            source        TEXT NOT NULL,
            source_id     TEXT NOT NULL,
            title         TEXT,
            content_hash  TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'current',
            origin        TEXT NOT NULL DEFAULT 'crawl',
            cold_block_key TEXT,
            metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
            provenance    JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_document_current
                UNIQUE (domain, source, source_id, status)
        )
        """
    )
    # 同一身份最多一个 current 行；历史（退役）行可多个。
    # 不可退化为全表唯一 (domain, source, source_id)——那样「旧版退役+新版入库」会撞键。
    op.execute(
        """
        CREATE UNIQUE INDEX uq_document_current_only
            ON corpus_documents (domain, source, source_id)
            WHERE status = 'current'
        """
    )
    op.execute(
        "CREATE INDEX ix_corpus_documents_lookup ON corpus_documents (domain, source, updated_at DESC)"
    )
    op.execute("CREATE INDEX ix_corpus_documents_meta ON corpus_documents USING GIN (metadata)")

    # 分区母表：主键必须含分区键 domain
    op.execute(
        """
        CREATE TABLE corpus_chunks (
            id           BIGSERIAL,
            domain       TEXT NOT NULL,
            document_id  BIGINT NOT NULL REFERENCES corpus_documents(id) ON DELETE CASCADE,
            chunk_no     INT  NOT NULL,
            chunk_text   TEXT NOT NULL,
            char_start   INT  NOT NULL,
            char_end     INT  NOT NULL,
            section_path TEXT,
            tsv          TSVECTOR,
            tsv_config   TEXT,
            embedding    VECTOR(1024),
            embedded_at  TIMESTAMPTZ,
            PRIMARY KEY (id, domain),
            CONSTRAINT uq_chunk UNIQUE (domain, document_id, chunk_no)
        ) PARTITION BY LIST (domain)
        """
    )
    for dom in INITIAL_DOMAINS:
        op.execute(
            f"""
            CREATE TABLE corpus_chunks_{dom}
            PARTITION OF corpus_chunks FOR VALUES IN ('{dom}')
            """
        )
    op.execute("CREATE INDEX ix_chunks_doc ON corpus_chunks (document_id, chunk_no)")
    op.execute(
        """
        CREATE INDEX ix_chunks_tsv ON corpus_chunks USING GIN (tsv)
        """
    )

    op.execute(
        """
        CREATE TABLE embed_tasks (
            id           BIGSERIAL PRIMARY KEY,
            document_id  BIGINT NOT NULL REFERENCES corpus_documents(id) ON DELETE CASCADE,
            domain       TEXT NOT NULL,
            batch_key    TEXT NOT NULL,
            state        TEXT NOT NULL DEFAULT 'pending',
            claimed_at   TIMESTAMPTZ,
            finished_at  TIMESTAMPTZ,
            error        TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_embed_tasks_claim ON embed_tasks (state, created_at) WHERE state = 'pending'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_embed_task ON embed_tasks (document_id, batch_key)"
    )

    op.execute(
        """
        CREATE TABLE anchor_embedding (
            tag       TEXT PRIMARY KEY,
            model     TEXT NOT NULL,
            dim       INT  NOT NULL,
            embedding VECTOR(1024) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS anchor_embedding")
    op.execute("DROP TABLE IF EXISTS embed_tasks")
    op.execute("DROP TABLE IF EXISTS corpus_chunks CASCADE")
    op.execute("DROP TABLE IF EXISTS corpus_documents CASCADE")

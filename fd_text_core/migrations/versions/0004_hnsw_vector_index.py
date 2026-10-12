"""0004 向量列 HNSW 索引

修补缺陷：spec text-indexing-search 要求「vector(1024) cosine + HNSW 索引」，
但 0001 只声明了 `embedding VECTOR(1024)` 列而没有建任何索引——向量检索实际走
ORDER BY <=> 全表扫。集群侧的 hnsw.ef_search / iterative_scan 会话 GUC 只对
**索引扫描**生效，对顺序扫描无效，故「配了调优参数」不等于「有索引」。

为什么索引必须在迁移链内：text-corpus-store 规定 fd_text 的全部 DDL 由迁移链
管理，库外手工建的索引会被质量门的 schema 漂移检测判为违规（自相矛盾）。

分区语义（重要，踩过坑）：
corpus_chunks 是 LIST 分区母表。在母表上建索引时，PG 会为每个**已存在的分区**
自动建同名子索引并 attach 到母表索引下（pg_inherits）。因此：

  - 迁移里**只应建母表索引**，不要对每个分区再显式 CREATE INDEX —— 那会造出
    第二份重复索引（实测每个分区两份 17MB，且母表索引 attach 的是先存在的那份，
    显式建的反而成了无法单独 DROP 的冗余：DROP 它会被母表索引依赖挡住）。
  - 新分区在 ATTACH 时会自动获得母表索引对应的子索引，无需额外迁移。

规模说明（实测 2026-10-12，1024 维合成向量）：
  2,254 行：全表扫 24-64ms，HNSW 强制走索引 1537ms → 规划器正确选择全表扫
  100,000 行：全表扫 1714ms，HNSW 5.55ms（规划器自动选索引）→ 加速约 309 倍
故本索引的收益在万行以上才显现，当前语料规模下规划器不用它是**正确行为**，
不是索引失效。法律回迁（36.5 万点级）后收益实打实。

Revision ID: 0004_hnsw_idx
Revises: 0003_doc_content
Create Date: 2026-10-12
"""
from __future__ import annotations

from alembic import op

revision = "0004_hnsw_idx"
down_revision = "0003_doc_content"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_chunks_embedding_hnsw"


def upgrade() -> None:
    # 只建母表索引：PG 自动为各分区建子索引并 attach，新分区 ATTACH 时亦然。
    # 幂等：IF NOT EXISTS，重跑不报错。
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {INDEX_NAME}
            ON corpus_chunks USING hnsw (embedding vector_cosine_ops)
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")

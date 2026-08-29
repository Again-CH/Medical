"""企业知识向量表（pgvector）

新增 knowledge_documents 表，承载企业知识 / 临床指引等 RAG 语料，embedding 列使用
pgvector 的 ``vector(384)`` 类型，并建 HNSW 余弦索引支撑近邻检索。
需 Postgres 已安装 pgvector 扩展（``CREATE EXTENSION vector``）。

方言适配
--------
本迁移**只在 PostgreSQL 下生效**：sqlite 没有 ``CREATE EXTENSION`` / ``USING hnsw``，
也没有 pgvector 的 ``vector`` 类型。为了让「同一份迁移既能跑 sqlite（本地/测试）
也能跑 postgres（生产）」，非 Postgres 方言下整段跳过建表。

这与 ``alembic/env.py`` 的 ``include_object`` 保持一致——sqlite 下 knowledge_documents
也被排除在 autogenerate 比对之外，因此 ``alembic check`` 不会误报漂移。

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None

# 与 src/embeddings.EMBED_DIM 默认对齐；如需换维度，先重建表与索引。
EMBED_DIM = 384


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        # sqlite / 其他方言：RAG 走 src/kb.py 的内存关键词回退，无需向量表。
        return

    from pgvector.sqlalchemy import Vector

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "knowledge_documents",
        sa.Column("doc_id", sa.String(128), primary_key=True),
        sa.Column("doc_type", sa.String(32), nullable=False, server_default="enterprise"),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("tags", sa.Text(), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(256), nullable=False, server_default=""),
        sa.Column("embedding", Vector(EMBED_DIM), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_knowledge_documents_doc_type", "knowledge_documents", ["doc_type"])
    # HNSW 余弦近邻索引：向量检索走 embedding <=> :q 时由 planner 选用。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_documents_embedding "
        "ON knowledge_documents USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    if not _is_postgres():
        return

    op.execute("DROP INDEX IF EXISTS ix_knowledge_documents_embedding")
    op.drop_index("ix_knowledge_documents_doc_type")
    op.drop_table("knowledge_documents")

"""create rag_documents and rag_chunks

Revision ID: 0002_rag_tables
Revises: 0001_personal_memories
Create Date: 2026-09-24

Personal RAG (Phase 7): document metadata and chunk vectors. No PostgreSQL
extension is required (embeddings are stored as float32 bytes).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_rag_tables"
down_revision: Union[str, None] = "0001_personal_memories"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rag_documents",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("filename", sa.String(260), nullable=False),
        sa.Column("title", sa.String(300), nullable=True),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_location", sa.String(1024), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_rag_documents_source_location", "rag_documents", ["source_location"])
    op.create_index("ix_rag_documents_content_hash", "rag_documents", ["content_hash"])
    op.create_index("ix_rag_documents_status", "rag_documents", ["status"])

    op.create_table(
        "rag_chunks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("document_id", sa.String(32), sa.ForeignKey("rag_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
        sa.Column("embedding_model", sa.String(200), nullable=False),
        sa.Column("embedding_dim", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_rag_chunks_document_id", "rag_chunks", ["document_id"])
    op.create_index("ix_rag_chunks_document_order", "rag_chunks", ["document_id", "chunk_index"])


def downgrade() -> None:
    op.drop_index("ix_rag_chunks_document_order", table_name="rag_chunks")
    op.drop_index("ix_rag_chunks_document_id", table_name="rag_chunks")
    op.drop_table("rag_chunks")
    op.drop_index("ix_rag_documents_status", table_name="rag_documents")
    op.drop_index("ix_rag_documents_content_hash", table_name="rag_documents")
    op.drop_index("ix_rag_documents_source_location", table_name="rag_documents")
    op.drop_table("rag_documents")

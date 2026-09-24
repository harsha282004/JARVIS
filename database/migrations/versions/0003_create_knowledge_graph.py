"""create knowledge graph tables

Revision ID: 0003_knowledge_graph
Revises: 0002_rag_tables
Create Date: 2026-09-24

Personal knowledge graph (Phase 8): kg_entities, kg_relationships, kg_provenance.
Relational; foreign keys tie relationships to entities and provenance to relationships.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_knowledge_graph"
down_revision: Union[str, None] = "0002_rag_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "kg_entities",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("canonical_name", sa.String(200), nullable=False),
        sa.Column("name_key", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("trust", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_kg_entities_identity", "kg_entities", ["entity_type", "name_key", "status"])

    op.create_table(
        "kg_relationships",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("source_entity_id", sa.String(32), sa.ForeignKey("kg_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("relationship_type", sa.String(24), nullable=False),
        sa.Column("target_entity_id", sa.String(32), sa.ForeignKey("kg_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("trust", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_kg_relationships_source_entity_id", "kg_relationships", ["source_entity_id"])
    op.create_index("ix_kg_relationships_target_entity_id", "kg_relationships", ["target_entity_id"])
    op.create_index("ix_kg_relationships_edge", "kg_relationships", ["source_entity_id", "relationship_type", "target_entity_id"])

    op.create_table(
        "kg_provenance",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("relationship_id", sa.String(32), sa.ForeignKey("kg_relationships.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_kind", sa.String(24), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=True),
        sa.Column("source_name", sa.String(260), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("chunk_id", sa.String(64), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("trust", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_kg_provenance_relationship_id", "kg_provenance", ["relationship_id"])
    op.create_index("ix_kg_provenance_source", "kg_provenance", ["source_kind", "source_id"])


def downgrade() -> None:
    op.drop_index("ix_kg_provenance_source", table_name="kg_provenance")
    op.drop_index("ix_kg_provenance_relationship_id", table_name="kg_provenance")
    op.drop_table("kg_provenance")
    op.drop_index("ix_kg_relationships_edge", table_name="kg_relationships")
    op.drop_index("ix_kg_relationships_target_entity_id", table_name="kg_relationships")
    op.drop_index("ix_kg_relationships_source_entity_id", table_name="kg_relationships")
    op.drop_table("kg_relationships")
    op.drop_index("ix_kg_entities_identity", table_name="kg_entities")
    op.drop_table("kg_entities")

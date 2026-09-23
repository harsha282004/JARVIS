"""create personal_memories

Revision ID: 0001_personal_memories
Revises:
Create Date: 2026-09-24

Persistent personal memory (Phase 6). Extracted memories only, never transcripts.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_personal_memories"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "personal_memories",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_norm", sa.String(400), nullable=False),
        sa.Column("slot", sa.String(120), nullable=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("basis", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("superseded_by", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
    )
    op.create_index("ix_personal_memories_content_norm", "personal_memories", ["content_norm"])
    op.create_index("ix_personal_memories_slot", "personal_memories", ["slot"])
    op.create_index("ix_personal_memories_status_type", "personal_memories", ["status", "type"])


def downgrade() -> None:
    op.drop_index("ix_personal_memories_status_type", table_name="personal_memories")
    op.drop_index("ix_personal_memories_slot", table_name="personal_memories")
    op.drop_index("ix_personal_memories_content_norm", table_name="personal_memories")
    op.drop_table("personal_memories")

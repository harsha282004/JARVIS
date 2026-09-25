"""create hub_items (normalized integration data)

Revision ID: 0007_hub
Revises: 0006_proactive
Create Date: 2026-09-25

Phase 18 Integration Hub: one row per normalized external item (email, calendar event, commit, issue, document, message), unique per
(source, kind, source_id) so synchronization is idempotent. Short titles/summaries and small metadata only. Additive: no existing table changes;
downgrade drops only this table.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_hub"
down_revision: Union[str, None] = "0006_proactive"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hub_items",
        sa.Column("id", sa.String(24), primary_key=True),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("source_id", sa.String(200), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("extra", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.String(8), nullable=False, server_default="high"),
        sa.Column("content_hash", sa.String(40), nullable=False),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("source", "kind", "source_id", name="uq_hub_items_source"),
    )
    op.create_index("ix_hub_items_source_kind_ts", "hub_items", ["source", "kind", "source_timestamp"])
    op.create_index("ix_hub_items_retrieved", "hub_items", ["retrieved_at"])


def downgrade() -> None:
    op.drop_index("ix_hub_items_retrieved", table_name="hub_items")
    op.drop_index("ix_hub_items_source_kind_ts", table_name="hub_items")
    op.drop_table("hub_items")

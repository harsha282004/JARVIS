"""create proactive notification history

Revision ID: 0006_proactive
Revises: 0005_events
Create Date: 2026-09-25

Proactive intelligence (Phase 14): a minimal notification history used for de-duplication, cooldown and
"why did you notify me?". `dedupe_key` is unique so exactly one caller can claim (and deliver) a signal.
No message or email bodies are stored (only JARVIS's own short notification sentence). All timestamps are
timezone-aware UTC. Additive only: no existing table is changed; downgrade drops only this table.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_proactive"
down_revision: Union[str, None] = "0005_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "proactive_notifications",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("dedupe_key", sa.String(64), nullable=False, unique=True),
        sa.Column("signal_type", sa.String(32), nullable=False),
        sa.Column("source_type", sa.String(24), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("source_reference", sa.String(200), nullable=False, server_default=""),
        sa.Column("message", sa.String(300), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("urgency", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("channels", sa.String(64), nullable=False, server_default=""),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("relevant_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_proactive_source", "proactive_notifications", ["source_type", "source_id", "delivered_at"])
    op.create_index("ix_proactive_status_delivered", "proactive_notifications", ["status", "delivered_at"])


def downgrade() -> None:
    op.drop_index("ix_proactive_status_delivered", table_name="proactive_notifications")
    op.drop_index("ix_proactive_source", table_name="proactive_notifications")
    op.drop_table("proactive_notifications")

"""create events table

Revision ID: 0005_events
Revises: 0004_tasks_reminders
Create Date: 2026-09-24

Event and deadline intelligence (Phase 11): events. Each row carries its provenance (source type/id/reference)
and an extraction confidence; (source_type, source_id, dedupe_key) is unique so the same source cannot store the
same event twice. An event may reference a task (foreign key, set to NULL if the task is deleted).
All timestamps are timezone-aware and stored as UTC. Additive only: no existing table is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_events"
down_revision: Union[str, None] = "0004_tasks_reminders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("all_day", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("source_reference", sa.String(300), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("dedupe_key", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.UniqueConstraint("source_type", "source_id", "dedupe_key", name="uq_events_source_dedupe"),
    )
    op.create_index("ix_events_status_start_at", "events", ["status", "start_at"])
    op.create_index("ix_events_status_due_at", "events", ["status", "due_at"])
    op.create_index("ix_events_task_id", "events", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_events_task_id", table_name="events")
    op.drop_index("ix_events_status_due_at", table_name="events")
    op.drop_index("ix_events_status_start_at", table_name="events")
    op.drop_table("events")

"""Add durable local task queue tables (task_jobs / task_events)."""

from alembic import op
from sqlalchemy import Column, DateTime, Integer, String, Text, inspect

revision = "0009_task_jobs"
down_revision = "0008_model_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("task_jobs"):
        op.create_table(
            "task_jobs",
            Column("id", String(36), primary_key=True),
            Column("task_name", String(200), nullable=False),
            Column("payload_json", Text, nullable=False, server_default="{}"),
            Column("status", String(16), nullable=False, server_default="PENDING"),
            Column("attempts", Integer, nullable=False, server_default="0"),
            Column("available_at", DateTime(timezone=True), nullable=False),
            Column("lease_expires_at", DateTime(timezone=True), nullable=True),
            Column("last_error", Text, nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
            schema=None,
        )
        op.create_index("ix_task_jobs_status_available", "task_jobs", ["status", "available_at"])
        op.create_index("ix_task_jobs_status_lease", "task_jobs", ["status", "lease_expires_at"])
    if not inspector.has_table("task_events"):
        op.create_table(
            "task_events",
            Column("id", String(36), primary_key=True),
            Column("task_id", String(36), nullable=False),
            Column("event_type", String(32), nullable=False),
            Column("payload_json", Text, nullable=False, server_default="{}"),
            Column("created_at", DateTime(timezone=True), nullable=False),
            schema=None,
        )
        op.create_index("ix_task_events_task_created", "task_events", ["task_id", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if inspector.has_table("task_events"):
        op.drop_table("task_events")
    if inspector.has_table("task_jobs"):
        op.drop_table("task_jobs")

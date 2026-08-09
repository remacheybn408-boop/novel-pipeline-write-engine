"""Persist V3 agent runs and task graph state."""
from alembic import op
from sqlalchemy import Column, DateTime, Integer, String, Text, inspect

revision = "0016_agent_runs"
down_revision = "0015_review_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table("agent_runs"):
        op.create_table("agent_runs", Column("id", String(64), primary_key=True), Column("user_id", String(64), nullable=False), Column("project_id", String(64), nullable=False), Column("goal_hash", String(128), nullable=False), Column("idempotency_key", String(200)), Column("graph_revision", Integer, nullable=False), Column("status", String(32), nullable=False), Column("checkpoint_id", String(64)), Column("budget_limit", Integer, nullable=False, server_default="0"), Column("budget_used", Integer, nullable=False, server_default="0"), Column("event_cursor", Integer, nullable=False, server_default="0"), Column("terminal_reason", Text), Column("policy_version", String(64), nullable=False, server_default="v3-policy-1"), Column("created_at", DateTime(timezone=True), nullable=False), Column("updated_at", DateTime(timezone=True), nullable=False))
    elif "idempotency_key" not in {column["name"] for column in inspect(bind).get_columns("agent_runs")}:
        op.add_column("agent_runs", Column("idempotency_key", String(200)))
    if not inspect(bind).has_table("agent_tasks"):
        op.create_table("agent_tasks", Column("id", String(64), primary_key=True), Column("run_id", String(64), nullable=False), Column("task_key", String(128), nullable=False), Column("role", String(64), nullable=False), Column("status", String(32), nullable=False), Column("attempts", Integer, nullable=False, server_default="0"), Column("depends_on", Text, nullable=False, server_default="[]"), Column("checkpoint_id", String(64)), Column("lease_owner", String(200)), Column("last_error", Text))


def downgrade() -> None:
    op.drop_table("agent_tasks")
    op.drop_table("agent_runs")

"""Persist hashed V3 artifacts, reviews, and scoped memory."""
from alembic import op
from sqlalchemy import Column, String, Text, inspect

revision = "0020_agent_artifacts"
down_revision = "0019_agent_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table("agent_artifacts"):
        op.create_table("agent_artifacts", Column("id", String(64), primary_key=True), Column("run_id", String(64), nullable=False), Column("task_id", String(64)), Column("artifact_type", String(100), nullable=False), Column("sha256", String(128), nullable=False), Column("schema_version", String(64), nullable=False), Column("provenance", Text, nullable=False), Column("preview", Text, nullable=False), Column("payload", Text, nullable=False))
    if not inspect(bind).has_table("agent_reviews"):
        op.create_table("agent_reviews", Column("id", String(64), primary_key=True), Column("run_id", String(64), nullable=False), Column("artifact_id", String(64), nullable=False), Column("reviewer_role", String(64), nullable=False), Column("status", String(32), nullable=False), Column("evidence", Text, nullable=False), Column("conflict_group", String(128)), Column("payload", Text, nullable=False))
    if not inspect(bind).has_table("agent_memories"):
        op.create_table("agent_memories", Column("id", String(64), primary_key=True), Column("project_id", String(64), nullable=False), Column("run_id", String(64), nullable=False), Column("memory_key", String(200), nullable=False), Column("value", Text, nullable=False), Column("source_artifact_id", String(64), nullable=False), Column("status", String(32), nullable=False))


def downgrade() -> None:
    op.drop_table("agent_memories")
    op.drop_table("agent_reviews")
    op.drop_table("agent_artifacts")

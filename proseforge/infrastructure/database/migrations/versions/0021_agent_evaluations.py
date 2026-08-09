"""Persist bounded V3 expansion evaluation evidence."""
from alembic import op
from sqlalchemy import Column, Integer, String, Text, inspect

revision = "0021_agent_evaluations"
down_revision = "0020_agent_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not inspect(op.get_bind()).has_table("agent_evaluations"):
        op.create_table("agent_evaluations", Column("id", String(64), primary_key=True), Column("run_id", String(64), nullable=False), Column("fixture_hash", String(128), nullable=False), Column("score", Integer, nullable=False), Column("payload", Text, nullable=False))


def downgrade() -> None:
    op.drop_table("agent_evaluations")

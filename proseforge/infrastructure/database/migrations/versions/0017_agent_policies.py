"""Persist immutable V3 policy snapshots."""
from alembic import op
from sqlalchemy import Column, String, Text, inspect

revision = "0017_agent_policies"
down_revision = "0016_agent_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not inspect(op.get_bind()).has_table("agent_policy_snapshots"):
        op.create_table("agent_policy_snapshots", Column("id", String(64), primary_key=True), Column("run_id", String(64), nullable=False), Column("policy_version", String(64), nullable=False), Column("policy_hash", String(128), nullable=False), Column("payload", Text, nullable=False))


def downgrade() -> None:
    op.drop_table("agent_policy_snapshots")

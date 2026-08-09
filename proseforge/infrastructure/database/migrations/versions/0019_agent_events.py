"""Persist V3 events and replay cursors."""
from alembic import op
from sqlalchemy import Column, Integer, String, Text, UniqueConstraint, inspect

revision = "0019_agent_events"
down_revision = "0018_agent_graphs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not inspect(op.get_bind()).has_table("agent_events"):
        op.create_table("agent_events", Column("id", String(64), primary_key=True), Column("run_id", String(64), nullable=False), Column("sequence", Integer, nullable=False), Column("event_type", String(100), nullable=False), Column("payload", Text, nullable=False), UniqueConstraint("run_id", "sequence", name="uq_agent_events_run_sequence"))


def downgrade() -> None:
    op.drop_table("agent_events")

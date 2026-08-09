"""Reserve the V3 graph revision boundary."""
from alembic import op
from sqlalchemy import Column, Integer, String, Text, inspect

revision = "0018_agent_graphs"
down_revision = "0017_agent_policies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not inspect(op.get_bind()).has_table("agent_graph_revisions"):
        op.create_table("agent_graph_revisions", Column("id", String(64), primary_key=True), Column("project_id", String(64), nullable=False), Column("revision", Integer, nullable=False), Column("graph_hash", String(128), nullable=False), Column("payload", Text, nullable=False))


def downgrade() -> None:
    op.drop_table("agent_graph_revisions")

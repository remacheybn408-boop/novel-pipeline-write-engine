"""Persist development-only deterministic agent fault injection mode."""

import sqlalchemy as sa
from alembic import op

revision = "0024_agent_fault_mode"
down_revision = "0023_agent_task_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("agent_runs")}
    if "fault_mode" not in columns:
        op.add_column("agent_runs", sa.Column("fault_mode", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "fault_mode")

"""Persist per-task budget for deterministic runtime enforcement."""

import sqlalchemy as sa
from alembic import op

revision = "0023_agent_task_budget"
down_revision = "0022_agent_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("agent_tasks")}
    if "token_budget" not in columns:
        op.add_column("agent_tasks", sa.Column("token_budget", sa.Integer(), nullable=False, server_default="1"))
        if bind.dialect.name == "postgresql":
            # SQLite cannot ALTER COLUMN; per the 0003 convention the
            # sqlite branch keeps the server_default (ORM writes values
            # explicitly, so semantics are unchanged).
            op.alter_column("agent_tasks", "token_budget", server_default=None)


def downgrade() -> None:
    op.drop_column("agent_tasks", "token_budget")

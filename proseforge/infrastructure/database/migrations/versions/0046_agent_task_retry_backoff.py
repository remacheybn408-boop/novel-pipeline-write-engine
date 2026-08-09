"""agent_tasks retry backoff columns.

Retryable provider errors (5xx/429/timeout) get their own counter and a
scheduled retry time so the executor can back off exponentially instead of
hot-looping three instant retries into a provider outage. Plain columns,
PG/SQLite portable, inspector-guarded and idempotent (same style as 0045).
"""

import sqlalchemy as sa
from alembic import op

revision = "0046_agent_task_retry_backoff"
down_revision = "0045_license_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("agent_tasks")}
    if "retryable_attempts" not in columns:
        op.add_column("agent_tasks", sa.Column("retryable_attempts", sa.Integer(), nullable=False, server_default="0"))
    if "next_attempt_at" not in columns:
        op.add_column("agent_tasks", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("agent_tasks")}
    if "next_attempt_at" in columns:
        op.drop_column("agent_tasks", "next_attempt_at")
    if "retryable_attempts" in columns:
        op.drop_column("agent_tasks", "retryable_attempts")

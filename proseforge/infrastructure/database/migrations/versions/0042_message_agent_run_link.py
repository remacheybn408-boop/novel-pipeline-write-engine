"""messages.agent_run_id — link a swarm placeholder message to its agent run.

Nullable VARCHAR(64) + plain index. Deliberately NO foreign key: agent_runs
lives on a different lifecycle (runs may be deleted without touching
messages), and cross-chain FKs complicate the migration graph. Orphaned
values are harmless — the run lookup simply 404s. Inspector-guarded and
idempotent, same style as 0038/0041.
"""

import sqlalchemy as sa
from alembic import op

revision = "0042_message_agent_run_link"
down_revision = "0041_project_cluster_config"
branch_labels = None
depends_on = None

_INDEX = "ix_messages_agent_run_id"


def upgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("messages")}
    if "agent_run_id" not in existing:
        op.add_column("messages", sa.Column("agent_run_id", sa.String(64), nullable=True))
    existing_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("messages")}
    if _INDEX not in existing_indexes:
        op.create_index(_INDEX, "messages", ["agent_run_id"])


def downgrade() -> None:
    bind = op.get_bind()
    existing_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("messages")}
    if _INDEX in existing_indexes:
        op.drop_index(_INDEX, table_name="messages")
    existing = {column["name"] for column in sa.inspect(bind).get_columns("messages")}
    if "agent_run_id" in existing:
        with op.batch_alter_table("messages") as batch:
            batch.drop_column("agent_run_id")

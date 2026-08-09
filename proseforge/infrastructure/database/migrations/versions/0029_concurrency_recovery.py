"""Repair SQLite message idempotency and add durable concurrency cursors."""

import sqlalchemy as sa
from alembic import op

revision = "0029_concurrency_recovery"
down_revision = "0028_runtime_integrity"
branch_labels = None
depends_on = None


def _repair_sqlite_message_constraint(bind) -> None:
    if bind.dialect.name != "sqlite":
        return
    constraints = {item["name"]: tuple(item["column_names"] or ()) for item in sa.inspect(bind).get_unique_constraints("messages")}
    if constraints.get("uq_messages_client_request_id") == ("user_id", "client_request_id"):
        return
    if "uq_messages_client_request_id" not in constraints:
        return
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_constraint("uq_messages_client_request_id", type_="unique")
        batch_op.create_unique_constraint("uq_messages_client_request_id", ["user_id", "client_request_id"])


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    workflow_columns = {column["name"] for column in inspector.get_columns("workflow_runs")}
    if "event_cursor" not in workflow_columns:
        op.add_column("workflow_runs", sa.Column("event_cursor", sa.Integer(), nullable=False, server_default="0"))
        op.execute("""
            UPDATE workflow_runs
            SET event_cursor = COALESCE((
                SELECT MAX(sequence_no)
                FROM workflow_events
                WHERE workflow_events.workflow_run_id = workflow_runs.id
            ), 0)
        """)
        if bind.dialect.name == "postgresql":
            # SQLite cannot ALTER COLUMN; per the 0003 convention the
            # sqlite branch keeps the server_default (ORM writes values
            # explicitly, so semantics are unchanged).
            op.alter_column("workflow_runs", "event_cursor", server_default=None)

    usage_columns = {column["name"] for column in sa.inspect(bind).get_columns("model_usage_records")}
    if "accounted_total_tokens" not in usage_columns:
        op.add_column("model_usage_records", sa.Column("accounted_total_tokens", sa.Integer(), nullable=False, server_default="0"))
        op.execute("UPDATE model_usage_records SET accounted_total_tokens = total_tokens")
        if bind.dialect.name == "postgresql":
            op.alter_column("model_usage_records", "accounted_total_tokens", server_default=None)

    _repair_sqlite_message_constraint(bind)


def downgrade() -> None:
    bind = op.get_bind()
    usage_columns = {column["name"] for column in sa.inspect(bind).get_columns("model_usage_records")}
    if "accounted_total_tokens" in usage_columns:
        op.drop_column("model_usage_records", "accounted_total_tokens")
    workflow_columns = {column["name"] for column in sa.inspect(bind).get_columns("workflow_runs")}
    if "event_cursor" in workflow_columns:
        op.drop_column("workflow_runs", "event_cursor")

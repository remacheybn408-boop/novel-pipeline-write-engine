"""Add durable model usage records and workflow token budget fields."""

from alembic import op
from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text, inspect

revision = "0008_model_usage"
down_revision = "0007_project_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("model_usage_records"):
        op.create_table(
            "model_usage_records",
            Column("id", String(64), primary_key=True),
            Column("user_id", String(64), nullable=False),
            Column("project_id", String(64), nullable=True),
            Column("conversation_id", String(64), nullable=True),
            Column("message_id", String(64), nullable=True),
            Column("workflow_run_id", String(64), nullable=True),
            Column("workflow_step", String(100), nullable=True),
            Column("provider", String(64), nullable=False),
            Column("model_id", String(200), nullable=False),
            Column("provider_request_id", String(200), nullable=True),
            Column("call_id", String(64), nullable=False),
            Column("input_tokens", Integer, nullable=False, server_default="0"),
            Column("output_tokens", Integer, nullable=False, server_default="0"),
            Column("cached_input_tokens", Integer, nullable=False, server_default="0"),
            Column("reasoning_tokens", Integer, nullable=False, server_default="0"),
            Column("total_tokens", Integer, nullable=False, server_default="0"),
            Column("cost_usd", Float, nullable=True),
            Column("usage_source", String(16), nullable=False, server_default="provider"),
            Column("is_final", Boolean, nullable=False, server_default="false"),
            Column("latency_ms", Float, nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("metadata", Text, nullable=False, server_default="{}"),
            schema=None,
        )
        op.create_unique_constraint("uq_model_usage_records_call_id", "model_usage_records", ["call_id"])
        for name, columns in (
            ("ix_model_usage_records_user_created", ["user_id", "created_at"]),
            ("ix_model_usage_records_project_created", ["project_id", "created_at"]),
            ("ix_model_usage_records_conversation_created", ["conversation_id", "created_at"]),
            ("ix_model_usage_records_workflow_created", ["workflow_run_id", "created_at"]),
            ("ix_model_usage_records_provider_model_created", ["provider", "model_id", "created_at"]),
        ):
            op.create_index(name, "model_usage_records", columns)
    columns = {column["name"] for column in inspect(bind).get_columns("workflow_runs")} if inspect(bind).has_table("workflow_runs") else set()
    additions = (("used_tokens", Integer, "0"), ("token_limit", Integer, "0"), ("last_error", Text, None))
    for name, column_type, default in additions:
        if name not in columns:
            op.add_column("workflow_runs", Column(name, column_type, nullable=default is None, server_default=default))


def downgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("workflow_runs"):
        columns = {column["name"] for column in inspect(bind).get_columns("workflow_runs")}
        for name in ("last_error", "token_limit", "used_tokens"):
            if name in columns:
                op.drop_column("workflow_runs", name)
    if inspect(bind).has_table("model_usage_records"):
        op.drop_table("model_usage_records")

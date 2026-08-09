"""Audit log for chat tool calls.

tool_call_log: one row per tool invocation (or cache reuse) from the chat
tool-call rounds (tool system phase 1). Inspector-guarded, idempotent,
reversible (0033 style).
"""

import sqlalchemy as sa
from alembic import op

revision = "0034_tool_call_log"
down_revision = "0033_attachment_message_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "tool_call_log" not in set(sa.inspect(bind).get_table_names()):
        op.create_table(
            "tool_call_log",
            sa.Column("call_id", sa.String(64), primary_key=True),
            sa.Column("message_id", sa.String(64), nullable=False),
            sa.Column("conversation_id", sa.String(64), nullable=False),
            sa.Column("user_id", sa.String(64), nullable=False),
            sa.Column("tool_name", sa.String(64), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("error_class", sa.String(32), nullable=True),
            sa.Column("params_json", sa.Text, nullable=False),
            sa.Column("result_summary", sa.Text, nullable=False, server_default=""),
            sa.Column("result_bytes", sa.Integer, nullable=False, server_default="0"),
            sa.Column("cache_hit", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
            sa.Column("duration_ms", sa.Float, nullable=False, server_default="0"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resource_json", sa.Text, nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_tool_call_log_message_id", "tool_call_log", ["message_id"])
        op.create_index("ix_tool_call_log_conversation_created", "tool_call_log", ["conversation_id", "created_at"])
        op.create_index("ix_tool_call_log_tool_created", "tool_call_log", ["tool_name", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "tool_call_log" in set(inspector.get_table_names()):
        indexes = {item["name"] for item in inspector.get_indexes("tool_call_log")}
        for name in ("ix_tool_call_log_tool_created", "ix_tool_call_log_conversation_created", "ix_tool_call_log_message_id"):
            if name in indexes:
                op.drop_index(name, table_name="tool_call_log")
        op.drop_table("tool_call_log")

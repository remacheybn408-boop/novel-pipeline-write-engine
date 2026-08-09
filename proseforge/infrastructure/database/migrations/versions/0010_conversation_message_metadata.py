"""Add immutable conversation metadata and message edit records."""

from alembic import op
from sqlalchemy import Column, DateTime, Integer, String, Text, inspect

revision = "0010_conversation_message_metadata"
down_revision = "0009_task_jobs"
branch_labels = None
depends_on = None


def _add(table: str, column: Column) -> None:
    inspector = inspect(op.get_bind())
    if column.name not in {item["name"] for item in inspector.get_columns(table)}:
        op.add_column(table, column)


def upgrade() -> None:
    # The initial Alembic table was created with VARCHAR(32), but revision
    # identifiers now exceed that limit (0010_conversation_message_metadata).
    # PostgreSQL enforces the length; widen it before Alembic records this
    # revision. SQLite treats VARCHAR length as advisory and needs no DDL.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(128)")
    _add("conversation_branches", Column("status", String(32), nullable=True))
    _add("conversation_branches", Column("title", String(200), nullable=True))
    _add("conversation_branches", Column("created_by", String(64), nullable=True))
    _add("conversation_branches", Column("created_at", DateTime(timezone=True), nullable=True))
    _add("conversation_branches", Column("archived_at", DateTime(timezone=True), nullable=True))
    _add("conversation_branches", Column("fork_message_id", String(64), nullable=True))
    _add("messages", Column("parent_message_id", String(64), nullable=True))
    _add("messages", Column("generation_attempt", Integer, nullable=True))
    _add("messages", Column("model_snapshot_json", Text, nullable=True))
    _add("messages", Column("reasoning_snapshot_json", Text, nullable=True))
    _add("messages", Column("content_hash", String(128), nullable=True))
    inspector = inspect(op.get_bind())
    if not inspector.has_table("message_edits"):
        op.create_table(
            "message_edits",
            Column("id", String(64), primary_key=True),
            Column("message_id", String(64), nullable=False),
            Column("original_content", Text, nullable=False),
            Column("edited_content", Text, nullable=False),
            Column("created_branch_id", String(64), nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if inspector.has_table("message_edits"):
        op.drop_table("message_edits")
    for table, names in {
        "messages": ("parent_message_id", "generation_attempt", "model_snapshot_json", "reasoning_snapshot_json", "content_hash"),
        "conversation_branches": ("status", "title", "created_by", "created_at", "archived_at", "fork_message_id"),
    }.items():
        existing = {item["name"] for item in inspect(op.get_bind()).get_columns(table)}
        for name in names:
            if name in existing:
                op.drop_column(table, name)

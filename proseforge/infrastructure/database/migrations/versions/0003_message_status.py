"""Add durable assistant message status."""

from alembic import op
from sqlalchemy import Column, String, inspect

revision = "0003_message_status"
down_revision = "0002_remaining_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'COMPLETED'")
        op.execute("ALTER TABLE messages ALTER COLUMN status DROP DEFAULT")
        return
    # SQLite 不支持 ADD COLUMN IF NOT EXISTS / ALTER COLUMN。0001 建表时
    # messages.status 已存在，此分支对全新库为 no-op；仅当列缺失（异常
    # 中断的安装）时补列，保留 server_default 以兼容既有行。
    columns = {column["name"] for column in inspect(bind).get_columns("messages")}
    if "status" not in columns:
        op.add_column(
            "messages",
            Column("status", String(32), nullable=False, server_default="COMPLETED"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS status")
        return
    columns = {column["name"] for column in inspect(bind).get_columns("messages")}
    if "status" in columns:
        op.drop_column("messages", "status")

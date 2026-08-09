"""Track which chat message produced an attachment.

attachments.message_id: nullable String(64) + index. Chat file-download
attachments (```file blocks extracted on completion) record their source
message; pre-existing rows stay NULL. Inspector-guarded, idempotent,
reversible (0032 style).
"""

import sqlalchemy as sa
from alembic import op

revision = "0033_attachment_message_id"
down_revision = "0032_conversation_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "message_id" not in {column["name"] for column in sa.inspect(bind).get_columns("attachments")}:
        op.add_column("attachments", sa.Column("message_id", sa.String(64), nullable=True))
        op.create_index("ix_attachments_message_id", "attachments", ["message_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "message_id" in {column["name"] for column in inspector.get_columns("attachments")}:
        indexes = {item["name"] for item in inspector.get_indexes("attachments")}
        if "ix_attachments_message_id" in indexes:
            op.drop_index("ix_attachments_message_id", table_name="attachments")
        op.drop_column("attachments", "message_id")

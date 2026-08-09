"""Add per-conversation archive flag.

conversations.archived: Boolean NOT NULL DEFAULT false. Existing rows stay
unarchived via the constant server default (SQLite-safe ADD COLUMN).
Inspector-guarded, idempotent, reversible.
"""

import sqlalchemy as sa
from alembic import op

revision = "0032_conversation_archive"
down_revision = "0031_builtin_skill_states"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "archived" not in {column["name"] for column in sa.inspect(bind).get_columns("conversations")}:
        op.add_column("conversations", sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    bind = op.get_bind()
    if "archived" in {column["name"] for column in sa.inspect(bind).get_columns("conversations")}:
        op.drop_column("conversations", "archived")

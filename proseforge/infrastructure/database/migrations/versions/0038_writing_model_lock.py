"""Writing-model lock columns on projects.

Adds writing_model_provider / writing_model_id / model_locked_at /
model_lock_source. The lock is an application-layer rule (first outline
import or first generated chapter version wins); the columns are plain
nullable fields with no DB constraint so a future "switch model + re-read"
flow can rewrite them in place. Inspector-guarded and idempotent.
"""

import sqlalchemy as sa
from alembic import op

revision = "0038_writing_model_lock"
down_revision = "0037_narrative_rag_retrieval"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("writing_model_provider", sa.String(64)),
    ("writing_model_id", sa.String(200)),
    ("model_locked_at", sa.DateTime(timezone=True)),
    ("model_lock_source", sa.String(16)),
)


def upgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("projects")}
    for name, column_type in _COLUMNS:
        if name not in existing:
            op.add_column("projects", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("projects")}
    with op.batch_alter_table("projects") as batch:
        for name, _column_type in _COLUMNS:
            if name in existing:
                batch.drop_column(name)

"""agent_runs.single_model — collapsed-lane runs from normal-mode dispatch.

A run created through the unified dispatcher's normal-mode path collapses
every cluster lane onto the user's selected model. The executor skips
cluster-config resolution for such runs. Nullable Boolean, PG/SQLite
portable, inspector-guarded and idempotent (same style as 0043).
"""

import sqlalchemy as sa
from alembic import op

revision = "0044_run_single_model"
down_revision = "0043_knowledge_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("agent_runs")}
    if "single_model" not in columns:
        op.add_column("agent_runs", sa.Column("single_model", sa.Boolean(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("agent_runs")}
    if "single_model" in columns:
        op.drop_column("agent_runs", "single_model")

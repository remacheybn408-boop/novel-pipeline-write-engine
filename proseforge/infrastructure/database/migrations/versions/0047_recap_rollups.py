"""recap_rollups table (memory-pyramid recap producer).

Hierarchical recaps: volume recaps compress a finished volume's chapter
summaries, book recaps roll volume recaps up incrementally, era recaps
roll up every 10 volumes. Unique (project_id, level, span_start) makes
regeneration an idempotent upsert; ``stale`` is the invalidation flag
flipped by the revision writeback path (later task). Plain columns,
PG/SQLite portable, inspector-guarded and idempotent (same style as
0045/0046).
"""

import sqlalchemy as sa
from alembic import op

revision = "0047_recap_rollups"
down_revision = "0046_agent_task_retry_backoff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "recap_rollups" in set(inspector.get_table_names()):
        return
    op.create_table(
        "recap_rollups",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("span_start", sa.Integer(), nullable=False),
        sa.Column("span_end", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_version_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "level", "span_start", name="uq_recap_rollups_project_level_span_start"),
    )
    op.create_index("ix_recap_rollups_project_id", "recap_rollups", ["project_id"])
    op.create_index("ix_recap_rollups_user_id", "recap_rollups", ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "recap_rollups" in set(inspector.get_table_names()):
        op.drop_table("recap_rollups")

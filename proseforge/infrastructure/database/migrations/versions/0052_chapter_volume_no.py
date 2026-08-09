"""chapters.volume_no column.

Volume as a first-class citizen (Phase 2 跨卷连贯性): volume boundaries used
to live only as regex-parsed text labels inside run goals (rollup_recap
._VOLUME_LABEL_PATTERN), degrading to fixed 10-chapter volumes whenever the
outline lacked labels. The writeback now resolves the chapter's volume from
the goal and persists it on the chapter row, so UI / retrieval / rollup can
address volumes structurally. Plain nullable column, PG/SQLite portable,
inspector-guarded and idempotent (same style as 0049).
"""

import sqlalchemy as sa
from alembic import op

revision = "0052_chapter_volume_no"
down_revision = "0051_clear_retrieval_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("chapters")}
    if "volume_no" not in columns:
        op.add_column("chapters", sa.Column("volume_no", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("chapters")}
    if "volume_no" in columns:
        op.drop_column("chapters", "volume_no")

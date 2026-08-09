"""chapter_versions.paragraph_anchors column.

Pinpoint rewrite (米开朗基罗·改写, 第 11 项) needs a paragraph anchor index
next to each chapter version: paragraph id + content_hash per paragraph, so
review findings can be located to paragraphs and promise/foreshadow evidence
references can be re-validated after a paragraph is rewritten. Plain column,
PG/SQLite portable, inspector-guarded and idempotent (same style as 0046).

NOTE: down_revision 0048_pin_vector_1024 is created by a parallel workstream
(第三期 bge-m3 迁移); this file is merged ahead of it — do not run
`alembic upgrade` until 0048 lands.
"""

import sqlalchemy as sa
from alembic import op

revision = "0049_paragraph_anchors"
down_revision = "0048_pin_vector_1024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("chapter_versions")}
    if "paragraph_anchors" not in columns:
        op.add_column(
            "chapter_versions",
            sa.Column("paragraph_anchors", sa.Text(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("chapter_versions")}
    if "paragraph_anchors" in columns:
        op.drop_column("chapter_versions", "paragraph_anchors")

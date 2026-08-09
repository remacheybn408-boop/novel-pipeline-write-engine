"""Add immutable export manifests.

Revision 0014 intentionally sits between workflow definitions (0013) and
review reports (0015) so the V2 migration chain retains a single head.
"""

from alembic import op
from sqlalchemy import Column, DateTime, Integer, String, Text, inspect

revision = "0014_export_manifests"
down_revision = "0013_workflow_definitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("export_manifests"):
        return
    op.create_table(
        "export_manifests",
        Column("id", String(64), primary_key=True),
        Column("project_id", String(64), nullable=False, index=True),
        Column("user_id", String(64), nullable=False),
        Column("format", String(16), nullable=False),
        Column("template", String(32), nullable=False),
        Column("title", String(500)),
        Column("author", String(200)),
        Column("locale", String(32), nullable=False),
        Column("version_ids_json", Text, nullable=False),
        Column("content_hashes_json", Text, nullable=False),
        Column("file_sha256", String(64), nullable=False),
        Column("byte_size", Integer, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("export_manifests"):
        op.drop_table("export_manifests")

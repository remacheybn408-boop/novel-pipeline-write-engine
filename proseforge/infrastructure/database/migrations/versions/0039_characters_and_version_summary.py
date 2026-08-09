"""Characters table + chapter_versions.summary.

Characters are project-owned (ON DELETE CASCADE per the 0035 convention)
with a (project_id, name) uniqueness rule; auto-extracted rows
(source="auto") are written by the chapter summarizer. chapter_versions
gains a summary column filled by the same job. Inspector-guarded and
idempotent.
"""

import sqlalchemy as sa
from alembic import op

revision = "0039_characters_and_version_summary"
down_revision = "0038_writing_model_lock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "characters" not in set(inspector.get_table_names()):
        op.create_table(
            "characters",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column(
                "project_id", sa.String(64),
                sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True,
            ),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("aliases_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("role", sa.String(64), nullable=False, server_default=""),
            sa.Column("first_seen_chapter", sa.Integer(), nullable=True),
            sa.Column("last_seen_chapter", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(32), nullable=False, server_default="active"),
            sa.Column("source", sa.String(32), nullable=False, server_default="user"),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("project_id", "name", name="uq_characters_project_name"),
        )

    version_columns = {column["name"] for column in sa.inspect(bind).get_columns("chapter_versions")}
    if "summary" not in version_columns:
        op.add_column("chapter_versions", sa.Column("summary", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "summary" in {column["name"] for column in inspector.get_columns("chapter_versions")}:
        with op.batch_alter_table("chapter_versions") as batch:
            batch.drop_column("summary")
    if "characters" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("characters")

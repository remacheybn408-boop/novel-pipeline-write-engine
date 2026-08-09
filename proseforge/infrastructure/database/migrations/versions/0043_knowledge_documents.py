"""knowledge_documents table — reserved project-level knowledge base CRUD.

Project-owned (ON DELETE CASCADE per the 0035 convention), plain columns
only so the table is PG/SQLite portable. Inspector-guarded and idempotent,
same style as 0039.
"""

import sqlalchemy as sa
from alembic import op

revision = "0043_knowledge_documents"
down_revision = "0042_message_agent_run_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "knowledge_documents" not in set(inspector.get_table_names()):
        op.create_table(
            "knowledge_documents",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column(
                "project_id", sa.String(64),
                sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True,
            ),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "knowledge_documents" in set(inspector.get_table_names()):
        op.drop_table("knowledge_documents")

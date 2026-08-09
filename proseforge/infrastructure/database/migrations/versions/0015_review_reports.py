"""Add review reports and durable proposal approval metadata."""

from alembic import op
from sqlalchemy import Column, DateTime, String, Text, inspect

revision = "0015_review_reports"
down_revision = "0014_export_manifests"
branch_labels = None
depends_on = None


_PROPOSAL_COLUMNS = (
    Column("hunks_json", Text, nullable=False, server_default="[]"),
    Column("affected_facts_json", Text, nullable=False, server_default="[]"),
    Column("conflict_status", String(32), nullable=False, server_default="clear"),
    Column("guard_status", String(32), nullable=False, server_default="clear"),
    Column("context_snapshot_id", String(64)),
    Column("idempotency_key", String(200), unique=True),
    Column("decided_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("review_reports"):
        op.create_table(
            "review_reports",
            Column("id", String(64), primary_key=True),
            Column("project_id", String(64), nullable=False, index=True),
            Column("scope", String(32), nullable=False),
            Column("subject_type", String(32), nullable=False),
            Column("subject_id", String(64), nullable=False),
            Column("findings_json", Text, nullable=False),
            Column("scores_json", Text, nullable=False),
            Column("model_snapshot_json", Text, nullable=False),
            Column("context_snapshot_id", String(64)),
            Column("usage_call_id", String(64)),
            Column("status", String(32), nullable=False, server_default="COMPLETED"),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )
    existing = {column["name"] for column in inspector.get_columns("revision_proposals")}
    additions = [column for column in _PROPOSAL_COLUMNS if column.name not in existing]
    if additions:
        with op.batch_alter_table("revision_proposals") as batch:
            for column in additions:
                batch.add_column(column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = {column["name"] for column in inspector.get_columns("revision_proposals")}
    removable = [column.name for column in _PROPOSAL_COLUMNS if column.name in existing]
    if removable:
        with op.batch_alter_table("revision_proposals") as batch:
            for name in removable:
                batch.drop_column(name)
    if inspector.has_table("review_reports"):
        op.drop_table("review_reports")

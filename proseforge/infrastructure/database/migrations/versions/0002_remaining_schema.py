"""Add provider, context, workflow, quality, health, and audit tables."""

from alembic import op

from proseforge.infrastructure.database.base import Base

revision = "0002_remaining_schema"
down_revision = "0001_web_core"
branch_labels = None
depends_on = None

_TABLES = (
    "provider_credentials", "model_profiles", "attachments", "artifacts",
    "context_items", "context_snapshots", "workflow_runs", "workflow_steps",
    "workflow_events", "model_calls", "quality_reports", "health_checks", "audit_logs",
)
# model_catalog is NOT created here: its ORM model now carries an
# owner_id FK -> users.id, and users is only created by 0004_users. On a
# fresh database creating it at 0002 fails with "relation users does not
# exist". 0050_model_catalog_owner creates the table from metadata when it
# is missing (and only adds owner_id to pre-existing tables).


def upgrade() -> None:
    bind = op.get_bind()
    # PostgreSQL-only extensions (pgvector / pg_trgm); SQLite has no
    # extension mechanism and stores embeddings as JSON text instead.
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=False)


def downgrade() -> None:
    for name in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {name} CASCADE")

"""Add workflow lease, checkpoint, and cost guard fields."""

from alembic import op
from sqlalchemy import Column, DateTime, Float, String, Text, inspect

from proseforge.infrastructure.database.base import Base

revision = "0006_workflow_recovery"
down_revision = "0005_outline_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Some early Web v1 databases recorded 0005 while the workflow tables
    # were absent. Repair the durable base tables before adding recovery
    # columns so startup migrations remain safe for those installations.
    for name in ("workflow_runs", "workflow_steps", "workflow_events"):
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)
    columns = {column["name"] for column in inspect(bind).get_columns("workflow_runs")}
    additions = (
        ("lease_owner", String(200), None),
        ("lease_expires_at", DateTime(timezone=True), None),
        ("heartbeat_at", DateTime(timezone=True), None),
        ("checkpoint", Text(), None),
        ("estimated_cost", Float(), "0"),
        ("cost_limit", Float(), "0"),
    )
    for name, column_type, default in additions:
        if name not in columns:
            column = Column(name, column_type, nullable=default is None, server_default=default)
            op.add_column("workflow_runs", column)


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("workflow_runs")}
    for name in ("cost_limit", "estimated_cost", "checkpoint", "heartbeat_at", "lease_expires_at", "lease_owner"):
        if name in columns:
            op.drop_column("workflow_runs", name)

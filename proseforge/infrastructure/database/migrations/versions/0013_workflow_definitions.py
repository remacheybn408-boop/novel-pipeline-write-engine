"""Add versioned workflow definitions and per-node run state."""

from alembic import op
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    inspect,
)

revision = "0013_workflow_definitions"
down_revision = "0012_review_revision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("workflow_definitions"):
        op.create_table(
            "workflow_definitions",
            Column("id", String(64), primary_key=True),
            Column("project_id", String(64), nullable=False, index=True),
            Column("name", String(256), nullable=False),
            Column("revision", Integer, nullable=False),
            Column("definition_json", Text, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
            UniqueConstraint("project_id", "name", "revision", name="uq_workflow_definitions_project_name_revision"),
        )
    if not inspector.has_table("workflow_node_states"):
        op.create_table(
            "workflow_node_states",
            Column("id", String(64), primary_key=True),
            Column("run_id", String(64), nullable=False, index=True),
            Column("node_key", String(128), nullable=False),
            Column("status", String(32), nullable=False),
            Column("checkpoint_json", Text),
            Column("lease_owner", String(200)),
            Column("lease_expires_at", DateTime(timezone=True)),
            Column("retry_count", Integer, nullable=False, server_default="0"),
            Column("reserved_tokens", Integer, nullable=False, server_default="0"),
            Column("used_tokens", Integer, nullable=False, server_default="0"),
            Column("reserved_cost", Float, nullable=False, server_default="0"),
            Column("used_cost", Float, nullable=False, server_default="0"),
            Column("updated_at", DateTime(timezone=True), nullable=False),
            UniqueConstraint("run_id", "node_key", name="uq_workflow_node_states_run_node_key"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if inspector.has_table("workflow_node_states"):
        op.drop_table("workflow_node_states")
    if inspector.has_table("workflow_definitions"):
        op.drop_table("workflow_definitions")

"""Associate agent runs with optional V2 proposal targets."""

import sqlalchemy as sa
from alembic import op

revision = "0022_agent_proposals"
down_revision = "0021_agent_evaluations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("agent_runs")}
    if "chapter_id" not in columns:
        op.add_column("agent_runs", sa.Column("chapter_id", sa.String(64), nullable=True))
    if "base_version_id" not in columns:
        op.add_column("agent_runs", sa.Column("base_version_id", sa.String(64), nullable=True))
    if "proposal_id" not in columns:
        op.add_column("agent_runs", sa.Column("proposal_id", sa.String(64), nullable=True))
    op.create_index("ix_agent_runs_chapter_id", "agent_runs", ["chapter_id"], if_not_exists=True)
    op.create_index("ix_agent_runs_proposal_id", "agent_runs", ["proposal_id"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_agent_runs_proposal_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_chapter_id", table_name="agent_runs")
    op.drop_column("agent_runs", "proposal_id")
    op.drop_column("agent_runs", "base_version_id")
    op.drop_column("agent_runs", "chapter_id")

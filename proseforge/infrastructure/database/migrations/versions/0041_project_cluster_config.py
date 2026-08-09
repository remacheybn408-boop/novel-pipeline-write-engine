"""Per-project cluster (multi-model) config override on projects.

Adds cluster_config_json (Text, nullable). NULL means the project follows
the global cluster preference; when set, the JSON keeps the same stored
shape as the global preference ({mode, write_model, review_model,
revise_model}) and wins over it (project override > global > none).
Inspector-guarded and idempotent, same style as 0038.
"""

import sqlalchemy as sa
from alembic import op

revision = "0041_project_cluster_config"
down_revision = "0040_retrieval_trgm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("projects")}
    if "cluster_config_json" not in existing:
        op.add_column("projects", sa.Column("cluster_config_json", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("projects")}
    if "cluster_config_json" in existing:
        with op.batch_alter_table("projects") as batch:
            batch.drop_column("cluster_config_json")

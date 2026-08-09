"""Add runtime fencing, credential uniqueness, and session invalidation.

The credential cleanup preserves the historical repository read rule (highest
id wins) before adding the database constraint.
"""

import sqlalchemy as sa
from alembic import op

revision = "0028_runtime_integrity"
down_revision = "0027_agent_run_goal_and_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "session_version" not in user_columns:
        op.add_column("users", sa.Column("session_version", sa.Integer(), nullable=False, server_default="1"))
        if bind.dialect.name == "postgresql":
            # SQLite cannot ALTER COLUMN; per the 0003 convention the
            # sqlite branch keeps the server_default (ORM writes values
            # explicitly, so semantics are unchanged).
            op.alter_column("users", "session_version", server_default=None)

    task_columns = {column["name"] for column in inspector.get_columns("task_jobs")}
    if "lease_token" not in task_columns:
        op.add_column("task_jobs", sa.Column("lease_token", sa.Integer(), nullable=False, server_default="0"))
        if bind.dialect.name == "postgresql":
            op.alter_column("task_jobs", "lease_token", server_default=None)

    duplicates = bind.execute(sa.text("""
        SELECT user_id, provider
        FROM provider_credentials
        GROUP BY user_id, provider
        HAVING COUNT(*) > 1
    """)).fetchall()
    for user_id, provider in duplicates:
        ids = [row[0] for row in bind.execute(
            sa.text("SELECT id FROM provider_credentials WHERE user_id = :user_id AND provider = :provider ORDER BY id DESC"),
            {"user_id": user_id, "provider": provider},
        ).fetchall()]
        for obsolete_id in ids[1:]:
            bind.execute(sa.text("DELETE FROM provider_credentials WHERE id = :id"), {"id": obsolete_id})

    inspector = sa.inspect(bind)
    constraints = {constraint["name"] for constraint in inspector.get_unique_constraints("provider_credentials")}
    if "uq_provider_credentials_user_provider" not in constraints:
        with op.batch_alter_table("provider_credentials") as batch_op:
            batch_op.create_unique_constraint("uq_provider_credentials_user_provider", ["user_id", "provider"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    constraints = {constraint["name"] for constraint in inspector.get_unique_constraints("provider_credentials")}
    if "uq_provider_credentials_user_provider" in constraints:
        with op.batch_alter_table("provider_credentials") as batch_op:
            batch_op.drop_constraint("uq_provider_credentials_user_provider", type_="unique")
    task_columns = {column["name"] for column in inspector.get_columns("task_jobs")}
    if "lease_token" in task_columns:
        op.drop_column("task_jobs", "lease_token")
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "session_version" in user_columns:
        op.drop_column("users", "session_version")

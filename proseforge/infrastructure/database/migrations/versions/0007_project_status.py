"""Add the project lifecycle status column.

恢复说明（V15-003）：v1.1 中间态曾删除本文件，导致 0008_model_usage 的
down_revision 悬空、alembic 图断裂。本次恢复保持 revision/down_revision
与原始版本一致，PostgreSQL 行为与原 DO block 完全不变；同时为 native
SQLite 增加方言分支（SQLite 不支持 ADD COLUMN IF NOT EXISTS / ALTER
COLUMN，改用 PRAGMA table_info 检查后执行 plain ALTER TABLE ADD COLUMN）。
"""

from alembic import op
from sqlalchemy import Column, String, inspect

revision = "0007_project_status"
down_revision = "0006_workflow_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "DO $$ BEGIN "
            "IF to_regclass('public.projects') IS NOT NULL THEN "
            "ALTER TABLE projects ADD COLUMN IF NOT EXISTS status VARCHAR(32); "
            "UPDATE projects SET status = 'ACTIVE' WHERE status IS NULL; "
            "ALTER TABLE projects ALTER COLUMN status SET NOT NULL; "
            "END IF; END $$"
        )
        return
    inspector = inspect(bind)
    if not inspector.has_table("projects"):
        return
    columns = {column["name"] for column in inspector.get_columns("projects")}
    if "status" not in columns:
        op.add_column(
            "projects",
            Column("status", String(32), nullable=False, server_default="ACTIVE"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS status")
        return
    inspector = inspect(bind)
    if not inspector.has_table("projects"):
        return
    columns = {column["name"] for column in inspector.get_columns("projects")}
    if "status" in columns:
        op.drop_column("projects", "status")

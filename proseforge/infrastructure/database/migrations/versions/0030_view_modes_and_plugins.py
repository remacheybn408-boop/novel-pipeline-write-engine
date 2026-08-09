"""Add work/chat project modes, conversation timestamps, and plugin tables.

- projects.mode：'work'|'chat'，存量归 'work'（server_default 兜底）。
  CHECK 约束经 batch_alter_table 添加：SQLite 走表重建，PostgreSQL 走普通
  ALTER，双方言同一路径。
- conversations.created_at：NOT NULL DEFAULT now()，供会话列表倒序。
- user_skills / user_mcp_servers：每用户插件配置（Skill 与 MCP server），
  (user_id, name) 唯一；MCP headers 密文落库（路由层 CredentialCipher 加密）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0030_view_modes_and_plugins"
down_revision = "0029_concurrency_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "mode" not in {column["name"] for column in inspector.get_columns("projects")}:
        with op.batch_alter_table("projects") as batch_op:
            batch_op.add_column(sa.Column("mode", sa.String(8), nullable=False, server_default="work"))
            batch_op.create_check_constraint("ck_projects_mode", "mode IN ('work', 'chat')")

    if "created_at" not in {column["name"] for column in sa.inspect(bind).get_columns("conversations")}:
        if bind.dialect.name == "sqlite":
            # SQLite 禁止 ADD COLUMN 带非常量默认值（CURRENT_TIMESTAMP）：
            # 先加可空列再回填；ORM 创建路径始终显式写值，语义不受影响。
            op.add_column("conversations", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
            op.execute("UPDATE conversations SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")
        else:
            op.add_column("conversations", sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))

    if not inspector.has_table("user_skills"):
        op.create_table(
            "user_skills",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("user_id", "name", name="uq_user_skills_user_name"),
        )
        op.create_index("ix_user_skills_user_id", "user_skills", ["user_id"])

    if not inspector.has_table("user_mcp_servers"):
        op.create_table(
            "user_mcp_servers",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("transport", sa.String(16), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("encrypted_headers", sa.Text(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("user_id", "name", name="uq_user_mcp_servers_user_name"),
        )
        op.create_index("ix_user_mcp_servers_user_id", "user_mcp_servers", ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("user_mcp_servers"):
        op.drop_table("user_mcp_servers")
    if inspector.has_table("user_skills"):
        op.drop_table("user_skills")

    if "created_at" in {column["name"] for column in sa.inspect(bind).get_columns("conversations")}:
        op.drop_column("conversations", "created_at")

    if "mode" in {column["name"] for column in sa.inspect(bind).get_columns("projects")}:
        with op.batch_alter_table("projects") as batch_op:
            batch_op.drop_constraint("ck_projects_mode", type_="check")
            batch_op.drop_column("mode")

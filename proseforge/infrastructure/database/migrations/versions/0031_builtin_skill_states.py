"""Per-user enable states for built-in skills.

user_builtin_skill_states 记录用户对内置 skill（packs/skills 目录加载）
的启用开关；(user_id, skill_key) 唯一。**无状态行时默认 disabled**——
避免 11 个内置 skill 全量注入撑爆聊天上下文。双数据库兼容、幂等、可逆。
"""

import sqlalchemy as sa
from alembic import op

revision = "0031_builtin_skill_states"
down_revision = "0030_view_modes_and_plugins"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("user_builtin_skill_states"):
        op.create_table(
            "user_builtin_skill_states",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("skill_key", sa.String(200), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("user_id", "skill_key", name="uq_user_builtin_skill_states_user_key"),
        )
        op.create_index("ix_user_builtin_skill_states_user_id", "user_builtin_skill_states", ["user_id"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("user_builtin_skill_states"):
        op.drop_table("user_builtin_skill_states")

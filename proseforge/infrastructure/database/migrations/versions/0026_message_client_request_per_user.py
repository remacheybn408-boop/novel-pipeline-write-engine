"""Scope message idempotency keys per user.

client_request_id 的唯一域从全局收窄为 (user_id, client_request_id)：
messages 表新增 user_id 列并从 projects.owner_id 回填存量数据，随后
DROP 旧的全局唯一约束、ADD 复合唯一约束。约束替换只在 PostgreSQL 执行
（SQLite 无法就地改约束，新库由 metadata 建表即带新约束）。
"""

from alembic import op
from sqlalchemy import Column, String, inspect

revision = "0026_message_client_request_per_user"
down_revision = "0025_agent_constraints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "user_id" not in {item["name"] for item in inspector.get_columns("messages")}:
        op.add_column("messages", Column("user_id", String(128), nullable=True))
        op.create_index("ix_messages_user_id", "messages", ["user_id"])
    # 存量回填：message → branch → conversation → project.owner_id。
    op.execute(
        """
        UPDATE messages
        SET user_id = projects.owner_id
        FROM conversation_branches, conversations, projects
        WHERE messages.branch_id = conversation_branches.id
          AND conversation_branches.conversation_id = conversations.id
          AND conversations.project_id = projects.id
          AND messages.user_id IS NULL
        """
        if bind.dialect.name == "postgresql"
        else """
        UPDATE messages
        SET user_id = (
            SELECT projects.owner_id
            FROM conversation_branches, conversations, projects
            WHERE conversation_branches.id = messages.branch_id
              AND conversations.id = conversation_branches.conversation_id
              AND projects.id = conversations.project_id
        )
        WHERE messages.user_id IS NULL
        """
    )
    constraints = {item["name"] for item in inspect(bind).get_unique_constraints("messages")}
    if "uq_messages_client_request_id" in constraints:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("messages") as batch_op:
                batch_op.drop_constraint("uq_messages_client_request_id", type_="unique")
                batch_op.create_unique_constraint("uq_messages_client_request_id", ["user_id", "client_request_id"])
        else:
            op.drop_constraint("uq_messages_client_request_id", "messages", type_="unique")
            op.create_unique_constraint("uq_messages_client_request_id", "messages", ["user_id", "client_request_id"])


def downgrade() -> None:
    bind = op.get_bind()
    constraints = {item["name"] for item in inspect(bind).get_unique_constraints("messages")}
    if "uq_messages_client_request_id" in constraints:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("messages") as batch_op:
                batch_op.drop_constraint("uq_messages_client_request_id", type_="unique")
                batch_op.create_unique_constraint("uq_messages_client_request_id", ["client_request_id"])
        else:
            op.drop_constraint("uq_messages_client_request_id", "messages", type_="unique")
            op.create_unique_constraint("uq_messages_client_request_id", "messages", ["client_request_id"])
    inspector = inspect(bind)
    columns = {item["name"] for item in inspector.get_columns("messages")}
    if "user_id" in columns:
        indexes = {item["name"] for item in inspector.get_indexes("messages")}
        if "ix_messages_user_id" in indexes:
            op.drop_index("ix_messages_user_id", table_name="messages")
        op.drop_column("messages", "user_id")

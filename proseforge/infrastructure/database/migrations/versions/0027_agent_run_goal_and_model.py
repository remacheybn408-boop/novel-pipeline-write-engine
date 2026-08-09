"""Store goal text and per-run provider/model on agent runs.

agent_runs 新增三列（均 nullable，无需回填）：
- ``goal``：目标原文。此前只存 goal_hash，prompt 注入只能用 hash 前缀，
  LLM 看不到真实目标；新 run 起写入原文，存量 run 由 prompt 组装处回退
  goal_hash 前缀。
- ``provider`` / ``model``：run 级模型选择。NULL → executor 缺省
  openai / gpt-4.1-mini（与此前硬编码一致）。
"""

from alembic import op
from sqlalchemy import Column, String, Text, inspect

revision = "0027_agent_run_goal_and_model"
down_revision = "0026_message_client_request_per_user"
branch_labels = None
depends_on = None


def _add(table: str, column: Column) -> None:
    inspector = inspect(op.get_bind())
    if column.name not in {item["name"] for item in inspector.get_columns(table)}:
        op.add_column(table, column)


def upgrade() -> None:
    _add("agent_runs", Column("goal", Text, nullable=True))
    _add("agent_runs", Column("provider", String(64), nullable=True))
    _add("agent_runs", Column("model", String(200), nullable=True))


def downgrade() -> None:
    existing = {item["name"] for item in inspect(op.get_bind()).get_columns("agent_runs")}
    for name in ("goal", "provider", "model"):
        if name in existing:
            op.drop_column("agent_runs", name)

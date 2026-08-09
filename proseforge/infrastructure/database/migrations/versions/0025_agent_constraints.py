"""V3 agent data-integrity constraints and supporting indexes.

- agent_runs.idempotency_key 部分唯一索引（并发建跑防重，仅非 NULL 参与）。
- agent_tasks(run_id, task_key) 唯一约束：模型早已声明，0016 漏建，补齐漂移。
- agent_policy_snapshots.signature：策略快照 HMAC 签名列（可空，兼容存量回填）。
- agent_tasks.lease_expires_at：执行器租约过期时间（lease_owner 0016 已有则跳过）。
- agent_runs.checkpoint_id 加宽到 512：执行器写入 graph/done/cursor/exec 组合游标，64 放不下。
- 补齐 0016 未建的常用查询索引。
"""

import sqlalchemy as sa
from alembic import op

revision = "0025_agent_constraints"
down_revision = "0024_agent_fault_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    run_indexes = {index["name"] for index in inspector.get_indexes("agent_runs")}
    if "uq_agent_runs_idempotency_key" not in run_indexes:
        op.create_index(
            "uq_agent_runs_idempotency_key",
            "agent_runs",
            ["idempotency_key"],
            unique=True,
            if_not_exists=True,
            postgresql_where=sa.text("idempotency_key IS NOT NULL"),
            sqlite_where=sa.text("idempotency_key IS NOT NULL"),
        )
    for name, column in (("ix_agent_runs_user_id", "user_id"), ("ix_agent_runs_project_id", "project_id")):
        if name not in run_indexes:
            op.create_index(name, "agent_runs", [column], if_not_exists=True)

    task_columns = {column["name"] for column in inspector.get_columns("agent_tasks")}
    if "lease_owner" not in task_columns:
        op.add_column("agent_tasks", sa.Column("lease_owner", sa.String(200), nullable=True))
    if "lease_expires_at" not in task_columns:
        op.add_column("agent_tasks", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))

    inspector = sa.inspect(bind)
    task_indexes = {index["name"] for index in inspector.get_indexes("agent_tasks")}
    if "ix_agent_tasks_run_id" not in task_indexes:
        op.create_index("ix_agent_tasks_run_id", "agent_tasks", ["run_id"], if_not_exists=True)
    task_constraints = {constraint["name"] for constraint in inspector.get_unique_constraints("agent_tasks")}
    if "uq_agent_tasks_run_key" not in task_constraints and "uq_agent_tasks_run_key" not in task_indexes:
        with op.batch_alter_table("agent_tasks") as batch_op:
            batch_op.create_unique_constraint("uq_agent_tasks_run_key", ["run_id", "task_key"])

    snapshot_columns = {column["name"] for column in sa.inspect(bind).get_columns("agent_policy_snapshots")}
    if "signature" not in snapshot_columns:
        op.add_column("agent_policy_snapshots", sa.Column("signature", sa.String(128), nullable=True))

    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.alter_column("checkpoint_id", type_=sa.String(512), existing_nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.alter_column("checkpoint_id", type_=sa.String(64), existing_nullable=True)
    op.drop_column("agent_policy_snapshots", "signature")
    with op.batch_alter_table("agent_tasks") as batch_op:
        batch_op.drop_constraint("uq_agent_tasks_run_key", type_="unique")
    op.drop_index("ix_agent_tasks_run_id", table_name="agent_tasks")
    op.drop_column("agent_tasks", "lease_expires_at")
    op.drop_index("ix_agent_runs_project_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_user_id", table_name="agent_runs")
    op.drop_index("uq_agent_runs_idempotency_key", table_name="agent_runs")

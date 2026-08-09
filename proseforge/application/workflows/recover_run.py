from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from proseforge.infrastructure.database.models.remaining import WorkflowRunModel
from proseforge.infrastructure.database.models.workflow_v2 import WorkflowNodeStateModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork


async def recover_expired_workflow_nodes(uow: SqlAlchemyUnitOfWork) -> int:
    if uow.session is None:
        raise RuntimeError("unit of work is not active")
    now = datetime.now(UTC)
    nodes = list((await uow.session.scalars(select(WorkflowNodeStateModel).where(WorkflowNodeStateModel.status == "RUNNING", WorkflowNodeStateModel.lease_expires_at <= now).with_for_update())).all())
    recovered_runs: set[str] = set()
    for node in nodes:
        node.status = "PENDING"
        node.lease_owner = None
        node.lease_expires_at = None
        node.retry_count += 1
        node.updated_at = now
        recovered_runs.add(node.run_id)
    for run_id in recovered_runs:
        run = await uow.session.get(WorkflowRunModel, run_id)
        if run is not None and run.status == "RUNNING":
            run.status = "RECOVERING"
            run.lease_owner = None
            run.lease_expires_at = None
            await uow.workflows.append_event(run.id, "run.recovering", {"status": "RECOVERING", "reason": "node_lease_expired"})
            run.status = "QUEUED"
            await uow.workflows.append_event(run.id, "run.recovered", {"status": "QUEUED"})
    # 兜底：被 v1 恢复通道（repository.recover_expired 扫描全部 run）置为
    # RECOVERING 的 v2 definition run，一并重排回 QUEUED 交给执行器续跑。
    stuck = list((await uow.session.scalars(select(WorkflowRunModel).where(WorkflowRunModel.status == "RECOVERING", WorkflowRunModel.workflow_type == "DEFINITION"))).all())
    for run in stuck:
        run.status = "QUEUED"
        run.lease_owner = None
        run.lease_expires_at = None
        await uow.workflows.append_event(run.id, "run.recovered", {"status": "QUEUED"})
    await uow.session.flush()
    return len(nodes)


async def queued_definition_run_ids(uow: SqlAlchemyUnitOfWork) -> list[str]:
    """QUEUED 状态的 v2 definition run（恢复重排或入队失败后的回补对象）。"""
    if uow.session is None:
        raise RuntimeError("unit of work is not active")
    rows = await uow.session.scalars(select(WorkflowRunModel.id).where(WorkflowRunModel.status == "QUEUED", WorkflowRunModel.workflow_type == "DEFINITION"))
    return [str(row) for row in rows]

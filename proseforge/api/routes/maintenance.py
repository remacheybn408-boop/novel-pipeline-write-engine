from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from proseforge.api.dependencies import current_user, require_admin, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.application.workflows.recover_run import (
    queued_definition_run_ids,
    recover_expired_workflow_nodes,
)
from proseforge.infrastructure.blob.local import LocalBlobStore
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.operations.maintenance import (
    record_maintenance_audit,
    verify_attachment_blobs,
)
from proseforge.workflows.v2_tasks import EXECUTE_V2_RUN_TASK

router = APIRouter(prefix="/api/v1/maintenance", tags=["maintenance"])


async def _requeue_v2_runs(request: Request, run_ids: list[str]) -> int:
    """为 QUEUED 的 v2 run 重排执行器；broker 不可用时停止，run 保持 QUEUED
    等下一轮恢复（执行器的 run 租约对重复入队去重）。"""
    enqueued = 0
    for run_id in run_ids:
        try:
            await request.app.state.queue.enqueue(EXECUTE_V2_RUN_TASK, {"run_id": run_id})
        except Exception:
            break
        enqueued += 1
    return enqueued


@router.post("/workflows/recover-expired")
async def recover_expired_workflows(
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    require_admin(user)
    async with uow:
        recovered = await uow.workflows.recover_expired()
        record_maintenance_audit(uow, user.id, "recover_expired_workflows", {"recovered": recovered})
        await uow.commit()
    return {"status": "ok", "recovered": recovered}


@router.post("/workflow-runs/recover-expired")
async def recover_expired_v2_workflow_runs(
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    """v2 版恢复入口（与 v1 端点并列）：过期节点租约 → PENDING、run → QUEUED，
    然后为每个待执行 run 补入队执行器。"""
    require_admin(user)
    async with uow:
        recovered_nodes = await recover_expired_workflow_nodes(uow)
        queued = await queued_definition_run_ids(uow)
        record_maintenance_audit(uow, user.id, "recover_expired_v2_workflow_runs", {"recovered_nodes": recovered_nodes})
        await uow.commit()
    enqueued = await _requeue_v2_runs(request, queued)
    return {"status": "ok", "recovered_nodes": recovered_nodes, "enqueued": enqueued}


@router.post("/blobs/verify")
async def verify_blobs(
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    require_admin(user)
    async with uow:
        result = await verify_attachment_blobs(uow, LocalBlobStore(request.app.state.settings.blob_root))
        record_maintenance_audit(uow, user.id, "verify_blobs", result)
        await uow.commit()
    return {"status": "ok", **result}

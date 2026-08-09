from __future__ import annotations

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from proseforge.api.dependencies import current_user, unit_of_work
from proseforge.api.sse.encoder import encode_sse
from proseforge.application.auth.service import AuthUser
from proseforge.application.workflows.run_service import (
    TERMINAL_STATUSES,
    WorkflowRunService,
)
from proseforge.infrastructure.database.models.remaining import (
    WorkflowEventModel,
    WorkflowRunModel,
)
from proseforge.infrastructure.database.session import close_session
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v2", tags=["workflow-studio"])


@router.get("/workflow-runs/{run_id}/events")
async def workflow_run_events(run_id: str, request: Request, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]):
    try:
        cursor = max(0, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        cursor = 0
    async with uow:
        if await WorkflowRunService(uow).get_owned(run_id, user.id) is None:
            raise HTTPException(status_code=404, detail="workflow run not found")

    async def body():
        nonlocal cursor
        idle_seconds = 0
        while not await request.is_disconnected():
            session = request.app.state.session_factory()
            try:
                rows = list((await session.scalars(select(WorkflowEventModel).where(WorkflowEventModel.workflow_run_id == run_id, WorkflowEventModel.sequence_no > cursor).order_by(WorkflowEventModel.sequence_no))).all())
                run_status = await session.scalar(select(WorkflowRunModel.status).where(WorkflowRunModel.id == run_id))
            finally:
                # Client disconnect cancels the task mid-poll; close resiliently
                # so the pooled connection is checked in instead of GC-reaped.
                await close_session(session)
            if rows:
                idle_seconds = 0
                for row in rows:
                    cursor = row.sequence_no
                    yield encode_sse(event_id=str(row.sequence_no), event=row.event_type, data=json.loads(row.payload))
                if run_status in TERMINAL_STATUSES:
                    return
            else:
                idle_seconds += 1
                if idle_seconds >= 15:
                    idle_seconds = 0
                    yield ": heartbeat\n\n"
                if run_status in TERMINAL_STATUSES:
                    return
            await asyncio.sleep(1)

    return StreamingResponse(body(), media_type="text/event-stream", headers={"cache-control": "no-cache", "x-accel-buffering": "no"})

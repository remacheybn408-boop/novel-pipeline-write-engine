from __future__ import annotations

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from proseforge.api.dependencies import current_user, require_work_project, unit_of_work
from proseforge.api.sse.encoder import encode_sse
from proseforge.application.auth.service import AuthUser
from proseforge.application.workflows.control import (
    WorkflowControlService,
    decode_checkpoint,
    workflow_command,
)
from proseforge.domain.chapter.entity import Chapter
from proseforge.infrastructure.database.models.remaining import (
    WorkflowEventModel,
    WorkflowRunModel,
)
from proseforge.infrastructure.database.session import close_session
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1", tags=["workflows"])


class NovelWorkflowRequest(BaseModel):
    chapter_numbers: list[int] = Field(default_factory=list, min_length=1)
    cost_limit: float = Field(default=0.0, ge=0)
    provider: str = "openai"
    model: str = "gpt-4.1-mini"
    editor_model: str | None = None
    token_limit: int = Field(default=0, ge=0)


def _response(run) -> dict[str, object]:
    checkpoint = decode_checkpoint(run.checkpoint)
    return {
        "id": run.id, "project_id": run.project_id, "workflow_type": run.workflow_type, "status": run.status,
        "estimated_cost": float(getattr(run, "estimated_cost", 0) or 0), "cost_limit": float(getattr(run, "cost_limit", 0) or 0),
        "used_tokens": int(getattr(run, "used_tokens", 0) or 0), "token_limit": int(getattr(run, "token_limit", 0) or 0),
        "checkpoint": checkpoint.get("phase", run.checkpoint), "last_error": getattr(run, "last_error", None),
    }


@router.post("/projects/{project_id}/workflows/novel", status_code=status.HTTP_201_CREATED)
async def create_workflow(
    project_id: str,
    payload: NovelWorkflowRequest,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        project = await require_work_project(uow, user.id, project_id)
        existing = {chapter.chapter_no for chapter in await uow.chapters.list_owned(project.id, user.id)}
        for chapter_no in payload.chapter_numbers:
            if chapter_no not in existing:
                await uow.chapters.add(Chapter.create(project_id=project.id, chapter_no=chapter_no, title=f"Chapter {chapter_no}"))
        return_value = await uow.workflows.create(project.id, "NOVEL", cost_limit=payload.cost_limit, token_limit=payload.token_limit)
        command = workflow_command(user_id=user.id, chapter_numbers=payload.chapter_numbers, provider=payload.provider, model=payload.model, editor_model=payload.editor_model or payload.model)
        await uow.workflows.set_command(return_value, command)
        await uow.commit()
        try:
            task_id = await request.app.state.queue.enqueue("proseforge.workflows.generate_novel", {"workflow_id": return_value.id, **command})
        except Exception as exc:
            # The run was already persisted; never strand it in QUEUED when
            # the broker rejects the first dispatch.
            return_value.status = "FAILED"
            return_value.last_error = f"enqueue failed: {type(exc).__name__}"
            await uow.workflows.append_event(return_value.id, "FAILED", {"status": "FAILED", "reason": "enqueue_failed"})
            await uow.commit()
            raise HTTPException(status_code=503, detail="workflow queue is unavailable") from exc
        await uow.workflows.set_task(return_value, task_id)
        await uow.commit()
        result = _response(return_value)
        result["task_id"] = task_id
        return result


@router.get("/workflows/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        run = await uow.workflows.get_owned(workflow_id, user.id)
        if run is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        return _response(run)


@router.post("/workflows/{workflow_id}/{action}")
async def control_workflow(
    workflow_id: str,
    action: str,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=1)] = None,
) -> dict[str, object]:
    async with uow:
        try:
            result = await WorkflowControlService(uow, request.app.state.queue).execute(workflow_id, user.id, action, idempotency_key=idempotency_key)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        response = _response(result.run)
        response["idempotent_replay"] = result.idempotent_replay
        if result.task_id:
            response["task_id"] = result.task_id
        return response


@router.get("/workflows/{workflow_id}/events")
async def workflow_events(
    workflow_id: str,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
):
    last_event_id = request.headers.get("last-event-id")
    try:
        after = max(0, int(last_event_id or "0"))
    except ValueError:
        after = 0
    async with uow:
        if await uow.workflows.get_owned(workflow_id, user.id) is None:
            raise HTTPException(status_code=404, detail="workflow not found")
    async def body():
        cursor = after
        idle_seconds = 0
        terminal = {"COMPLETED", "FAILED", "CANCELLED"}
        while not await request.is_disconnected():
            session = request.app.state.session_factory()
            try:
                rows = list((await session.scalars(select(WorkflowEventModel).where(WorkflowEventModel.workflow_run_id == workflow_id, WorkflowEventModel.sequence_no > cursor).order_by(WorkflowEventModel.sequence_no))).all())
                run_status = await session.scalar(select(WorkflowRunModel.status).where(WorkflowRunModel.id == workflow_id))
            finally:
                # Client disconnect cancels the task mid-poll; close resiliently
                # so the pooled connection is checked in instead of GC-reaped.
                await close_session(session)
            if run_status is None:
                # The run was deleted mid-stream; emit a terminal event so the
                # client can close instead of polling until it disconnects.
                yield encode_sse(event_id=str(cursor), event="run.deleted", data={"status": "DELETED"})
                return
            if rows:
                idle_seconds = 0
                for row in rows:
                    cursor = row.sequence_no
                    yield encode_sse(event_id=str(row.sequence_no), event=row.event_type, data=json.loads(row.payload))
                if run_status in terminal:
                    return
            else:
                idle_seconds += 1
                if idle_seconds >= 15:
                    idle_seconds = 0
                    yield ": heartbeat\n\n"
                if run_status in terminal:
                    return
            await asyncio.sleep(1)

    return StreamingResponse(body(), media_type="text/event-stream", headers={"cache-control": "no-cache", "x-accel-buffering": "no"})

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from proseforge.api.dependencies import current_user, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.application.workflows.definition_service import (
    WorkflowDefinitionService,
    definition_response,
)
from proseforge.application.workflows.run_service import (
    WorkflowRunService,
    node_response,
    run_response,
)
from proseforge.infrastructure.database.models.remaining import WorkflowRunModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.workflows.v2_tasks import EXECUTE_V2_RUN_TASK

router = APIRouter(prefix="/api/v2", tags=["workflow-studio"])


async def _enqueue_executor(request: Request, uow: SqlAlchemyUnitOfWork, run: WorkflowRunModel) -> str | None:
    """入队 v2 执行器；broker 不可用时把 run 落回 QUEUED，交给恢复循环重排。"""
    try:
        return await request.app.state.queue.enqueue(EXECUTE_V2_RUN_TASK, {"run_id": run.id})
    except Exception as exc:
        run.status = "QUEUED"
        await uow.workflows.append_event(run.id, "run.enqueue_failed", {"status": "QUEUED", "error": type(exc).__name__})
        return None


class DefinitionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    definition: dict[str, object]


class DefinitionUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    definition: dict[str, object]


class RunCreateRequest(BaseModel):
    token_limit: int = Field(default=0, ge=0)
    cost_limit: float = Field(default=0, ge=0)


@router.post("/projects/{project_id}/workflow-definitions", status_code=status.HTTP_201_CREATED)
async def create_definition(project_id: str, payload: DefinitionRequest, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        try:
            row = await WorkflowDefinitionService(uow).create(project_id, user.id, payload.name, payload.definition)
            await uow.commit()
            return definition_response(row)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except FileExistsError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/projects/{project_id}/workflow-definitions")
async def list_definitions(project_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> list[dict[str, object]]:
    async with uow:
        try:
            return [definition_response(row) for row in await WorkflowDefinitionService(uow).list(project_id, user.id)]
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/workflow-definitions/{definition_id}")
async def get_definition(definition_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        row = await WorkflowDefinitionService(uow).get(definition_id, user.id)
        if row is None:
            raise HTTPException(status_code=404, detail="workflow definition not found")
        return definition_response(row)


@router.put("/workflow-definitions/{definition_id}")
async def update_definition(definition_id: str, payload: DefinitionUpdateRequest, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        try:
            row = await WorkflowDefinitionService(uow).update(definition_id, user.id, payload.name, payload.definition)
            await uow.commit()
            return definition_response(row)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


@router.delete("/workflow-definitions/{definition_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_definition(definition_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> Response:
    async with uow:
        try:
            await WorkflowDefinitionService(uow).delete(definition_id, user.id)
            await uow.commit()
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/workflow-definitions/{definition_id}/runs", status_code=status.HTTP_201_CREATED)
async def create_run(definition_id: str, payload: RunCreateRequest, request: Request, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        try:
            run, nodes = await WorkflowRunService(uow).create(definition_id, user.id, payload.token_limit, payload.cost_limit)
            await uow.commit()
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        task_id = None
        if run.status == "RUNNING":
            task_id = await _enqueue_executor(request, uow, run)
            await uow.commit()
        response: dict[str, object] = {"run": run_response(run), "nodes": [node_response(node) for node in nodes]}
        if task_id is not None:
            response["task_id"] = task_id
        return response


@router.get("/workflow-runs/{run_id}")
async def get_run(run_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        snapshot = await WorkflowRunService(uow).snapshot(run_id, user.id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="workflow run not found")
        run, nodes, cursor = snapshot
        return {"run": run_response(run), "nodes": [node_response(node) for node in nodes], "event_cursor": cursor}


@router.post("/workflow-runs/{run_id}/{action}")
async def control_run(run_id: str, action: str, request: Request, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)], idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)]) -> dict[str, object]:
    async with uow:
        try:
            run, replay = await WorkflowRunService(uow).control(run_id, user.id, action, idempotency_key)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        task_id = None
        if not replay and action in {"resume", "retry"} and run.status == "RUNNING":
            task_id = await _enqueue_executor(request, uow, run)
        await uow.commit()
        response: dict[str, object] = {"run": run_response(run), "idempotent_replay": replay}
        if task_id is not None:
            response["task_id"] = task_id
        return response

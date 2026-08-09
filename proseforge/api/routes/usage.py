from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from proseforge.api.dependencies import current_user, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.application.usage.query_usage import UsageQuery
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1", tags=["usage"])


def _response(row) -> dict[str, object]:
    return {
        "id": row.id, "user_id": row.user_id, "project_id": row.project_id,
        "conversation_id": row.conversation_id, "message_id": row.message_id,
        "workflow_run_id": row.workflow_run_id, "workflow_step": row.workflow_step,
        "provider": row.provider, "model_id": row.model_id, "provider_request_id": row.provider_request_id,
        "input_tokens": row.input_tokens, "output_tokens": row.output_tokens,
        "cached_input_tokens": row.cached_input_tokens, "reasoning_tokens": row.reasoning_tokens,
        "total_tokens": row.total_tokens, "cost_usd": row.cost_usd,
        "usage_source": row.usage_source, "is_final": row.is_final,
        "latency_ms": row.latency_ms, "metadata": json.loads(row.metadata_json or "{}"),
    }


async def _owned_scope(uow: SqlAlchemyUnitOfWork, user_id: str, *, project_id: str | None = None, conversation_id: str | None = None, workflow_id: str | None = None) -> None:
    if project_id and await uow.projects.get_by_id(user_id, project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    if conversation_id and not await uow.conversations.belongs_to_owner(conversation_id, user_id):
        raise HTTPException(status_code=404, detail="conversation not found")
    if workflow_id and await uow.workflows.get_owned(workflow_id, user_id) is None:
        raise HTTPException(status_code=404, detail="workflow not found")


@router.get("/usage/records")
async def usage_records(user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)], project_id: str | None = None, conversation_id: str | None = None, workflow_id: str | None = None, message_id: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[dict[str, object]]:
    async with uow:
        await _owned_scope(uow, user.id, project_id=project_id, conversation_id=conversation_id, workflow_id=workflow_id)
        rows = await UsageQuery(uow.usage).records(user.id, project_id=project_id, conversation_id=conversation_id, workflow_run_id=workflow_id, message_id=message_id, limit=limit)
        return [_response(row) for row in rows]


@router.get("/usage/summary")
async def usage_summary(user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)], project_id: str | None = None, conversation_id: str | None = None, workflow_id: str | None = None) -> dict[str, object]:
    async with uow:
        await _owned_scope(uow, user.id, project_id=project_id, conversation_id=conversation_id, workflow_id=workflow_id)
        actual = await UsageQuery(uow.usage).summary(user.id, project_id=project_id, conversation_id=conversation_id, workflow_run_id=workflow_id)
        return {"scope": "project" if project_id else "conversation" if conversation_id else "workflow" if workflow_id else "user", "project_id": project_id, "conversation_id": conversation_id, "workflow_id": workflow_id, **actual}


@router.get("/usage/by-model")
async def usage_by_model(user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)], days: int = Query(default=7)) -> dict[str, object]:
    """Per-user token usage grouped by provider+model. days: 1/7/30 (else 7)."""
    async with uow:
        return await UsageQuery(uow.usage).by_model(user.id, days=days)


@router.get("/projects/{project_id}/usage")
async def project_usage(project_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    return await usage_summary(user, uow, project_id=project_id)


@router.get("/conversations/{conversation_id}/usage")
async def conversation_usage(conversation_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    return await usage_summary(user, uow, conversation_id=conversation_id)


@router.get("/workflows/{workflow_id}/usage")
async def workflow_usage(workflow_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    return await usage_summary(user, uow, workflow_id=workflow_id)

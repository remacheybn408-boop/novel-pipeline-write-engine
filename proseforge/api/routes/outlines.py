from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from proseforge.api.dependencies import current_user, require_work_project, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.application.outlines.intake_service import OutlineIntakeService
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1", tags=["outlines"])
intake = OutlineIntakeService()


class OutlineImportRequest(BaseModel):
    title: str = Field(default="Untitled outline", min_length=1, max_length=500)
    content: str = ""
    data: dict[str, object] = Field(default_factory=dict)
    # Optional writing-model selection carried from the workbench: importing
    # an outline locks the project's writing model (first-come-first-served
    # with the first generated chapter version).
    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=200)


class OutlineAnswerRequest(BaseModel):
    answers: dict[str, object] = Field(default_factory=dict)


def _payload(request: OutlineImportRequest) -> dict[str, object]:
    return {**request.data, "title": request.data.get("title", request.title), "raw_content": request.content}


def _response(outline) -> dict[str, object]:
    payload = json.loads(outline.payload)
    missing_fields = [field for field in intake.REQUIRED if not payload.get(field)]
    if payload.get("planned_volumes") is None and payload.get("planned_chapters") is None:
        missing_fields.append("planned_chapters")
    if payload.get("chapter_word_target") is None:
        missing_fields.append("chapter_word_target")
    return {
        "id": outline.id, "project_id": outline.project_id, "title": outline.title,
        "status": outline.status, "payload": payload,
        "missing_questions": json.loads(outline.missing_questions), "missing_fields": missing_fields, "confirmed": outline.confirmed,
    }


@router.post("/projects/{project_id}/outlines/import", status_code=status.HTTP_201_CREATED)
async def import_outline(
    project_id: str,
    payload: OutlineImportRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    data = _payload(payload)
    spec = intake.parse(data)
    async with uow:
        await require_work_project(uow, user.id, project_id)
        outline = await uow.outlines.add(project_id, spec.title, data, list(intake.clarification_questions(spec)))
        if payload.provider and payload.model:
            await uow.projects.lock_writing_model(project_id, provider=payload.provider, model_id=payload.model, source="outline_import")
        await uow.commit()
        return _response(outline)


@router.get("/projects/{project_id}/outlines")
async def list_outlines(
    project_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> list[dict[str, object]]:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        return [_response(row) for row in await uow.outlines.list_owned(project_id, user.id)]


@router.get("/outlines/{outline_id}")
async def get_outline(
    outline_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        outline = await uow.outlines.get_owned(outline_id, user.id)
        if outline is None:
            raise HTTPException(status_code=404, detail="outline not found")
        return _response(outline)


@router.post("/outlines/{outline_id}/parse")
async def parse_outline(
    outline_id: str,
    payload: OutlineAnswerRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        outline = await uow.outlines.get_owned(outline_id, user.id)
        if outline is None:
            raise HTTPException(status_code=404, detail="outline not found")
        data = {**json.loads(outline.payload), **payload.answers}
        spec = intake.parse(data)
        await uow.outlines.update(outline, data, list(intake.clarification_questions(spec)))
        await uow.commit()
        return _response(outline)


@router.post("/outlines/{outline_id}/confirm")
async def confirm_outline(
    outline_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        outline = await uow.outlines.get_owned(outline_id, user.id)
        if outline is None:
            raise HTTPException(status_code=404, detail="outline not found")
        missing = json.loads(outline.missing_questions)
        if missing:
            raise HTTPException(status_code=409, detail={"code": "OUTLINE_NEEDS_ANSWERS", "questions": missing})
        await uow.outlines.confirm(outline)
        await uow.commit()
        return _response(outline)

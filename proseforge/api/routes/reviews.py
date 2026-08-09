from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from proseforge.api.dependencies import current_user, require_work_project, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.application.quality.create_review import normalize_findings
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v2", tags=["reviews"])


class ReviewCreateRequest(BaseModel):
    scope: str = Field(default="chapter")
    subject_type: str = Field(default="chapter")
    subject_id: str = Field(min_length=1)
    findings: list[dict[str, object]] = Field(default_factory=list)
    scores: dict[str, object] = Field(default_factory=dict)
    model_snapshot: dict[str, object] = Field(default_factory=dict)
    context_snapshot_id: str | None = None
    usage_call_id: str | None = None


def report_response(row) -> dict[str, object]:
    return {"id": row.id, "project_id": row.project_id, "scope": row.scope, "subject_type": row.subject_type, "subject_id": row.subject_id, "findings": json.loads(row.findings_json), "scores": json.loads(row.scores_json), "model_snapshot": json.loads(row.model_snapshot_json), "context_snapshot_id": row.context_snapshot_id, "usage_call_id": row.usage_call_id, "status": row.status}


@router.post("/projects/{project_id}/reviews", status_code=status.HTTP_201_CREATED)
async def create_review(project_id: str, payload: ReviewCreateRequest, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        if payload.subject_type == "chapter":
            chapter = await uow.chapters.get_owned(payload.subject_id, user.id)
            if chapter is None or chapter.project_id != project_id:
                raise HTTPException(status_code=404, detail="subject not found")
        try:
            findings = normalize_findings(payload.findings)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        row = await uow.revisions.create_review(project_id=project_id, scope=payload.scope, subject_type=payload.subject_type, subject_id=payload.subject_id, findings=findings, scores=payload.scores, model_snapshot=payload.model_snapshot, context_snapshot_id=payload.context_snapshot_id, usage_call_id=payload.usage_call_id)
        await uow.commit()
        return report_response(row)


@router.get("/reviews/{review_id}")
async def get_review(review_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        row = await uow.revisions.get_review_owned(review_id, user.id)
        if row is None:
            raise HTTPException(status_code=404, detail="review not found")
        return report_response(row)

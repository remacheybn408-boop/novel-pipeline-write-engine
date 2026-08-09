"""Canon conflict endpoints (work-mode projects only).

Conflicts are heuristic evidence rows (same entity + same field +
different value); resolving never edits the underlying canon — it only
closes the row and records the user's note in the evidence.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from proseforge.api.dependencies import current_user, require_work_project, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1", tags=["conflicts"])


class ResolveRequest(BaseModel):
    resolution: str = Field(default="", max_length=500)


def _response(row) -> dict[str, object]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "candidate_source": row.candidate_source,
        "conflicting_source": row.conflicting_source,
        "field_or_claim": row.field_or_claim,
        "evidence": json.loads(row.evidence_json or "{}"),
        "status": row.status,
        "resolved_by": row.resolved_by,
    }


@router.get("/projects/{project_id}/conflicts")
async def list_conflicts(
    project_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
    status: Literal["open", "resolved"] | None = "open",
) -> list[dict[str, object]]:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        rows = await uow.retrieval.list_conflicts(project_id=project_id, owner_id=user.id, status=status)
        return [_response(row) for row in rows]


@router.post("/projects/{project_id}/conflicts/{conflict_id}/resolve")
async def resolve_conflict(
    project_id: str,
    conflict_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
    payload: ResolveRequest | None = None,
) -> dict[str, object]:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        row = await uow.retrieval.get_conflict_owned(conflict_id, project_id=project_id, owner_id=user.id)
        if row is None:
            raise HTTPException(status_code=404, detail="conflict not found")
        row.status = "resolved"
        row.resolved_by = user.id
        if payload and payload.resolution:
            evidence = json.loads(row.evidence_json or "{}")
            evidence["resolution"] = payload.resolution
            row.evidence_json = json.dumps(evidence, ensure_ascii=False)
        await uow.commit()
        return _response(row)

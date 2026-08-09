from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from proseforge.api.dependencies import current_user, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.application.revision.approve_proposal import (
    ApprovalBlocked,
    ApprovalConflict,
    approve_persisted_proposal,
)
from proseforge.application.revision.reject_proposal import reject_persisted_proposal
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v2", tags=["reviews-revisions"])


class ProposalRequest(BaseModel):
    base_version_id: str = Field(min_length=1)
    after_text: str
    rationale: str = Field(min_length=1, max_length=4000)
    hunks: list[dict[str, object]] | None = None
    affected_facts: list[object] = Field(default_factory=list)
    context_snapshot_id: str | None = None
    guard_status: str = Field(default="clear", pattern=r"^(clear|blocked)$")


class ProposalDecisionRequest(BaseModel):
    accept_hunks: list[int] | None = None


def _response(row) -> dict[str, object]:
    return {"id": row.id, "chapter_id": row.chapter_id, "base_version_id": row.base_version_id, "before_hash": row.before_hash, "after_hash": row.after_hash, "after_text": row.after_text, "rationale": row.rationale, "hunks": json.loads(row.hunks_json), "affected_facts": json.loads(row.affected_facts_json), "conflict_status": row.conflict_status, "guard_status": row.guard_status, "context_snapshot_id": row.context_snapshot_id, "status": row.status}


@router.post("/chapters/{chapter_id}/proposals", status_code=status.HTTP_201_CREATED)
@router.post("/chapters/{chapter_id}/revision-proposals", status_code=status.HTTP_201_CREATED)
async def create_proposal(chapter_id: str, payload: ProposalRequest, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        chapter = await uow.chapters.get_owned(chapter_id, user.id)
        if chapter is None:
            raise HTTPException(status_code=404, detail="chapter not found")
        base = await uow.chapters.get_version_owned(chapter_id, payload.base_version_id, user.id)
        if base is None:
            raise HTTPException(status_code=404, detail="base version not found")
        row = await uow.revisions.create(chapter_id=chapter_id, base_version_id=base.id, before=base.content, after=payload.after_text, rationale=payload.rationale, hunks=payload.hunks, affected_facts=payload.affected_facts, context_snapshot_id=payload.context_snapshot_id)
        row.guard_status = payload.guard_status
        await uow.commit()
        return _response(row)


@router.get("/chapters/{chapter_id}/proposals")
async def list_proposals(chapter_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> list[dict[str, object]]:
    async with uow:
        if await uow.chapters.get_owned(chapter_id, user.id) is None:
            raise HTTPException(status_code=404, detail="chapter not found")
        return [_response(row) for row in await uow.revisions.list_owned(chapter_id, user.id)]


@router.get("/proposals/{proposal_id}/diff")
@router.get("/revision-proposals/{proposal_id}/diff")
async def proposal_diff(proposal_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        row = await uow.revisions.get_owned(proposal_id, user.id)
        if row is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        return {"proposal_id": row.id, "before_hash": row.before_hash, "after_hash": row.after_hash, "hunks": json.loads(row.hunks_json), "after_text": row.after_text, "guard_status": row.guard_status}


@router.post("/proposals/{proposal_id}/reject")
@router.post("/revision-proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        try:
            row = await reject_persisted_proposal(uow=uow, proposal_id=proposal_id, user_id=user.id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        await uow.commit()
        return _response(row)


@router.post("/proposals/{proposal_id}/approve")
@router.post("/revision-proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: str, request: Request, payload: ProposalDecisionRequest | None = None, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None, user: Annotated[AuthUser, Depends(current_user)] = None, uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)] = None) -> dict[str, object]:
    async with uow:
        try:
            result = await approve_persisted_proposal(uow=uow, proposal_id=proposal_id, user_id=user.id, idempotency_key=idempotency_key, accept_hunks=payload.accept_hunks if payload else None)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ApprovalBlocked as error:
            raise HTTPException(status_code=422, detail={"code": "REVISION_GUARD_BLOCKED", "message": str(error)}) from error
        except ApprovalConflict as error:
            raise HTTPException(status_code=409, detail={"code": error.code, "current_version_id": error.current_version_id}) from error
        await uow.commit()
        if result.retrieval_job_id is not None:
            await request.app.state.queue.enqueue(
                "proseforge.retrieval.index_document",
                {"job_id": result.retrieval_job_id, "user_id": user.id},
            )
        if result.summarize_job_id is not None:
            await request.app.state.queue.enqueue(
                "proseforge.work.summarize_chapter",
                {"job_id": result.summarize_job_id, "user_id": user.id},
            )
        version = result.version
        return {**_response(result.proposal), "replayed": result.replayed, "version": None if version is None else {"id": version.id, "version_no": version.version_no, "content": version.content}}

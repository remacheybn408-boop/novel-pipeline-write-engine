from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AliasChoices, BaseModel, Field
from sqlalchemy import select, update

from proseforge.api.dependencies import current_user, require_work_project, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.application.story_bible.service import (
    StoryBibleService,
    StoryBibleStatusTransitionError,
)
from proseforge.domain.story_bible.entities import (
    EXCLUDED_STATUS,
    StoryFact,
    StoryFactValidationError,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel

router = APIRouter(prefix="/api/v2", tags=["story-bible"])


class FactRequest(BaseModel):
    kind: str
    key: str = Field(min_length=1, max_length=200)
    value: dict[str, object]
    pinned: bool = False


class FactPatchRequest(BaseModel):
    expected_version: int = Field(ge=1, validation_alias=AliasChoices("expected_version", "version"))
    kind: str | None = None
    key: str | None = Field(default=None, min_length=1, max_length=200)
    value: dict[str, object] | None = None
    pinned: bool | None = None


class PromiseStatusRequest(BaseModel):
    status: str = Field(min_length=1, max_length=32)
    version: int | None = Field(default=None, ge=1)


async def _owned_fact(uow, entry_id: str, user_id: str) -> StoryBibleEntryModel:
    row = await uow.session.scalar(
        select(StoryBibleEntryModel)
        .join(ProjectModel, ProjectModel.id == StoryBibleEntryModel.project_id)
        .where(StoryBibleEntryModel.id == entry_id, ProjectModel.owner_id == user_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="fact not found")
    return row


def _invalid_fact(error: StoryFactValidationError) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "INVALID_STORY_FACT", "message": str(error)})


def _invalid_transition(error: StoryBibleStatusTransitionError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "code": "INVALID_PROMISE_STATE_TRANSITION",
            "details": {"allowed": list(error.allowed)},
        },
    )


@router.get("/projects/{project_id}/story-bible")
async def list_facts(project_id: str, user: Annotated[AuthUser, Depends(current_user)], uow=Depends(unit_of_work)):
    async with uow:
        await require_work_project(uow, user.id, project_id)
        rows = (await uow.session.scalars(select(StoryBibleEntryModel).where(StoryBibleEntryModel.project_id == project_id).order_by(StoryBibleEntryModel.kind, StoryBibleEntryModel.key))).all()
        return [_response(row) for row in rows]


@router.post("/projects/{project_id}/story-bible/entries", status_code=status.HTTP_201_CREATED)
async def create_fact(project_id: str, payload: FactRequest, user: Annotated[AuthUser, Depends(current_user)], uow=Depends(unit_of_work)):
    async with uow:
        await require_work_project(uow, user.id, project_id)
        try:
            fact = StoryFact.create(project_id, payload.kind, payload.key, payload.value, pinned=payload.pinned)
        except StoryFactValidationError as error:
            raise _invalid_fact(error) from error
        now = datetime.now(UTC)
        row = StoryBibleEntryModel(id=fact.id, project_id=project_id, kind=fact.kind, key=fact.key, value_json=json.dumps(fact.value, ensure_ascii=False), status=fact.status, pinned=fact.pinned, created_at=now, updated_at=now)
        uow.session.add(row); await uow.commit(); return _response(row)


@router.post("/story-bible/{entry_id}/pin")
async def pin_fact(entry_id: str, user: Annotated[AuthUser, Depends(current_user)], uow=Depends(unit_of_work)):
    async with uow:
        row = await _owned_fact(uow, entry_id, user.id)
        expected_version = row.version
        pinned = not row.pinned
        result = await uow.session.execute(
            update(StoryBibleEntryModel)
            .where(StoryBibleEntryModel.id == row.id, StoryBibleEntryModel.version == expected_version)
            .values(pinned=pinned, version=expected_version + 1, updated_at=datetime.now(UTC))
        )
        if result.rowcount != 1:
            current_version = await uow.session.scalar(select(StoryBibleEntryModel.version).where(StoryBibleEntryModel.id == entry_id))
            raise HTTPException(status_code=409, detail={"code": "VERSION_CONFLICT", "current_version": current_version})
        await uow.commit()
        return {**_response(row), "pinned": pinned, "version": expected_version + 1}


@router.post("/story-bible/{entry_id}/exclude")
async def toggle_exclude_fact(entry_id: str, user: Annotated[AuthUser, Depends(current_user)], uow=Depends(unit_of_work)):
    """Exclude/restore a fact (pin-style toggle with optimistic locking).

    Excluded rows are invisible to every retrieval layer (scene pack,
    scene state, conflict detection); restore returns non-promise facts
    to "active" and promises to "open".
    """
    async with uow:
        row = await _owned_fact(uow, entry_id, user.id)
        expected_version = row.version
        if row.status == EXCLUDED_STATUS:
            new_status = "open" if row.kind == "promise" else "active"
        else:
            new_status = EXCLUDED_STATUS
        result = await uow.session.execute(
            update(StoryBibleEntryModel)
            .where(StoryBibleEntryModel.id == row.id, StoryBibleEntryModel.version == expected_version)
            .values(status=new_status, version=expected_version + 1, updated_at=datetime.now(UTC))
        )
        if result.rowcount != 1:
            current_version = await uow.session.scalar(select(StoryBibleEntryModel.version).where(StoryBibleEntryModel.id == entry_id))
            raise HTTPException(status_code=409, detail={"code": "VERSION_CONFLICT", "current_version": current_version})
        await uow.commit()
        return {**_response(row), "status": new_status, "version": expected_version + 1}


@router.patch("/story-bible/{entry_id}")
async def update_fact(
    entry_id: str,
    payload: FactPatchRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow=Depends(unit_of_work),
) -> dict[str, object]:
    changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
    async with uow:
        row = await _owned_fact(uow, entry_id, user.id)
        if row.version != payload.expected_version:
            raise HTTPException(status_code=409, detail={"code": "VERSION_CONFLICT", "current_version": row.version})
        try:
            updated = StoryBibleService.update_fact(StoryBibleService.from_record(row), changes)
        except StoryFactValidationError as error:
            raise _invalid_fact(error) from error

        result = await uow.session.execute(
            update(StoryBibleEntryModel)
            .where(StoryBibleEntryModel.id == row.id, StoryBibleEntryModel.version == payload.expected_version)
            .values(
                kind=updated.kind,
                key=updated.key,
                value_json=json.dumps(updated.value, ensure_ascii=False),
                status=updated.status,
                pinned=updated.pinned,
                version=payload.expected_version + 1,
                updated_at=datetime.now(UTC),
            )
        )
        if result.rowcount != 1:
            current_version = await uow.session.scalar(select(StoryBibleEntryModel.version).where(StoryBibleEntryModel.id == entry_id))
            raise HTTPException(status_code=409, detail={"code": "VERSION_CONFLICT", "current_version": current_version})
        await uow.commit()
        return {
            "id": row.id,
            "project_id": updated.project_id,
            "kind": updated.kind,
            "key": updated.key,
            "value": updated.value,
            "status": updated.status,
            "confidence": row.confidence,
            "source": row.source,
            "pinned": updated.pinned,
            "version": payload.expected_version + 1,
        }


@router.post("/story-bible/{entry_id}/status")
async def transition_promise_status(
    entry_id: str,
    payload: PromiseStatusRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow=Depends(unit_of_work),
) -> dict[str, object]:
    async with uow:
        row = await _owned_fact(uow, entry_id, user.id)
        expected_version = payload.version if payload.version is not None else row.version
        if row.version != expected_version:
            raise HTTPException(status_code=409, detail={"code": "VERSION_CONFLICT", "current_version": row.version})
        try:
            updated = StoryBibleService.validate_status_transition(StoryBibleService.from_record(row), payload.status)
        except StoryBibleStatusTransitionError as error:
            raise _invalid_transition(error) from error

        result = await uow.session.execute(
            update(StoryBibleEntryModel)
            .where(StoryBibleEntryModel.id == row.id, StoryBibleEntryModel.version == expected_version)
            .values(status=updated.status, version=expected_version + 1, updated_at=datetime.now(UTC))
        )
        if result.rowcount != 1:
            current_version = await uow.session.scalar(select(StoryBibleEntryModel.version).where(StoryBibleEntryModel.id == entry_id))
            raise HTTPException(status_code=409, detail={"code": "VERSION_CONFLICT", "current_version": current_version})
        await uow.commit()
        return {
            "id": row.id,
            "project_id": updated.project_id,
            "kind": updated.kind,
            "key": updated.key,
            "value": updated.value,
            "status": updated.status,
            "confidence": row.confidence,
            "source": row.source,
            "pinned": updated.pinned,
            "version": expected_version + 1,
        }


def _response(row: StoryBibleEntryModel) -> dict[str, object]:
    return {"id": row.id, "project_id": row.project_id, "kind": row.kind, "key": row.key, "value": json.loads(row.value_json), "status": row.status, "confidence": row.confidence, "source": row.source, "pinned": row.pinned, "version": row.version}

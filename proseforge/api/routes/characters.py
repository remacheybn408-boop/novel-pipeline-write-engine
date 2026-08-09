"""Character CRUD endpoints (work-mode projects only)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from proseforge.api.dependencies import current_user, require_work_project, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.domain.characters.entity import Character
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1", tags=["characters"])


class CharacterCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    summary: str = ""
    role: str = Field(default="", max_length=64)


class CharacterUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    aliases: list[str] | None = Field(default=None, max_length=20)
    summary: str | None = None
    role: str | None = Field(default=None, max_length=64)
    status: Literal["active", "archived"] | None = None


def _response(character: Character) -> dict[str, object]:
    return {
        "id": character.id,
        "project_id": character.project_id,
        "name": character.name,
        "aliases": character.aliases,
        "summary": character.summary,
        "role": character.role,
        "first_seen_chapter": character.first_seen_chapter,
        "last_seen_chapter": character.last_seen_chapter,
        "status": character.status,
        "source": character.source,
        "confidence": character.confidence,
    }


@router.get("/projects/{project_id}/characters")
async def list_characters(
    project_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
    include_archived: bool = False,
) -> list[dict[str, object]]:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        characters = await uow.characters.list_owned(project_id, user.id, include_archived=include_archived)
        return [_response(character) for character in characters]


@router.post("/projects/{project_id}/characters", status_code=status.HTTP_201_CREATED)
async def create_character(
    project_id: str,
    payload: CharacterCreateRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        name = payload.name.strip()
        if await uow.characters.get_by_name(project_id, name) is not None:
            raise HTTPException(status_code=409, detail="character name already exists")
        character = Character.create(
            project_id=project_id, name=name,
            aliases=[alias.strip() for alias in payload.aliases if alias.strip()],
            summary=payload.summary, role=payload.role,
        )
        try:
            await uow.characters.add(character)
            await uow.commit()
        except IntegrityError:
            await uow.rollback()
            raise HTTPException(status_code=409, detail="character name already exists") from None
        return _response(character)


@router.patch("/projects/{project_id}/characters/{character_id}")
async def update_character(
    project_id: str,
    character_id: str,
    payload: CharacterUpdateRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        updates = payload.model_dump(exclude_unset=True)
        if "name" in updates and updates["name"] is not None:
            updates["name"] = str(updates["name"]).strip()
        if "aliases" in updates and updates["aliases"] is not None:
            updates["aliases"] = [str(alias).strip() for alias in updates["aliases"] if str(alias).strip()]
        try:
            character = await uow.characters.update_owned(character_id, project_id, user.id, **updates)
            await uow.commit()
        except IntegrityError:
            await uow.rollback()
            raise HTTPException(status_code=409, detail="character name already exists") from None
        if character is None:
            raise HTTPException(status_code=404, detail="character not found")
        return _response(character)


@router.delete("/projects/{project_id}/characters/{character_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_character(
    project_id: str,
    character_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> None:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        deleted = await uow.characters.delete_owned(character_id, project_id, user.id)
        await uow.commit()
        if not deleted:
            raise HTTPException(status_code=404, detail="character not found")

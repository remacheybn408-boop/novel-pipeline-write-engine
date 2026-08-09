from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from proseforge.api.dependencies import current_user, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1/model-profiles", tags=["model-profiles"])


class ModelProfileRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    config: dict[str, object] = Field(default_factory=dict)
    role: Literal["writer", "editor"] = "writer"


class ModelProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict[str, object] | None = None


@router.get("")
async def list_profiles(user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> list[dict[str, object]]:
    async with uow:
        return await uow.model_profiles.list_for_user(user.id)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_profile(payload: ModelProfileRequest, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        config = {**payload.config, "role": payload.role}
        profile = await uow.model_profiles.create(user.id, payload.name, config)
        await uow.commit()
        return profile


@router.patch("/{profile_id}")
async def update_profile(profile_id: str, payload: ModelProfilePatch, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    async with uow:
        row = await uow.model_profiles.get_owned(user.id, profile_id)
        if row is None:
            raise HTTPException(status_code=404, detail="model profile not found")
        result = await uow.model_profiles.update(row, payload.name, payload.config)
        await uow.commit()
        return result


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(profile_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> None:
    async with uow:
        row = await uow.model_profiles.get_owned(user.id, profile_id)
        if row is None:
            raise HTTPException(status_code=404, detail="model profile not found")
        await uow.model_profiles.delete(row)
        await uow.commit()

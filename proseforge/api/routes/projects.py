from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from proseforge.api.dependencies import current_user, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.blob.local import LocalBlobStore
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


class ProjectCreateRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=200, pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(min_length=1, max_length=500)
    genre: str = ""
    style: str = ""
    mode: Literal["work", "chat"] = "work"


class ProjectResponse(BaseModel):
    id: str
    slug: str
    title: str
    genre: str
    style: str
    language: str
    status: str
    mode: str
    # Writing-model lock, flat and read-only (locked <=> model_locked_at set).
    writing_model_provider: str | None = None
    writing_model_id: str | None = None
    model_locked_at: datetime | None = None
    model_lock_source: str | None = None


class ProjectUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    genre: str | None = Field(default=None, max_length=200)
    style: str | None = Field(default=None, max_length=200)


def _response(project: Project) -> ProjectResponse:
    return ProjectResponse(
        id=project.id, slug=project.slug, title=project.title, genre=project.genre,
        style=project.style, language=project.language, status=project.status, mode=project.mode,
        writing_model_provider=project.writing_model_provider,
        writing_model_id=project.writing_model_id,
        model_locked_at=project.model_locked_at,
        model_lock_source=project.model_lock_source,
    )


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreateRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> ProjectResponse:
    async with uow:
        existing = await uow.projects.get_by_slug(user.id, payload.slug)
        if existing:
            raise HTTPException(status_code=409, detail="project slug already exists")
        project = Project.create(owner_id=user.id, slug=payload.slug, title=payload.title, genre=payload.genre, style=payload.style, mode=payload.mode)
        await uow.projects.add(project)
        await uow.commit()
        return _response(project)


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
    mode: Literal["work", "chat"] | None = None,
    archived: bool = False,
) -> list[ProjectResponse]:
    async with uow:
        return [_response(project) for project in await uow.projects.list_for_owner(user.id, mode, archived)]


@router.get("/{slug}", response_model=ProjectResponse)
async def get_project(
    slug: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> ProjectResponse:
    async with uow:
        project = await uow.projects.get_by_slug(user.id, slug)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return _response(project)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: str,
    payload: ProjectUpdateRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> ProjectResponse:
    async with uow:
        project = await uow.projects.update(user.id, project_id, **payload.model_dump(exclude_unset=True))
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        await uow.commit()
        return _response(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: str,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> None:
    async with uow:
        orphaned_blob_keys = await uow.projects.delete(user.id, project_id)
        if orphaned_blob_keys is None:
            raise HTTPException(status_code=404, detail="project not found")
        await uow.commit()
    # Blob files go only after the DB commit succeeded; keys returned here
    # have no remaining references in attachments/artifacts.
    blob_store = LocalBlobStore(request.app.state.settings.blob_root)
    for storage_key in orphaned_blob_keys:
        await blob_store.delete(storage_key)


async def _set_project_status(project_id: str, status_value: str, user: AuthUser, uow: SqlAlchemyUnitOfWork) -> ProjectResponse:
    async with uow:
        project = await uow.projects.set_status(user.id, project_id, status_value)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        await uow.commit()
        return _response(project)


@router.post("/{project_id}/archive")
async def archive_project(project_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    return await _set_project_status(project_id, "ARCHIVED", user, uow)


@router.post("/{project_id}/restore")
async def restore_project(project_id: str, user: Annotated[AuthUser, Depends(current_user)], uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)]) -> dict[str, object]:
    return await _set_project_status(project_id, "ACTIVE", user, uow)

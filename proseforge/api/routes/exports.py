from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from proseforge.api.dependencies import current_user, require_work_project, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.application.writing.export_service import ExportArtifact, render_export
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)
from proseforge.infrastructure.database.models.export import ExportManifestModel
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1", tags=["exports"])

ExportFormat = Literal["txt", "md", "docx", "epub"]
ExportTemplate = Literal["web-serial", "submission", "archive"]


class ExportRequest(BaseModel):
    format: ExportFormat
    chapter_range: tuple[int, int] | None = None
    version_ids: list[str] = Field(default_factory=list, max_length=200)
    locale: str = Field(default="en", min_length=2, max_length=32)
    title: str | None = Field(default=None, max_length=500)
    author: str | None = Field(default=None, max_length=200)
    template: ExportTemplate = "archive"

    @model_validator(mode="after")
    def validate_range(self) -> ExportRequest:
        if self.chapter_range is not None:
            start, end = self.chapter_range
            if start < 1 or end < start:
                raise ValueError("chapter_range must be an inclusive positive range")
        if len(self.version_ids) != len(set(self.version_ids)):
            raise ValueError("version_ids must be unique")
        return self


class ManifestResponse(BaseModel):
    id: str
    project_id: str
    format: str
    template: str
    title: str | None
    locale: str
    version_ids: list[str]
    content_hashes: dict[str, str]
    file_sha256: str
    byte_size: int
    download_url: str


@dataclass(frozen=True)
class SnapshotChapter:
    chapter_no: int
    title: str


async def _resolve_snapshot(
    uow: SqlAlchemyUnitOfWork,
    *,
    project_id: str,
    owner_id: str,
    version_ids: list[str],
    chapter_range: tuple[int, int] | None,
) -> list[tuple[SnapshotChapter, ChapterVersionModel]]:
    chapter_query = (
        select(ChapterModel)
        .join(ProjectModel, ProjectModel.id == ChapterModel.project_id)
        .where(ChapterModel.project_id == project_id, ProjectModel.owner_id == owner_id)
        .order_by(ChapterModel.chapter_no)
    )
    if chapter_range is not None:
        chapter_query = chapter_query.where(ChapterModel.chapter_no.between(*chapter_range))
    chapters = list(await uow.session.scalars(chapter_query))
    if not chapters:
        raise HTTPException(status_code=404, detail="no chapters found for export")
    chapter_by_id = {chapter.id: chapter for chapter in chapters}

    if version_ids:
        rows = list(
            await uow.session.scalars(
                select(ChapterVersionModel).where(
                    ChapterVersionModel.id.in_(version_ids),
                    ChapterVersionModel.chapter_id.in_(chapter_by_id),
                )
            )
        )
        if len(rows) != len(version_ids):
            raise HTTPException(status_code=404, detail="one or more versions not found")
        if len({row.chapter_id for row in rows}) != len(rows):
            raise HTTPException(status_code=422, detail="select at most one version per chapter")
        row_by_chapter = {row.chapter_id: row for row in rows}
        selected = [(chapter, row_by_chapter[chapter.id]) for chapter in chapters if chapter.id in row_by_chapter]
    else:
        missing = [chapter.chapter_no for chapter in chapters if chapter.active_version_id is None]
        if missing:
            raise HTTPException(status_code=409, detail={"message": "every exported chapter needs an active version", "chapter_numbers": missing})
        active_ids = [chapter.active_version_id for chapter in chapters if chapter.active_version_id]
        rows = list(await uow.session.scalars(select(ChapterVersionModel).where(ChapterVersionModel.id.in_(active_ids))))
        row_by_id = {row.id: row for row in rows}
        if len(row_by_id) != len(active_ids):
            raise HTTPException(status_code=409, detail="an active chapter version is missing")
        selected = [(chapter, row_by_id[chapter.active_version_id]) for chapter in chapters]

    return [(SnapshotChapter(chapter.chapter_no, chapter.title), version) for chapter, version in selected]


def _render(payload: ExportRequest, project_title: str, snapshot: list[tuple[SnapshotChapter, ChapterVersionModel]]) -> ExportArtifact:
    return render_export(
        format_name=payload.format,
        chapters=[(chapter, version.content) for chapter, version in snapshot],
        title=payload.title or project_title,
        author=payload.author or "",
        locale=payload.locale,
        template=payload.template,
    )


def _manifest_response(manifest: ExportManifestModel) -> ManifestResponse:
    return ManifestResponse(
        id=manifest.id,
        project_id=manifest.project_id,
        format=manifest.format,
        template=manifest.template,
        title=manifest.title,
        locale=manifest.locale,
        version_ids=json.loads(manifest.version_ids_json),
        content_hashes=json.loads(manifest.content_hashes_json),
        file_sha256=manifest.file_sha256,
        byte_size=manifest.byte_size,
        download_url=f"/api/v1/projects/{manifest.project_id}/exports/{manifest.id}/download",
    )


@router.post("/projects/{project_id}/exports", response_model=ManifestResponse, status_code=status.HTTP_201_CREATED)
async def request_export(
    project_id: str,
    payload: ExportRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> ManifestResponse:
    async with uow:
        project = await require_work_project(uow, user.id, project_id)
        snapshot = await _resolve_snapshot(
            uow,
            project_id=project_id,
            owner_id=user.id,
            version_ids=payload.version_ids,
            chapter_range=payload.chapter_range,
        )
        artifact = _render(payload, project.title, snapshot)
        version_ids = [version.id for _, version in snapshot]
        manifest = await uow.exports.create(
            project_id=project_id,
            user_id=user.id,
            format_name=payload.format,
            template=payload.template,
            title=payload.title or project.title,
            author=payload.author,
            locale=payload.locale,
            version_ids=version_ids,
            content_hashes={version.id: version.content_hash for _, version in snapshot},
            file_sha256=artifact.sha256,
            byte_size=len(artifact.body),
        )
        await uow.commit()
        return _manifest_response(manifest)


@router.get("/projects/{project_id}/exports/{manifest_id}", response_model=ManifestResponse)
async def get_export_manifest(
    project_id: str,
    manifest_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> ManifestResponse:
    async with uow:
        manifest = await uow.exports.get_owned(manifest_id, project_id, user.id)
        if manifest is None:
            raise HTTPException(status_code=404, detail="export manifest not found")
        return _manifest_response(manifest)


@router.get("/projects/{project_id}/exports/{manifest_id}/download")
async def download_export(
    project_id: str,
    manifest_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> Response:
    async with uow:
        manifest = await uow.exports.get_owned(manifest_id, project_id, user.id)
        if manifest is None:
            raise HTTPException(status_code=404, detail="export manifest not found")
        project = await require_work_project(uow, user.id, project_id)
        version_ids = json.loads(manifest.version_ids_json)
        snapshot = await _resolve_snapshot(
            uow,
            project_id=project_id,
            owner_id=user.id,
            version_ids=version_ids,
            chapter_range=None,
        )
        payload = ExportRequest(
            format=manifest.format,
            version_ids=version_ids,
            locale=manifest.locale,
            title=manifest.title,
            author=manifest.author,
            template=manifest.template,
        )
        artifact = _render(payload, project.title, snapshot)
        if artifact.sha256 != manifest.file_sha256:
            raise HTTPException(status_code=409, detail="export snapshot hash mismatch")
        manifest_id_value = manifest.id
        manifest_hash = manifest.file_sha256
        manifest_title = manifest.title
    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", manifest_title or "proseforge").strip("-") or "proseforge"
    return Response(
        content=artifact.body,
        media_type=artifact.media_type,
        headers={
            "content-disposition": f'attachment; filename="{filename}.{artifact.extension}"',
            "x-proseforge-manifest-id": manifest_id_value,
            "x-proseforge-file-sha256": manifest_hash,
        },
    )

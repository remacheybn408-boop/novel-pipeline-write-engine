from __future__ import annotations

import difflib
import io
import re
import zipfile
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from proseforge.api.dependencies import current_user, require_work_project, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.application.writing.selection_action import (
    SelectionActionConflict,
    SelectionActionRequest,
    SelectionActionValidationError,
    create_selection_action_proposals,
)
from proseforge.domain.chapter.entity import Chapter
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1", tags=["chapters"])
v2_router = APIRouter(prefix="/api/v2", tags=["chapters"])


class ChapterCreateRequest(BaseModel):
    chapter_no: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=500)


class VersionCreateRequest(BaseModel):
    content: str = Field(min_length=0)
    base_version: int | None = Field(default=None, ge=1)


class SelectionActionPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    action: str = Field(pattern=r"^(continue|expand|shorten|rewrite|change-tone|review)$")
    start: int = Field(alias="from", ge=0)
    end: int = Field(alias="to", ge=0)
    selected_text_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_version_id: str = Field(min_length=1)
    params: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_range(self) -> SelectionActionPayload:
        if self.end <= self.start:
            raise ValueError("to must be greater than from")
        return self


def chapter_response(chapter: Chapter) -> dict[str, object]:
    return {
        "id": chapter.id,
        "project_id": chapter.project_id,
        "chapter_no": chapter.chapter_no,
        "title": chapter.title,
        "status": chapter.status,
        "active_version_id": chapter.active_version_id,
    }


async def enqueue_chapter_index(request: Request, *, job_id: str, user_id: str) -> None:
    """Dispatch the narrative-RAG worker task for a job row already committed
    in the caller's transaction; replay is safe (same version is skipped)."""
    await request.app.state.queue.enqueue(
        "proseforge.retrieval.index_document",
        {"job_id": job_id, "user_id": user_id},
    )


@router.post("/projects/{project_id}/chapters", status_code=status.HTTP_201_CREATED)
async def create_chapter(
    project_id: str,
    payload: ChapterCreateRequest,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        chapter = Chapter.create(project_id=project_id, chapter_no=payload.chapter_no, title=payload.title)
        await uow.chapters.add(chapter)
        # Narrative RAG: queue indexing in the same transaction, dispatch after commit.
        index_job = await uow.retrieval.enqueue_job(
            project_id=project_id, job_type="index_chapter", source_type="chapter", source_id=chapter.id
        )
        await uow.commit()
        await enqueue_chapter_index(request, job_id=index_job.id, user_id=user.id)
        return chapter_response(chapter)


@router.get("/projects/{project_id}/chapters")
async def list_chapters(
    project_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> list[dict[str, object]]:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        return [chapter_response(chapter) for chapter in await uow.chapters.list_owned(project_id, user.id)]


@router.get("/projects/{project_id}/chapters/{chapter_id}/content")
async def get_chapter_content(
    project_id: str,
    chapter_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    """Active-version full text of one chapter (frontend getChapterContent)."""
    async with uow:
        await require_work_project(uow, user.id, project_id)
        chapter = await uow.chapters.get_owned(chapter_id, user.id)
        if chapter is None or chapter.project_id != project_id:
            raise HTTPException(status_code=404, detail="chapter not found")
        content = ""
        if chapter.active_version_id:
            version = await uow.chapters.get_version_owned(chapter_id, chapter.active_version_id, user.id)
            if version is not None:
                content = version.content
        return {"chapter_id": chapter.id, "title": chapter.title, "chapter_no": chapter.chapter_no, "content": content}


# Filename-unsafe characters (Windows + path separators) collapse to "_".
_FILENAME_UNSAFE_PATTERN = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _chapter_file_name(chapter_no: int, title: str, used: set[str]) -> str:
    """「第N章_标题.md」, sanitized and deduped within one download."""
    safe_title = _FILENAME_UNSAFE_PATTERN.sub("_", title).strip() or f"第{chapter_no}章"
    name = f"第{chapter_no}章_{safe_title}.md"
    suffix = 2
    candidate = name
    while candidate in used:
        candidate = f"第{chapter_no}章_{safe_title}-{suffix}.md"
        suffix += 1
    used.add(candidate)
    return candidate


@router.get("/projects/{project_id}/chapters/download")
async def download_chapters_zip(
    project_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> Response:
    """Zip of every completed chapter's active-version 正文 (「第N章_标题.md」).

    Chapters without an active version are skipped; 409 when nothing is
    downloadable yet.
    """
    async with uow:
        await require_work_project(uow, user.id, project_id)
        chapters = sorted(await uow.chapters.list_owned(project_id, user.id), key=lambda chapter: chapter.chapter_no)
        buffer = io.BytesIO()
        used_names: set[str] = set()
        members = 0
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for chapter in chapters:
                if not chapter.active_version_id:
                    continue
                version = await uow.chapters.get_version_owned(chapter.id, chapter.active_version_id, user.id)
                if version is None:
                    continue
                archive.writestr(_chapter_file_name(chapter.chapter_no, chapter.title, used_names), version.content)
                members += 1
        if members == 0:
            raise HTTPException(status_code=409, detail="no completed chapters to download")
        return Response(
            content=buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="proseforge-chapters-{project_id[:8]}.zip"'},
        )


@router.get("/projects/{project_id}/chapters/{chapter_id}/download")
async def download_chapter(
    project_id: str,
    chapter_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> Response:
    """Single-chapter markdown download (active version, completed chapters only)."""
    async with uow:
        await require_work_project(uow, user.id, project_id)
        chapter = await uow.chapters.get_owned(chapter_id, user.id)
        if chapter is None or chapter.project_id != project_id:
            raise HTTPException(status_code=404, detail="chapter not found")
        if not chapter.active_version_id:
            raise HTTPException(status_code=409, detail="chapter has no completed version")
        version = await uow.chapters.get_version_owned(chapter_id, chapter.active_version_id, user.id)
        if version is None:
            raise HTTPException(status_code=404, detail="chapter version not found")
        file_name = _chapter_file_name(chapter.chapter_no, chapter.title, set())
        return Response(
            content=version.content,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_name)}"},
        )


@router.post("/chapters/{chapter_id}/versions", status_code=status.HTTP_201_CREATED)
async def append_version(
    chapter_id: str,
    payload: VersionCreateRequest,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        chapter = await uow.chapters.get_owned(chapter_id, user.id)
        if chapter is None:
            raise HTTPException(status_code=404, detail="chapter not found")
        if payload.base_version is not None:
            current = chapter.active_version_id
            versions = await uow.chapters.list_versions(chapter_id, user.id)
            active = next((version for version in versions if version.id == current), None)
            if active is not None and active.version_no != payload.base_version:
                raise HTTPException(status_code=409, detail={"code": "VERSION_CONFLICT", "current_version": active.version_no})
        version = await uow.chapters.append_version(chapter_id=chapter_id, content=payload.content)
        await uow.chapters.set_active_version(chapter_id, version.id)
        # Narrative RAG: queue re-indexing of the new active version in the same transaction.
        index_job = await uow.retrieval.enqueue_job(
            project_id=chapter.project_id, job_type="index_chapter", source_type="chapter", source_id=chapter_id
        )
        await uow.commit()
        await enqueue_chapter_index(request, job_id=index_job.id, user_id=user.id)
        return {"id": version.id, "chapter_id": version.chapter_id, "version_no": version.version_no, "content": version.content, "word_count": version.word_count}


@router.get("/chapters/{chapter_id}/versions")
async def list_versions(
    chapter_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> list[dict[str, object]]:
    async with uow:
        if await uow.chapters.get_owned(chapter_id, user.id) is None:
            raise HTTPException(status_code=404, detail="chapter not found")
        return [
            {"id": version.id, "chapter_id": version.chapter_id, "version_no": version.version_no,
             "content": version.content, "word_count": version.word_count}
            for version in await uow.chapters.list_versions(chapter_id, user.id)
        ]


@router.post("/chapters/{chapter_id}/activate-version")
async def activate_version(
    chapter_id: str,
    version_id: str,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        chapter = await uow.chapters.get_owned(chapter_id, user.id)
        if chapter is None:
            raise HTTPException(status_code=404, detail="chapter not found")
        version = await uow.chapters.get_version_owned(chapter_id, version_id, user.id)
        if version is None:
            raise HTTPException(status_code=404, detail="version not found")
        await uow.chapters.set_active_version(chapter_id, version.id)
        # Narrative RAG: rollback changes the active version, so the indexed
        # content must be refreshed; the worker skips no-op replays.
        index_job = await uow.retrieval.enqueue_job(
            project_id=chapter.project_id, job_type="index_chapter", source_type="chapter", source_id=chapter_id
        )
        await uow.commit()
        await enqueue_chapter_index(request, job_id=index_job.id, user_id=user.id)
        return {"chapter_id": chapter_id, "active_version_id": version.id, "version_no": version.version_no}


@router.get("/chapters/{chapter_id}/diff")
async def chapter_diff(
    chapter_id: str,
    from_version: int,
    to_version: int,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        if await uow.chapters.get_owned(chapter_id, user.id) is None:
            raise HTTPException(status_code=404, detail="chapter not found")
        versions = await uow.chapters.list_versions(chapter_id, user.id)
    source = next((version for version in versions if version.version_no == from_version), None)
    target = next((version for version in versions if version.version_no == to_version), None)
    if source is None or target is None:
        raise HTTPException(status_code=404, detail="version not found")
    diff = list(difflib.unified_diff(source.content.splitlines(), target.content.splitlines(), fromfile=f"v{from_version}", tofile=f"v{to_version}", lineterm=""))
    return {"chapter_id": chapter_id, "from_version": from_version, "to_version": to_version, "changed": source.content != target.content, "diff": diff}


@v2_router.post("/chapters/{chapter_id}/selection-actions", status_code=status.HTTP_201_CREATED)
async def create_selection_action(
    chapter_id: str,
    payload: SelectionActionPayload,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    request = SelectionActionRequest(
        action=payload.action,  # type: ignore[arg-type]
        start=payload.start,
        end=payload.end,
        selected_text_hash=payload.selected_text_hash,
        base_version_id=payload.base_version_id,
        params=payload.params,
    )
    async with uow:
        try:
            result = await create_selection_action_proposals(
                uow=uow,
                owner_id=user.id,
                chapter_id=chapter_id,
                request=request,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except SelectionActionConflict as error:
            raise HTTPException(
                status_code=409,
                detail={"code": error.code, "current_version_id": error.current_version_id},
            ) from error
        except SelectionActionValidationError as error:
            raise HTTPException(status_code=422, detail={"code": error.code}) from error
        await uow.commit()
    if payload.action == "continue":
        return {"candidate_proposal_ids": list(result.proposal_ids)}
    if payload.action == "review":
        return {"review_id": result.review_id}
    return {"proposal_id": result.proposal_ids[0]}

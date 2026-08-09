from __future__ import annotations

import hashlib
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import Response

from proseforge.api.dependencies import (
    current_user,
    require_owned_project,
    unit_of_work,
)
from proseforge.application.auth.service import AuthUser
from proseforge.application.files.upload_service import (
    validate_upload,
    verify_download_digest,
)
from proseforge.infrastructure.blob.local import LocalBlobStore
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1", tags=["files"])


@router.post("/projects/{project_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_file(
    project_id: str,
    file: Annotated[UploadFile, File(...)],
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, str]:
    data = await file.read()
    filename = file.filename or "upload"
    try:
        validate_upload(filename, data, file.content_type or "application/octet-stream")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    digest = hashlib.sha256(data).hexdigest()
    async with uow:
        # Both modes may upload: chat attachments ride the same endpoint.
        project = await require_owned_project(uow, user.id, project_id)
        storage_key = await LocalBlobStore(request.app.state.settings.blob_root).put(data=data, media_type=file.content_type or "application/octet-stream")
        attachment = await uow.attachments.add(project.id, filename, digest, storage_key)
        await uow.commit()
        return {"id": attachment.id, "filename": attachment.filename, "storage_key": attachment.storage_key}


@router.get("/projects/{project_id}/files")
async def list_files(
    project_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> list[dict[str, str]]:
    async with uow:
        await require_owned_project(uow, user.id, project_id)
        return [{"id": row.id, "filename": row.filename, "sha256": row.sha256} for row in await uow.attachments.list_owned(project_id, user.id)]


@router.get("/files/{file_id}")
async def get_file(
    file_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, str]:
    async with uow:
        attachment = await uow.attachments.get_owned(file_id, user.id)
        if attachment is None:
            raise HTTPException(status_code=404, detail="file not found")
        return {"id": attachment.id, "project_id": attachment.project_id, "filename": attachment.filename, "sha256": attachment.sha256, "storage_key": attachment.storage_key}


@router.get("/files/{file_id}/download")
async def download_file(
    file_id: str,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> Response:
    async with uow:
        attachment = await uow.attachments.get_owned(file_id, user.id)
        if attachment is None:
            raise HTTPException(status_code=404, detail="file not found")
        # Read every needed attribute inside the session: after the block the
        # ORM instance is detached and attribute access raises DetachedInstanceError.
        storage_key, filename, sha256 = attachment.storage_key, attachment.filename, attachment.sha256
    try:
        data = await LocalBlobStore(request.app.state.settings.blob_root).get(storage_key)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="file content not found") from exc
    if not verify_download_digest(data, sha256):
        raise HTTPException(status_code=500, detail="file integrity check failed")
    ascii_name = filename.encode("ascii", "ignore").decode("ascii") or "download"
    safe_ascii = ascii_name.replace('"', "_").replace("\\", "_")
    disposition = f"attachment; filename=\"{safe_ascii}\"; filename*=UTF-8''{quote(filename, safe='')}"
    return Response(content=data, media_type="application/octet-stream", headers={"content-disposition": disposition})


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    file_id: str,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> None:
    async with uow:
        attachment = await uow.attachments.get_owned(file_id, user.id)
        if attachment is None:
            raise HTTPException(status_code=404, detail="file not found")
        storage_key = attachment.storage_key
        delete_blob = not await uow.attachments.has_other_blob_references(storage_key, attachment.id)
        await uow.session.delete(attachment)
        await uow.commit()
    if delete_blob:
        await LocalBlobStore(request.app.state.settings.blob_root).delete(storage_key)

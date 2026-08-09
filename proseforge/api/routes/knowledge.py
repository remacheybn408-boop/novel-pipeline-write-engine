"""Knowledge base document CRUD endpoints (work-mode projects only).

Reserved project-level skeleton: list/create/get/patch/delete under
/api/v1/projects/{project_id}/knowledge-base. Ownership is enforced twice —
require_work_project 404s foreign/missing/chat-mode projects, and every
repository method re-checks ProjectModel.owner_id.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from proseforge.api.dependencies import current_user, require_work_project, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.domain.knowledge.entity import KnowledgeDocument
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

router = APIRouter(prefix="/api/v1", tags=["knowledge"])


class KnowledgeDocumentCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = ""


class KnowledgeDocumentUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = None


def _response(document: KnowledgeDocument) -> dict[str, object]:
    return {
        "id": document.id,
        "project_id": document.project_id,
        "title": document.title,
        "content": document.content,
        "created_at": document.created_at.isoformat() if document.created_at else None,
        "updated_at": document.updated_at.isoformat() if document.updated_at else None,
    }


@router.get("/projects/{project_id}/knowledge-base")
async def list_knowledge_documents(
    project_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> list[dict[str, object]]:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        documents = await uow.knowledge.list_for_project(project_id, user.id)
        return [_response(document) for document in documents]


@router.post("/projects/{project_id}/knowledge-base", status_code=status.HTTP_201_CREATED)
async def create_knowledge_document(
    project_id: str,
    payload: KnowledgeDocumentCreateRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        title = payload.title.strip()
        if not title:
            # Whitespace-only titles pass min_length=1 but must not be stored.
            raise HTTPException(status_code=422, detail="title must not be blank")
        document = KnowledgeDocument.create(
            project_id=project_id, title=title, content=payload.content,
        )
        created = await uow.knowledge.create(document)
        await uow.commit()
        # Read back so the response carries the stored timestamps.
        stored = await uow.knowledge.get_owned(created.id, project_id, user.id)
        return _response(stored or created)


@router.get("/projects/{project_id}/knowledge-base/{document_id}")
async def get_knowledge_document(
    project_id: str,
    document_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        document = await uow.knowledge.get_owned(document_id, project_id, user.id)
        if document is None:
            raise HTTPException(status_code=404, detail="knowledge document not found")
        return _response(document)


@router.patch("/projects/{project_id}/knowledge-base/{document_id}")
async def update_knowledge_document(
    project_id: str,
    document_id: str,
    payload: KnowledgeDocumentUpdateRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        updates = payload.model_dump(exclude_unset=True)
        if "title" in updates and updates["title"] is not None:
            updates["title"] = str(updates["title"]).strip()
            if not updates["title"]:
                raise HTTPException(status_code=422, detail="title must not be blank")
        document = await uow.knowledge.update(document_id, project_id, user.id, **updates)
        await uow.commit()
        if document is None:
            raise HTTPException(status_code=404, detail="knowledge document not found")
        return _response(document)


@router.delete("/projects/{project_id}/knowledge-base/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_document(
    project_id: str,
    document_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> None:
    async with uow:
        await require_work_project(uow, user.id, project_id)
        deleted = await uow.knowledge.delete_owned(document_id, project_id, user.id)
        await uow.commit()
        if not deleted:
            raise HTTPException(status_code=404, detail="knowledge document not found")

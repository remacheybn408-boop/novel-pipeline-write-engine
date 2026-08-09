"""Knowledge base document persistence: owner-checked project CRUD."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.knowledge.entity import KnowledgeDocument
from proseforge.infrastructure.database.models.knowledge import KnowledgeDocumentModel
from proseforge.infrastructure.database.models.project import ProjectModel


class SqlAlchemyKnowledgeRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_for_project(self, project_id: str, owner_id: str) -> list[KnowledgeDocument]:
        rows = await self.session.scalars(
            select(KnowledgeDocumentModel)
            .join(ProjectModel, KnowledgeDocumentModel.project_id == ProjectModel.id)
            .where(KnowledgeDocumentModel.project_id == project_id, ProjectModel.owner_id == owner_id)
            .order_by(KnowledgeDocumentModel.created_at, KnowledgeDocumentModel.id)
        )
        return [self._entity(row) for row in rows]

    async def get_owned(self, document_id: str, project_id: str, owner_id: str) -> KnowledgeDocument | None:
        row = await self._row(document_id)
        if row is None or row.project_id != project_id:
            return None
        owner = await self.session.scalar(select(ProjectModel.owner_id).where(ProjectModel.id == project_id))
        if owner != owner_id:
            return None
        return self._entity(row)

    async def create(self, document: KnowledgeDocument) -> KnowledgeDocument:
        now = datetime.now(UTC)
        self.session.add(KnowledgeDocumentModel(
            id=document.id, project_id=document.project_id,
            title=document.title, content=document.content,
            created_at=now, updated_at=now,
        ))
        await self.session.flush()
        return document

    async def update(self, document_id: str, project_id: str, owner_id: str, *, title: str | None = None, content: str | None = None) -> KnowledgeDocument | None:
        row = await self._row(document_id)
        if row is None or row.project_id != project_id:
            return None
        owner = await self.session.scalar(select(ProjectModel.owner_id).where(ProjectModel.id == project_id))
        if owner != owner_id:
            return None
        if title is not None:
            row.title = title
        if content is not None:
            row.content = content
        row.updated_at = datetime.now(UTC)
        await self.session.flush()
        return self._entity(row)

    async def delete_owned(self, document_id: str, project_id: str, owner_id: str) -> bool:
        row = await self._row(document_id)
        if row is None or row.project_id != project_id:
            return False
        owner = await self.session.scalar(select(ProjectModel.owner_id).where(ProjectModel.id == project_id))
        if owner != owner_id:
            return False
        await self.session.execute(delete(KnowledgeDocumentModel).where(KnowledgeDocumentModel.id == document_id))
        await self.session.flush()
        return True

    async def _row(self, document_id: str) -> KnowledgeDocumentModel | None:
        return await self.session.scalar(select(KnowledgeDocumentModel).where(KnowledgeDocumentModel.id == document_id))

    @staticmethod
    def _entity(row: KnowledgeDocumentModel) -> KnowledgeDocument:
        return KnowledgeDocument(
            id=row.id,
            project_id=row.project_id,
            title=row.title,
            content=row.content,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

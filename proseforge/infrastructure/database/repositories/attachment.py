from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.remaining import (
    ArtifactModel,
    AttachmentModel,
)


class SqlAlchemyAttachmentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, project_id: str, filename: str, sha256: str, storage_key: str, message_id: str | None = None) -> AttachmentModel:
        attachment = AttachmentModel(id=new_id(), project_id=project_id, filename=filename, sha256=sha256, storage_key=storage_key, message_id=message_id)
        self.session.add(attachment)
        await self.session.flush()
        return attachment

    async def list_owned(self, project_id: str, owner_id: str) -> list[AttachmentModel]:
        rows = await self.session.scalars(
            select(AttachmentModel)
            .join(ProjectModel, ProjectModel.id == AttachmentModel.project_id)
            .where(AttachmentModel.project_id == project_id, ProjectModel.owner_id == owner_id)
            .order_by(AttachmentModel.filename, AttachmentModel.id)
        )
        return list(rows)

    async def list_all(self) -> list[AttachmentModel]:
        rows = await self.session.scalars(select(AttachmentModel).order_by(AttachmentModel.id))
        return list(rows)

    async def get_owned(self, attachment_id: str, owner_id: str) -> AttachmentModel | None:
        return await self.session.scalar(
            select(AttachmentModel)
            .join(ProjectModel, ProjectModel.id == AttachmentModel.project_id)
            .where(AttachmentModel.id == attachment_id, ProjectModel.owner_id == owner_id)
        )

    async def has_other_blob_references(self, storage_key: str, excluding_attachment_id: str) -> bool:
        attachment_count = await self.session.scalar(
            select(func.count(AttachmentModel.id)).where(
                AttachmentModel.storage_key == storage_key,
                AttachmentModel.id != excluding_attachment_id,
            )
        )
        artifact_count = await self.session.scalar(
            select(func.count(ArtifactModel.id)).where(ArtifactModel.storage_key == storage_key)
        )
        return bool(attachment_count or artifact_count)

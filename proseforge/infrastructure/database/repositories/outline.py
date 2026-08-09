from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.remaining import (
    OutlineModel,
    OutlineVersionModel,
)


class SqlAlchemyOutlineRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, project_id: str, title: str, payload: dict[str, object], missing: list[str]) -> OutlineModel:
        outline = OutlineModel(
            id=new_id(), project_id=project_id, title=title, payload=json.dumps(payload, ensure_ascii=False),
            missing_questions=json.dumps(missing, ensure_ascii=False), status="NEEDS_ANSWERS" if missing else "READY_TO_CONFIRM",
        )
        self.session.add(outline)
        await self.session.flush()
        await self.save_version(outline.id, payload)
        return outline

    async def get_owned(self, outline_id: str, owner_id: str) -> OutlineModel | None:
        return await self.session.scalar(
            select(OutlineModel).join(ProjectModel, ProjectModel.id == OutlineModel.project_id).where(
                OutlineModel.id == outline_id, ProjectModel.owner_id == owner_id
            )
        )

    async def list_owned(self, project_id: str, owner_id: str) -> list[OutlineModel]:
        rows = await self.session.scalars(
            select(OutlineModel).join(ProjectModel, ProjectModel.id == OutlineModel.project_id).where(
                OutlineModel.project_id == project_id, ProjectModel.owner_id == owner_id
            ).order_by(OutlineModel.id)
        )
        return list(rows)

    async def update(self, outline: OutlineModel, payload: dict[str, object], missing: list[str]) -> OutlineModel:
        outline.payload = json.dumps(payload, ensure_ascii=False)
        outline.missing_questions = json.dumps(missing, ensure_ascii=False)
        outline.status = "NEEDS_ANSWERS" if missing else "READY_TO_CONFIRM"
        await self.save_version(outline.id, payload)
        await self.session.flush()
        return outline

    async def confirm(self, outline: OutlineModel) -> OutlineModel:
        outline.confirmed = True
        outline.status = "CONFIRMED"
        await self.session.flush()
        return outline

    async def save_version(self, outline_id: str, payload: dict[str, object]) -> OutlineVersionModel:
        maximum = await self.session.scalar(select(func.max(OutlineVersionModel.version_no)).where(OutlineVersionModel.outline_id == outline_id))
        version = OutlineVersionModel(id=new_id(), outline_id=outline_id, version_no=(maximum or 0) + 1, payload=json.dumps(payload, ensure_ascii=False))
        self.session.add(version)
        await self.session.flush()
        return version

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.remaining import (
    ContextItemModel,
    ContextSnapshotModel,
)


class SqlAlchemyContextRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_owned(self, project_id: str, owner_id: str) -> list[ContextItemModel]:
        rows = await self.session.scalars(
            select(ContextItemModel).join(ProjectModel, ProjectModel.id == ContextItemModel.project_id).where(
                ContextItemModel.project_id == project_id, ProjectModel.owner_id == owner_id
            ).order_by(ContextItemModel.priority.desc(), ContextItemModel.id)
        )
        return list(rows)

    async def get_owned(self, item_id: str, owner_id: str) -> ContextItemModel | None:
        return await self.session.scalar(
            select(ContextItemModel).join(ProjectModel, ProjectModel.id == ContextItemModel.project_id).where(
                ContextItemModel.id == item_id, ProjectModel.owner_id == owner_id
            )
        )

    async def add(self, project_id: str, source_type: str, content: str, source_id: str = "manual") -> ContextItemModel:
        item = ContextItemModel(id=new_id(), project_id=project_id, source_type=source_type, source_id=source_id, content=content, provenance=json.dumps({"source": source_type}))
        self.session.add(item)
        await self.session.flush()
        return item

    async def snapshot(self, project_id: str, items: list[ContextItemModel]) -> ContextSnapshotModel:
        payload = {"items": [{"id": item.id, "source_type": item.source_type, "content": item.content, "pinned": item.pinned, "priority": item.priority} for item in items]}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        snapshot = ContextSnapshotModel(id=new_id(), project_id=project_id, snapshot_hash=hashlib.sha256(encoded.encode()).hexdigest(), payload=encoded)
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    async def get_snapshot_owned(self, snapshot_id: str, owner_id: str) -> ContextSnapshotModel | None:
        return await self.session.scalar(
            select(ContextSnapshotModel)
            .join(ProjectModel, ProjectModel.id == ContextSnapshotModel.project_id)
            .where(ContextSnapshotModel.id == snapshot_id, ProjectModel.owner_id == owner_id)
        )

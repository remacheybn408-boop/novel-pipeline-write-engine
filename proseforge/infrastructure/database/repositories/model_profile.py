from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.remaining import ModelProfileModel


class SqlAlchemyModelProfileRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_for_user(self, user_id: str) -> list[dict[str, object]]:
        rows = await self.session.scalars(select(ModelProfileModel).where(ModelProfileModel.user_id == user_id).order_by(ModelProfileModel.name))
        return [self._entity(row) for row in rows]

    async def create(self, user_id: str, name: str, config: dict[str, object]) -> dict[str, object]:
        row = ModelProfileModel(id=new_id(), user_id=user_id, name=name, config=json.dumps(config, ensure_ascii=False))
        self.session.add(row)
        await self.session.flush()
        return self._entity(row)

    async def get_owned(self, user_id: str, profile_id: str) -> ModelProfileModel | None:
        return await self.session.scalar(select(ModelProfileModel).where(ModelProfileModel.id == profile_id, ModelProfileModel.user_id == user_id))

    async def update(self, row: ModelProfileModel, name: str | None, config: dict[str, object] | None) -> dict[str, object]:
        if name is not None:
            row.name = name
        if config is not None:
            row.config = json.dumps(config, ensure_ascii=False)
        await self.session.flush()
        return self._entity(row)

    async def delete(self, row: ModelProfileModel) -> None:
        await self.session.delete(row)
        await self.session.flush()

    @staticmethod
    def _entity(row: ModelProfileModel) -> dict[str, object]:
        return {"id": row.id, "name": row.name, "config": json.loads(row.config or "{}")}

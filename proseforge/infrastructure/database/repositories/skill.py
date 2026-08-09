from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.plugin.entity import Skill
from proseforge.infrastructure.database.models.plugin import UserSkillModel


class SqlAlchemySkillRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, skill: Skill) -> Skill:
        self.session.add(UserSkillModel(**skill.__dict__))
        await self.session.flush()
        return skill

    async def get_for_user(self, user_id: str, skill_id: str) -> Skill | None:
        row = await self.session.scalar(
            select(UserSkillModel).where(UserSkillModel.user_id == user_id, UserSkillModel.id == skill_id)
        )
        return None if row is None else self._entity(row)

    async def get_by_name(self, user_id: str, name: str) -> Skill | None:
        row = await self.session.scalar(
            select(UserSkillModel).where(UserSkillModel.user_id == user_id, UserSkillModel.name == name)
        )
        return None if row is None else self._entity(row)

    async def list_for_user(self, user_id: str, *, enabled_only: bool = False) -> list[Skill]:
        query = select(UserSkillModel).where(UserSkillModel.user_id == user_id)
        if enabled_only:
            query = query.where(UserSkillModel.enabled.is_(True))
        rows = await self.session.scalars(query.order_by(UserSkillModel.created_at, UserSkillModel.id))
        return [self._entity(row) for row in rows]

    async def count_for_user(self, user_id: str) -> int:
        return int(await self.session.scalar(select(func.count(UserSkillModel.id)).where(UserSkillModel.user_id == user_id)) or 0)

    async def update(self, user_id: str, skill_id: str, *, name: str | None = None, description: str | None = None, content: str | None = None, enabled: bool | None = None) -> Skill | None:
        row = await self.session.scalar(
            select(UserSkillModel).where(UserSkillModel.user_id == user_id, UserSkillModel.id == skill_id)
        )
        if row is None:
            return None
        for field, value in (("name", name), ("description", description), ("content", content), ("enabled", enabled)):
            if value is not None:
                setattr(row, field, value)
        await self.session.flush()
        return self._entity(row)

    async def delete(self, user_id: str, skill_id: str) -> bool:
        row = await self.session.scalar(
            select(UserSkillModel).where(UserSkillModel.user_id == user_id, UserSkillModel.id == skill_id)
        )
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.flush()
        return True

    @staticmethod
    def _entity(row: UserSkillModel) -> Skill:
        return Skill(
            id=row.id,
            user_id=row.user_id,
            name=row.name,
            description=row.description,
            content=row.content,
            enabled=row.enabled,
            created_at=row.created_at,
        )

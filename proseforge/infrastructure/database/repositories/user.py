from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.auth import UserModel


class SqlAlchemyUserRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def count(self) -> int:
        return int(await self.session.scalar(select(func.count()).select_from(UserModel)) or 0)

    async def get_by_email(self, email: str) -> UserModel | None:
        return await self.session.scalar(select(UserModel).where(func.lower(UserModel.email) == email.lower()))

    async def get_by_id(self, user_id: str) -> UserModel | None:
        return await self.session.get(UserModel, user_id)

    async def create(self, email: str, password_hash: str, role: str = "USER") -> UserModel:
        user = UserModel(id=new_id(), email=email.lower(), password_hash=password_hash, role=role)
        self.session.add(user)
        await self.session.flush()
        return user

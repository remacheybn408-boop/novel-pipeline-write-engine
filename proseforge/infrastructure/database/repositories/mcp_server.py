from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.plugin.entity import McpServer
from proseforge.infrastructure.database.models.plugin import UserMcpServerModel


class SqlAlchemyMcpServerRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, server: McpServer) -> McpServer:
        self.session.add(UserMcpServerModel(**server.__dict__))
        await self.session.flush()
        return server

    async def get_for_user(self, user_id: str, server_id: str) -> McpServer | None:
        row = await self.session.scalar(
            select(UserMcpServerModel).where(UserMcpServerModel.user_id == user_id, UserMcpServerModel.id == server_id)
        )
        return None if row is None else self._entity(row)

    async def get_by_name(self, user_id: str, name: str) -> McpServer | None:
        row = await self.session.scalar(
            select(UserMcpServerModel).where(UserMcpServerModel.user_id == user_id, UserMcpServerModel.name == name)
        )
        return None if row is None else self._entity(row)

    async def list_for_user(self, user_id: str) -> list[McpServer]:
        rows = await self.session.scalars(
            select(UserMcpServerModel).where(UserMcpServerModel.user_id == user_id).order_by(UserMcpServerModel.created_at, UserMcpServerModel.id)
        )
        return [self._entity(row) for row in rows]

    async def count_for_user(self, user_id: str) -> int:
        return int(await self.session.scalar(select(func.count(UserMcpServerModel.id)).where(UserMcpServerModel.user_id == user_id)) or 0)

    async def update(self, user_id: str, server_id: str, *, name: str | None = None, transport: str | None = None, url: str | None = None, encrypted_headers: str | None = None, enabled: bool | None = None) -> McpServer | None:
        row = await self.session.scalar(
            select(UserMcpServerModel).where(UserMcpServerModel.user_id == user_id, UserMcpServerModel.id == server_id)
        )
        if row is None:
            return None
        for field, value in (("name", name), ("transport", transport), ("url", url), ("encrypted_headers", encrypted_headers), ("enabled", enabled)):
            if value is not None:
                setattr(row, field, value)
        await self.session.flush()
        return self._entity(row)

    async def delete(self, user_id: str, server_id: str) -> bool:
        row = await self.session.scalar(
            select(UserMcpServerModel).where(UserMcpServerModel.user_id == user_id, UserMcpServerModel.id == server_id)
        )
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.flush()
        return True

    @staticmethod
    def _entity(row: UserMcpServerModel) -> McpServer:
        return McpServer(
            id=row.id,
            user_id=row.user_id,
            name=row.name,
            transport=row.transport,
            url=row.url,
            encrypted_headers=row.encrypted_headers,
            enabled=row.enabled,
            created_at=row.created_at,
        )

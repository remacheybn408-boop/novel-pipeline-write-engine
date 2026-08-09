from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.remaining import ProviderCredentialModel


class SqlAlchemyCredentialRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, user_id: str, provider: str, encrypted_payload: str, record_id: str | None = None) -> ProviderCredentialModel:
        record = ProviderCredentialModel(id=record_id or new_id(), user_id=user_id, provider=provider, encrypted_payload=encrypted_payload)
        self.session.add(record)
        await self.session.flush()
        return record

    async def upsert(self, user_id: str, provider: str, encrypted_payload: str, record_id: str | None = None) -> ProviderCredentialModel:
        values = {"id": record_id or new_id(), "user_id": user_id, "provider": provider, "encrypted_payload": encrypted_payload}
        bind = self.session.bind
        if bind is not None and bind.dialect.name == "postgresql":
            statement = pg_insert(ProviderCredentialModel).values(**values).on_conflict_do_update(
                constraint="uq_provider_credentials_user_provider",
                set_={"encrypted_payload": encrypted_payload},
            )
            await self.session.execute(statement)
        elif bind is not None and bind.dialect.name == "sqlite":
            statement = sqlite_insert(ProviderCredentialModel).values(**values).on_conflict_do_update(
                index_elements=("user_id", "provider"),
                set_={"encrypted_payload": encrypted_payload},
            )
            await self.session.execute(statement)
        else:
            record = await self.get_for_user(user_id, provider)
            if record is None:
                return await self.create(user_id, provider, encrypted_payload, record_id)
            record.encrypted_payload = encrypted_payload
        await self.session.flush()
        record = await self.get_for_user(user_id, provider)
        if record is None:
            raise RuntimeError("credential upsert did not persist a record")
        return record

    async def list_for_user(self, user_id: str) -> list[ProviderCredentialModel]:
        rows = await self.session.scalars(
            select(ProviderCredentialModel).where(ProviderCredentialModel.user_id == user_id).order_by(ProviderCredentialModel.provider)
        )
        return list(rows)

    async def list_all(self) -> list[ProviderCredentialModel]:
        rows = await self.session.scalars(select(ProviderCredentialModel).order_by(ProviderCredentialModel.user_id, ProviderCredentialModel.provider))
        return list(rows)

    async def get_for_user(self, user_id: str, provider: str) -> ProviderCredentialModel | None:
        return await self.session.scalar(
            select(ProviderCredentialModel).where(
                ProviderCredentialModel.user_id == user_id,
                ProviderCredentialModel.provider == provider,
            )
        )

    async def delete_owned(self, credential_id: str, user_id: str) -> bool:
        """Delete a credential by id, scoped to its owner.

        Nothing references credentials by id (all lookups are by
        (user_id, provider)), so deletion cannot leave dangling references;
        returns False when the row is missing or belongs to another user.
        """
        result = await self.session.execute(
            delete(ProviderCredentialModel).where(
                ProviderCredentialModel.id == credential_id,
                ProviderCredentialModel.user_id == user_id,
            )
        )
        await self.session.flush()
        return bool(result.rowcount)

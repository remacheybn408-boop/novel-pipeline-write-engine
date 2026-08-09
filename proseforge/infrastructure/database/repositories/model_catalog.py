from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.common.ids import new_id
from proseforge.domain.model.modality import (
    is_text_or_omni_model,
    supports_vision_by_name,
)
from proseforge.domain.ports.model_provider import ProviderModel
from proseforge.infrastructure.database.models.remaining import ModelCatalogModel


class SqlAlchemyModelCatalogRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, provider: str, model_id: str) -> ProviderModel | None:
        row = await self.session.scalar(
            select(ModelCatalogModel).where(
                ModelCatalogModel.provider == provider,
                ModelCatalogModel.model_id == model_id,
            )
        )
        return self._entity(row) if row is not None else None

    async def list(self, provider: str | None = None, search: str | None = None, available_only: bool = False) -> list[ProviderModel]:
        query = select(ModelCatalogModel).order_by(ModelCatalogModel.provider, ModelCatalogModel.model_id)
        if provider:
            query = query.where(ModelCatalogModel.provider == provider)
        if search:
            query = query.where(ModelCatalogModel.model_id.ilike(f"%{search}%"))
        rows = await self.session.scalars(query)
        entities = [self._entity(row) for row in rows]
        return [item for item in entities if not available_only or item.capabilities.get("availability", "available") == "available"]

    async def upsert(self, models: list[ProviderModel], owner_id: str | None = None) -> None:
        for model in models:
            # Intake hygiene (domain/model/modality.py): the catalog feeds
            # text chat generation, so video/TTS/image/embedding/rerank
            # models are dropped at this choke point — discovery sync and
            # manual registration alike.
            if not is_text_or_omni_model(model.model_id):
                continue
            row = await self.session.scalar(
                select(ModelCatalogModel).where(
                    ModelCatalogModel.provider == model.provider,
                    ModelCatalogModel.model_id == model.model_id,
                )
            )
            capabilities = dict(model.capabilities)
            if supports_vision_by_name(model.model_id):
                # Vendors never report a vision flag; name-derived stamp.
                # setdefault keeps any provider/manual-supplied value.
                capabilities.setdefault("vision", True)
            capabilities.setdefault("display_name", model.display_name)
            capabilities.setdefault("availability", "available")
            if model.context_window is not None:
                capabilities.setdefault("context_window", model.context_window)
            if model.max_output_tokens is not None:
                capabilities.setdefault("max_output_tokens", model.max_output_tokens)
            payload = json.dumps(capabilities, ensure_ascii=False)
            if row is None:
                self.session.add(ModelCatalogModel(id=new_id(), provider=model.provider, model_id=model.model_id, capabilities=payload, owner_id=owner_id))
            else:
                existing = json.loads(row.capabilities or "{}")
                if existing.get("manual"):
                    # User-registered models keep their own display name and
                    # context window; discovery sync must not clobber them.
                    continue
                row.capabilities = payload
                if owner_id is not None:
                    # A manual add over an existing synced row converts it to
                    # an owned manual row.
                    row.owner_id = owner_id
        await self.session.flush()

    async def get_row(self, provider: str, model_id: str) -> ModelCatalogModel | None:
        """Raw catalog row (ownership metadata included) for delete checks."""
        return await self.session.scalar(
            select(ModelCatalogModel).where(
                ModelCatalogModel.provider == provider,
                ModelCatalogModel.model_id == model_id,
            )
        )

    async def delete(self, row: ModelCatalogModel) -> None:
        await self.session.delete(row)
        await self.session.flush()

    async def mark_unavailable(self, provider: str, model_ids: set[str]) -> None:
        if not model_ids:
            return
        rows = await self.session.scalars(select(ModelCatalogModel).where(ModelCatalogModel.provider == provider))
        for row in rows:
            if row.model_id not in model_ids:
                capabilities = json.loads(row.capabilities or "{}")
                if capabilities.get("manual"):
                    continue
                capabilities["availability"] = "unavailable"
                row.capabilities = json.dumps(capabilities, ensure_ascii=False)
        await self.session.flush()

    async def mark_provider_unavailable(self, provider: str) -> None:
        """Mark every synced (non-manual) model of a provider unavailable.

        Used when the last credential for a provider is deleted: the models
        disappear from pickers (available_only listings) while the catalog
        rows stay as window history. Manual entries are user-managed and
        left untouched.
        """
        rows = await self.session.scalars(select(ModelCatalogModel).where(ModelCatalogModel.provider == provider))
        for row in rows:
            capabilities = json.loads(row.capabilities or "{}")
            if capabilities.get("manual"):
                continue
            capabilities["availability"] = "unavailable"
            row.capabilities = json.dumps(capabilities, ensure_ascii=False)
        await self.session.flush()

    @staticmethod
    def _entity(row: ModelCatalogModel) -> ProviderModel:
        capabilities = json.loads(row.capabilities or "{}")
        return ProviderModel(
            provider=row.provider,
            model_id=row.model_id,
            display_name=str(capabilities.pop("display_name", row.model_id)),
            capabilities=capabilities,
            context_window=capabilities.pop("context_window", None),
            max_output_tokens=capabilities.pop("max_output_tokens", None),
            owner_id=row.owner_id,
        )

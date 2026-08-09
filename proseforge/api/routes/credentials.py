from __future__ import annotations

import base64
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from proseforge.api.dependencies import current_user, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.application.models.cluster_config import scrub_stale_cluster_refs
from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.security.credential_cipher import (
    CredentialCipher,
    derive_key,
)
from proseforge.infrastructure.security.endpoint_policy import EndpointPolicy

router = APIRouter(prefix="/api/v1", tags=["credentials"])


class CredentialRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    api_key: str = Field(min_length=1, max_length=1000)
    base_url: str | None = None
    allow_local: bool = False


def cipher_for(request: Request) -> CredentialCipher:
    raw = request.app.state.settings.master_key.get_secret_value()
    return CredentialCipher(derive_key(raw))


@router.post("/credentials", status_code=status.HTTP_201_CREATED)
async def save_credential(
    payload: CredentialRequest,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, str]:
    if payload.base_url:
        try:
            EndpointPolicy(tuple(request.app.state.settings.allowed_local_provider_hosts)).validate(payload.base_url, allow_local=payload.allow_local)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    async with uow:
        existing = await uow.credentials.get_for_user(user.id, payload.provider)
        record_id = existing.id if existing else new_id()
        associated_data = f"{user.id}:{payload.provider}:{record_id}".encode()
        encrypted = cipher_for(request).encrypt(json.dumps({"api_key": payload.api_key, "base_url": payload.base_url}).encode(), associated_data=associated_data)
        record = await uow.credentials.upsert(user.id, payload.provider, base64.b64encode(encrypted).decode(), record_id)
        await uow.commit()
    await request.app.state.queue.enqueue("proseforge.providers.sync_all_models", {})
    return {"id": record.id, "provider": record.provider, "masked_key": f"{payload.api_key[:3]}****{payload.api_key[-4:]}" if len(payload.api_key) > 7 else "****"}


@router.get("/credentials")
async def list_credentials(
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> list[dict[str, str]]:
    async with uow:
        return [{"id": row.id, "provider": row.provider, "masked_key": "configured"} for row in await uow.credentials.list_for_user(user.id)]


@router.delete("/credentials/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(
    credential_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> None:
    async with uow:
        # Uniform 404 for missing and foreign ids (no ownership leak).
        owned = {row.id: row for row in await uow.credentials.list_for_user(user.id)}
        record = owned.get(credential_id)
        if record is None:
            raise HTTPException(status_code=404, detail="credential not found")
        provider = record.provider
        if not await uow.credentials.delete_owned(credential_id, user.id):
            raise HTTPException(status_code=404, detail="credential not found")
        # Soft cascade: when the last credential for a provider is gone
        # (across all users — the catalog is provider-level, shared), hide
        # its synced models from pickers so the chat model selector stays in
        # sync. Manual entries and other users' providers are untouched.
        remaining = await uow.credentials.list_all()
        if not any(row.provider == provider for row in remaining):
            await uow.model_catalog.mark_provider_unavailable(provider)
            # Provider gone everywhere: every user's cluster refs to its
            # models are stale.
            await scrub_stale_cluster_refs(uow, None)
        else:
            # Other users may still use this provider; only this user's pool
            # shrank.
            await scrub_stale_cluster_refs(uow, user.id)
        await uow.commit()

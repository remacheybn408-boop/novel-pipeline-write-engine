from __future__ import annotations

import base64
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from proseforge.api.dependencies import current_user, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.application.models.cluster_config import scrub_stale_cluster_refs
from proseforge.domain.model.capabilities import capabilities_from_model
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.security.credential_cipher import (
    CredentialCipher,
    derive_key,
)
from proseforge.providers.factory import build_provider

router = APIRouter(prefix="/api/v1", tags=["providers"])

# Third-party aggregators / inference clouds (they only re-expose official
# models through their own API). Hidden from the provider list per product
# decision: official first-party providers are enough. Registered providers
# stay functional for any legacy credentials; they are just no longer
# offered in the UI.
THIRD_PARTY_PROVIDERS = frozenset({
    "openrouter",
    "groq",
    "together",
    "fireworks",
    "perplexity",
    "cerebras",
    "sambanova",
    "deepinfra",
    "novita",
    "siliconflow",
})


class CustomModelRequest(BaseModel):
    # Context window is product-managed (min(700K, real window) * 0.65 input
    # budget); users no longer type it in. Extra fields are ignored.
    provider: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=200)
    display_name: str | None = None
    capabilities: dict[str, object] = Field(default_factory=dict)


@router.get("/providers")
async def list_providers(request: Request, user: Annotated[AuthUser, Depends(current_user)]) -> list[dict[str, object]]:
    del user
    return [{"id": provider_id, "status": "configured"} for provider_id in request.app.state.provider_registry.ids() if provider_id not in THIRD_PARTY_PROVIDERS]


@router.get("/models")
async def list_models(
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
    provider: str | None = None,
    q: str | None = None,
    available_only: bool = True,
) -> list[dict[str, object]]:
    del request
    async with uow:
        # Long-term invariant: a model is listable only while its provider
        # still has at least one credential for the current user. Generation
        # paths resolve credentials per (user, provider), so models from a
        # provider the user never credentialed are unusable anyway — never
        # let them (or stale catalog rows) surface in the pickers.
        credentialed = {row.provider for row in await uow.credentials.list_for_user(user.id)}
        rows = []
        for model in await uow.model_catalog.list(provider, q, available_only):
            manual = bool(model.capabilities.get("manual"))
            if manual and model.owner_id is not None:
                # Manual rows are per-user since migration 0050: only the
                # owner's rows are visible.
                if model.owner_id != user.id:
                    continue
            elif model.provider not in credentialed:
                # Stale rows: synced models of a provider whose credentials
                # are all gone — and legacy shared manual rows (owner_id
                # NULL, pre-0050) follow the same gate. Legacy rows are not
                # user-deletable, so without this gate a deleted provider's
                # models would haunt every picker forever.
                continue
            resolved = capabilities_from_model(model)
            rows.append({"provider": model.provider, "model_id": model.model_id, "display_name": model.display_name, "capabilities": model.capabilities, "context_window": resolved.context_window, "max_output_tokens": resolved.max_output_tokens, "reasoning_levels": resolved.reasoning_profile.supported_levels if resolved.reasoning_profile else ["auto"], "owner_id": model.owner_id, "legacy_shared": manual and model.owner_id is None})
        return rows


@router.post("/models", status_code=201)
async def add_custom_model(
    payload: CustomModelRequest,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    capabilities = {**payload.capabilities, "manual": True, "availability": "available"}
    from proseforge.domain.ports.model_provider import ProviderModel
    # No context_window: capabilities_from_model resolves the real window
    # (verified known_windows table first, catalog column as history).
    model = ProviderModel(payload.provider, payload.model_id, payload.display_name or payload.model_id, capabilities, owner_id=user.id)
    async with uow:
        await uow.model_catalog.upsert([model], owner_id=user.id)
        await uow.commit()
        return {"provider": model.provider, "model_id": model.model_id, "display_name": model.display_name, "capabilities": capabilities, "context_window": model.context_window, "owner_id": user.id}


@router.delete("/models/{provider}/{model_id}", status_code=204)
async def delete_custom_model(
    provider: str,
    model_id: str,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> None:
    """Delete a manually registered model owned by the current user.

    Only owned manual rows are deletable: synced (discovery) rows and legacy
    shared manual rows (owner_id NULL, pre-0050) are managed by sync /
    backward-compat rules, not by users. Rows owned by someone else are
    answered 404 to avoid leaking their existence.
    """
    async with uow:
        row = await uow.model_catalog.get_row(provider, model_id)
        if row is None or (row.owner_id is not None and row.owner_id != user.id):
            raise HTTPException(status_code=404, detail="model not found")
        if not json.loads(row.capabilities or "{}").get("manual"):
            raise HTTPException(status_code=403, detail="synced models are managed by provider sync and cannot be deleted")
        if row.owner_id is None:
            raise HTTPException(status_code=403, detail="legacy shared manual models cannot be deleted")
        await uow.model_catalog.delete(row)
        # Keep cluster configs in sync: refs to the deleted model reset to
        # auto (global preference + this user's project overrides).
        await scrub_stale_cluster_refs(uow, user.id)
        await uow.commit()


@router.post("/providers/{provider_id}/sync-models")
async def sync_models(
    provider_id: str,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        credential = await uow.credentials.get_for_user(user.id, provider_id)
        if credential is None:
            raise HTTPException(status_code=400, detail="provider credentials are not configured")
        raw_key = request.app.state.settings.master_key.get_secret_value()
        key = derive_key(raw_key)
        associated = f"{user.id}:{provider_id}:{credential.id}".encode()
        try:
            payload = json.loads(CredentialCipher(key).decrypt(base64.b64decode(credential.encrypted_payload), associated_data=associated))
            provider = build_provider(provider_id, payload["api_key"], payload.get("base_url"))
            models = await provider.list_models()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"provider not registered: {provider_id}") from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"provider model discovery failed: {type(exc).__name__}") from exc
        await uow.model_catalog.upsert([model for model in models])
        await uow.model_catalog.mark_unavailable(provider_id, {model.model_id for model in models})
        await uow.commit()
        return {"provider": provider_id, "count": len(models), "models": [model.model_id for model in models]}


@router.post("/providers/{provider_id}/probe")
async def probe_provider(
    provider_id: str,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        credential = await uow.credentials.get_for_user(user.id, provider_id)
        if credential is None:
            raise HTTPException(status_code=400, detail="provider credentials are not configured")
        raw_key = request.app.state.settings.master_key.get_secret_value()
        key = derive_key(raw_key)
        associated = f"{user.id}:{provider_id}:{credential.id}".encode()
        try:
            payload = json.loads(CredentialCipher(key).decrypt(base64.b64decode(credential.encrypted_payload), associated_data=associated))
            provider = build_provider(provider_id, payload["api_key"], payload.get("base_url"))
            result = await provider.validate_credentials()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"provider not registered: {provider_id}") from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"provider probe failed: {type(exc).__name__}") from exc
        return {"provider": provider_id, **result}

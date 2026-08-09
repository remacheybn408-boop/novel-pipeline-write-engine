from __future__ import annotations

import base64
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from proseforge.api.dependencies import current_user, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.application.models.reasoning_policy import resolve_reasoning
from proseforge.domain.model.capabilities import ReasoningLevel, capabilities_from_model
from proseforge.infrastructure.security.credential_cipher import (
    CredentialCipher,
    derive_key,
)
from proseforge.providers.factory import build_provider

router = APIRouter(prefix="/api/v2", tags=["model-capabilities"])


class ReasoningValidation(BaseModel):
    provider: str
    model_id: str
    level: str = "auto"
    probe: bool = False


def _reasoning_error(message: str, supported_levels: list[str]) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "code": "UNSUPPORTED_REASONING_LEVEL",
            "message": message,
            "retryable": False,
            "details": {"supported_levels": supported_levels},
        },
    )


async def _probe_warnings(settings, uow, user: AuthUser, provider_id: str) -> list[str]:
    """真实的 provider 连通性探测（仅 probe=true 时调用）。

    失败只回类型名——异常文本可能带 base_url/密钥片段，绝不写进 warnings。
    """
    credential = await uow.credentials.get_for_user(user.id, provider_id)
    if credential is None:
        return ["provider probe skipped: credentials are not configured"]
    raw_key = settings.master_key.get_secret_value()
    key = derive_key(raw_key)
    try:
        associated = f"{user.id}:{provider_id}:{credential.id}".encode()
        secret = json.loads(CredentialCipher(key).decrypt(base64.b64decode(credential.encrypted_payload), associated_data=associated))
        provider = build_provider(provider_id, secret["api_key"], secret.get("base_url"))
        await provider.validate_credentials()
    except Exception as exc:
        return [f"provider probe failed: {type(exc).__name__}"]
    return []


@router.get("/models")
async def models(user: Annotated[AuthUser, Depends(current_user)], uow=Depends(unit_of_work), provider: str | None = None, capability: str | None = None):
    del user
    async with uow:
        values = await uow.model_catalog.list(provider, None, True)
        return [{"provider": item.provider, "model_id": item.model_id, "capabilities": item.capabilities, "context_window": item.context_window, "max_output_tokens": item.max_output_tokens} for item in values if not capability or item.capabilities.get(capability)]


@router.get("/models/{provider}/{model_id}/capabilities")
async def model_capabilities(provider: str, model_id: str, user: Annotated[AuthUser, Depends(current_user)], uow=Depends(unit_of_work)):
    del user
    async with uow:
        values = await uow.model_catalog.list(provider, model_id, False)
        if not values:
            raise HTTPException(status_code=404, detail="model not found")
        caps = capabilities_from_model(values[0])
        return {"provider": provider, "model_id": model_id, **caps.__dict__}


@router.post("/model-resolutions/validate")
async def validate_resolution(payload: ReasoningValidation, request: Request, user: Annotated[AuthUser, Depends(current_user)], uow=Depends(unit_of_work)):
    async with uow:
        values = await uow.model_catalog.list(payload.provider, payload.model_id, False)
        if not values:
            raise HTTPException(status_code=404, detail="model not found")
        caps = capabilities_from_model(values[0])
        try:
            policy = resolve_reasoning(payload.level, caps)
        except ValueError as exc:
            try:
                ReasoningLevel(payload.level)
            except ValueError:
                raise _reasoning_error(f"Unknown reasoning level {payload.level!r}.", [item.value for item in ReasoningLevel]) from exc
            raise _reasoning_error(str(exc), [ReasoningLevel.AUTO.value]) from exc
        warnings = list(policy["warnings"])
        if payload.probe:  # probe=false（默认）绝不呼 provider
            warnings.extend(await _probe_warnings(request.app.state.settings, uow, user, payload.provider))
        return {
            "provider": payload.provider,
            "model_id": payload.model_id,
            "normalized_level": policy["level"],
            "provider_parameter": policy.get("provider_parameter"),
            "context_window": caps.context_window,
            "warnings": warnings,
        }

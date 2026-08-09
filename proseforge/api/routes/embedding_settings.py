"""Embedding configuration endpoints (narrative RAG phase 2).

Three engines: "local" (built-in ONNX model, the default), "api" (vendor
HTTP embedding), "off" (keyword-only indexing, no vectors). The choice is
stored in user_preferences under the key "embedding"; records without an
"engine" field predate the three-engine switch and read as "api".

Switching to a different embedding identity while indexed chunks exist is
rejected (409) unless force=true — mixed embedding spaces would silently
corrupt retrieval. force clears the user's retrieval documents/chunks and
re-enqueues indexing for every chapter with an active version in their
work-mode projects, same as the approve-proposal enqueue path.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from proseforge.api.dependencies import current_user, unit_of_work
from proseforge.application.auth.service import AuthUser
from proseforge.application.retrieval.indexing import (
    EMBEDDING_PREF_KEY,
    embedding_identity,
    index_health_with_rebuild_suppression,
    mark_index_rebuild_started,
    normalize_embedding_preference,
)
from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.embeddings.llama_server import (
    LLAMA_MODELS,
    LlamaServerEmbedder,
)
from proseforge.infrastructure.embeddings.local import (
    DEFAULT_LOCAL_MODEL,
    LOCAL_EMBEDDING_MODELS,
    LocalEmbedder,
    local_model_status,
    visible_local_models,
)
from proseforge.infrastructure.security.credential_cipher import (
    CredentialCipher,
    derive_key,
)
from proseforge.infrastructure.security.endpoint_policy import EndpointPolicy

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["settings"])


class EmbeddingSettingsRequest(BaseModel):
    engine: str = Field(pattern=r"^(local|api|off)$")
    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=200)
    local_model: str | None = Field(default=None, max_length=200)
    # Dedicated embedding credential (engine="api" only): saved encrypted
    # under the synthetic provider name "embedding:<credential_name or
    # provider>", so the vector engine can use its own API base/key instead
    # of sharing the chat credential.
    api_key: str | None = Field(default=None, max_length=1000)
    base_url: str | None = Field(default=None, max_length=500)
    credential_name: str | None = Field(default=None, max_length=64)
    force: bool = False


class EmbeddingDownloadRequest(BaseModel):
    local_model: str = Field(max_length=200)


# Background download tasks must be referenced somewhere or the event loop
# may garbage-collect them mid-flight.
_DOWNLOAD_TASKS: set[asyncio.Task[None]] = set()


async def _run_model_download(embedder: LocalEmbedder | LlamaServerEmbedder) -> None:
    try:
        if isinstance(embedder, LlamaServerEmbedder):
            # Download endpoint: fetch the GGUF weights only — spawning the
            # llama-server happens on first indexing/retrieval use.
            await embedder.ensure_model_ready()
        else:
            await embedder.ensure_ready()
    except Exception:
        # Failure details are already in the per-model status file; the
        # settings endpoint surfaces them to the UI.
        logger.exception("local embedding model download failed: %s", embedder.model)


async def _settings_payload(uow: SqlAlchemyUnitOfWork, user_id: str, cache_dir: str) -> dict[str, object]:
    preference = await uow.user_preferences.get(user_id, EMBEDDING_PREF_KEY)
    config = normalize_embedding_preference(json.loads(preference.value_json) if preference else None)
    indexed_model = await uow.retrieval.get_indexed_model_for_owner(owner_id=user_id)
    return {
        "engine": config["engine"],
        "provider": config["provider"],
        "model": config["model"],
        "local_model": config["local_model"],
        "credential_provider": config.get("credential_provider"),
        "base_url": config.get("base_url"),
        "local": local_model_status(config["local_model"], cache_dir),
        # Per-model disk truth for every VISIBLE local model (bge-m3
        # convergence: hidden registry entries stay functional for rollback
        # but are not offered in the UI), so the frontend can show the real
        # status of a selected-but-unsaved model without a save first.
        "local_models": {model: local_model_status(model, cache_dir) for model in visible_local_models()},
        # Display metadata for the visible models — the frontend renders its
        # model picker from this list instead of a hardcoded copy.
        "visible_models": [
            {
                "id": model,
                "size_mb": int(info["size_mb"]),
                "dimension": int(info["dimension"]),
                "chunk_chars": int(info.get("chunk_chars", 0)),
            }
            for model, info in visible_local_models().items()
        ],
        "indexed_model": indexed_model,
        # Index reconciliation (P0 visibility): chapters that should be
        # indexed vs documents/chunks actually indexed. drift=True means
        # the read side is silently returning empty evidence — except while
        # a force rebuild is in flight, which is drift by design.
        "index_health": await index_health_with_rebuild_suppression(uow, user_id),
    }


@router.get("/settings/embedding")
async def get_embedding_settings(
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    async with uow:
        payload = await _settings_payload(uow, user.id, request.app.state.settings.embedding_cache_dir)
        # The payload build may retire a settled rebuild marker; persist it.
        await uow.commit()
        return payload


@router.put("/settings/embedding")
async def put_embedding_settings(
    payload: EmbeddingSettingsRequest,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
    uow: Annotated[SqlAlchemyUnitOfWork, Depends(unit_of_work)],
) -> dict[str, object]:
    config = {
        "engine": payload.engine,
        "provider": payload.provider if payload.engine == "api" else None,
        "model": payload.model if payload.engine == "api" else None,
        "local_model": payload.local_model or DEFAULT_LOCAL_MODEL,
    }
    if payload.engine == "api" and (not payload.provider or not payload.model):
        raise HTTPException(status_code=400, detail="engine=api 需要提供 provider 和 model")
    if payload.engine == "local" and config["local_model"] not in LOCAL_EMBEDDING_MODELS:
        raise HTTPException(status_code=400, detail=f"不支持的本地模型 {config['local_model']}")
    if payload.engine == "api" and payload.api_key and not payload.base_url:
        raise HTTPException(status_code=400, detail="独立向量凭证需要同时提供 base_url")
    if payload.engine == "api" and payload.base_url:
        try:
            EndpointPolicy(tuple(request.app.state.settings.allowed_local_provider_hosts)).validate(payload.base_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    reindex_job_ids: list[str] = []
    async with uow:
        credential_provider: str | None = None
        credential_base_url: str | None = None
        if payload.engine == "api" and payload.api_key:
            # Same encrypted write as api/routes/credentials.py, under the
            # synthetic provider name "embedding:<credential_name or
            # provider>" so it never collides with chat credentials.
            credential_provider = f"embedding:{payload.credential_name or payload.provider}"
            credential_base_url = payload.base_url
            existing = await uow.credentials.get_for_user(user.id, credential_provider)
            record_id = existing.id if existing else new_id()
            associated_data = f"{user.id}:{credential_provider}:{record_id}".encode()
            cipher = CredentialCipher(derive_key(request.app.state.settings.master_key.get_secret_value()))
            encrypted = cipher.encrypt(
                json.dumps({"api_key": payload.api_key, "base_url": payload.base_url}).encode(),
                associated_data=associated_data,
            )
            await uow.credentials.upsert(
                user.id, credential_provider, base64.b64encode(encrypted).decode(), record_id
            )
        elif payload.engine == "api":
            # No new key in this save: keep the previously stored dedicated
            # credential when the provider selection is unchanged (the key is
            # never echoed back, so blank means "keep as is").
            preference = await uow.user_preferences.get(user.id, EMBEDDING_PREF_KEY)
            previous = normalize_embedding_preference(json.loads(preference.value_json) if preference else None)
            if previous.get("credential_provider") and previous.get("provider") == payload.provider:
                credential_provider = previous["credential_provider"]
                credential_base_url = previous.get("base_url")
        config["credential_provider"] = credential_provider
        config["base_url"] = credential_base_url
        if payload.engine == "api" and credential_provider is None:
            credential = await uow.credentials.get_for_user(user.id, str(payload.provider))
            if credential is None:
                raise HTTPException(status_code=400, detail=f"provider {payload.provider} 未配置凭证")
        identity = embedding_identity(config)
        conflicts = await uow.retrieval.count_active_chunks_with_other_model(
            owner_id=user.id, embedding_model=identity
        )
        if conflicts > 0 and not payload.force:
            raise HTTPException(
                status_code=409,
                detail="已有索引数据使用其他 embedding 引擎，切换将清空全部索引并重建；确认请带 force=true",
            )
        if payload.force:
            # Full rebuild, unconditional on conflicts: the old conflicts>0
            # precondition made force a no-op on an EMPTY index (all jobs
            # failed -> nothing indexed -> no conflict rows), which is
            # exactly the state a rebuild is needed for. Drop in-flight jobs
            # first, or a running job of the old engine would rewrite the
            # document with a stale identity (its source_version still
            # matches -> silent skip).
            await uow.retrieval.delete_unfinished_jobs_for_owner(owner_id=user.id)
            await uow.retrieval.delete_documents_for_owner(owner_id=user.id)
            # Arm the rebuild marker BEFORE the clear takes effect for
            # readers: until the fresh jobs settle, index_health drift is
            # expected and must not alarm.
            await mark_index_rebuild_started(uow, user.id)
            for project_id, chapter_id in await uow.retrieval.list_indexable_chapters_for_owner(owner_id=user.id):
                job = await uow.retrieval.enqueue_job(
                    project_id=project_id, job_type="index_chapter", source_type="chapter", source_id=chapter_id
                )
                reindex_job_ids.append(job.id)
        else:
            # Failed index jobs are terminal in the worker and a save may
            # have fixed ANY class of cause (credential, model download,
            # llama-server port), so re-arm all of them for the sweeper —
            # not just the "embedding 未配置" exact match.
            await uow.retrieval.requeue_failed_jobs_for_owner(owner_id=user.id)
        await uow.user_preferences.set(user.id, EMBEDDING_PREF_KEY, json.dumps(config, ensure_ascii=False))
        await uow.commit()
        response = await _settings_payload(uow, user.id, request.app.state.settings.embedding_cache_dir)
        # The payload build may retire a settled rebuild marker; persist it.
        await uow.commit()

    # Enqueue after commit, same pattern as the proposal-approve route.
    for job_id in reindex_job_ids:
        await request.app.state.queue.enqueue(
            "proseforge.retrieval.index_document",
            {"job_id": job_id, "user_id": user.id},
        )
    return response


@router.post("/settings/embedding/download", status_code=202)
async def download_embedding_model(
    payload: EmbeddingDownloadRequest,
    request: Request,
    user: Annotated[AuthUser, Depends(current_user)],
) -> dict[str, object]:
    """Kick off a background download of a whitelisted local model.

    Idempotent: the lock in LocalEmbedder.ensure_ready makes repeat calls
    no-ops (a second call while downloading just returns 202 again).
    """
    if payload.local_model not in LOCAL_EMBEDDING_MODELS:
        raise HTTPException(status_code=422, detail=f"不支持的本地模型 {payload.local_model}")
    settings = request.app.state.settings
    embedder: LocalEmbedder | LlamaServerEmbedder
    if payload.local_model in LLAMA_MODELS:
        embedder = LlamaServerEmbedder(
            payload.local_model,
            cache_dir=settings.embedding_cache_dir,
            hf_endpoint=settings.hf_endpoint,
        )
    else:
        embedder = LocalEmbedder(
            payload.local_model,
            cache_dir=settings.embedding_cache_dir,
            hf_endpoint=settings.hf_endpoint,
        )
    task = asyncio.create_task(_run_model_download(embedder))
    _DOWNLOAD_TASKS.add(task)
    task.add_done_callback(_DOWNLOAD_TASKS.discard)
    return local_model_status(payload.local_model, settings.embedding_cache_dir)

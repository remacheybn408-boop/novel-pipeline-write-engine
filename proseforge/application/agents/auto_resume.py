"""Auto-resume probing for auto-paused agent runs (celery beat).

The executor pauses a run (status=PAUSED + ``run.auto_paused`` event) after
AUTO_PAUSE_STREAK consecutive retryable provider failures (5xx/429/timeout).
The beat task ``probe_auto_paused_runs`` (every 10 minutes) probes those runs
with a minimal one-token completion against the run's provider/model:

- probe succeeds -> run flips back to RUNNING, ``execute_run`` is re-enqueued
  (same shape as the resume route), a ``run.resumed`` event (probe=true)
  lands, and a 「总调度：模型已恢复」 notice is appended to the swarm chat
  message (same append + SSE pattern as the executor's notify_auto_pause);
- probe fails -> a ``run.resume_probe`` event lands with the error type;
- after MAX_RESUME_PROBES failed probes the run is left to manual resume.

Manually paused runs carry no ``run.auto_paused`` event and are never probed.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

logger = logging.getLogger(__name__)

MAX_RESUME_PROBES = 2
PROBE_SCAN_LIMIT = 50


async def _add_run_event(uow, run, event_type: str, data: dict[str, object] | None = None) -> None:
    # Same row-locked sequence allocation as the executor's add_event:
    # (run_id, sequence) stays unique and monotonic under replay.
    from proseforge.domain.common.ids import new_id
    from proseforge.infrastructure.database.models.agents import (
        AgentEventModel,
        AgentRunModel,
    )

    locked = await uow.session.scalar(
        select(AgentRunModel).where(AgentRunModel.id == run.id).with_for_update().execution_options(populate_existing=True)
    )
    sequence = int(locked.event_cursor) + 1
    uow.session.add(AgentEventModel(id=new_id(), run_id=locked.id, sequence=sequence, event_type=event_type, payload=json.dumps(data or {}, sort_keys=True)))
    locked.event_cursor = sequence
    locked.updated_at = datetime.now(UTC)


async def resolve_run_provider(uow, settings, user_id: str, provider_id: str):
    """Decrypt the run owner's credential and build the provider — the same
    resolution flow the executor uses per task. None when the user has no
    credential for the provider."""
    import base64

    from proseforge.infrastructure.security.credential_cipher import (
        CredentialCipher,
        derive_key,
    )
    from proseforge.providers.factory import build_provider

    credential = await uow.credentials.get_for_user(user_id, provider_id)
    if credential is None:
        return None
    raw_key = derive_key(settings.master_key.get_secret_value())
    associated = f"{user_id}:{provider_id}:{credential.id}".encode()
    secret = json.loads(CredentialCipher(raw_key).decrypt(base64.b64decode(credential.encrypted_payload), associated_data=associated))
    return build_provider(provider_id, str(secret["api_key"]), secret.get("base_url"))


async def probe_model(provider, model: str) -> None:
    """Cheapest real availability check: a one-token completion. count_tokens
    is a local estimate in every provider, so it proves nothing; stream does.
    Raises whatever the provider raises on failure."""
    from proseforge.domain.ports.model_provider import GenerationRequest

    request = GenerationRequest(
        model=model,
        system_blocks=(),
        input_blocks=({"role": "user", "text": "ping"},),
        max_output_tokens=1,
    )
    async for _event in provider.stream(request):
        break


async def append_run_chat_notice(session_factory, settings, *, run_id: str, idempotency_key: str | None, text: str) -> None:
    """Append a 总调度 notice to the run's swarm chat message (batch chapter
    runs fall back to the parent analyze run's message via the idempotency
    key) and publish the same message.completed SSE shape the executor uses,
    so the ChatPage subscription refreshes. Best-effort: a notify failure
    must never break probe bookkeeping."""
    from proseforge.application.agents.batch_dispatch import parse_batch_key
    from proseforge.infrastructure.database.models.conversation import MessageModel
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
    from proseforge.infrastructure.events.hybrid import HybridEventStream

    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            message = await uow.session.scalar(select(MessageModel).where(MessageModel.agent_run_id == run_id))
            if message is None and idempotency_key:
                parsed = parse_batch_key(idempotency_key)
                if parsed is not None:
                    message = await uow.session.scalar(select(MessageModel).where(MessageModel.agent_run_id == parsed[0]))
            if message is None:
                return
            message.content = (message.content or "") + text
            conversation_id = await uow.conversations.conversation_id_for_message(message.id)
            message_id = message.id
            await uow.commit()
        event_stream = HybridEventStream(session_factory, settings.redis_url)
        event: dict[str, object] = {"event": "message.completed", "message_id": message_id, "status": "COMPLETED"}
        await event_stream.publish(f"message:{message_id}", event)
        if conversation_id:
            await event_stream.publish(f"conversation:{conversation_id}", event)
    except Exception:
        logger.exception("auto-resume notify failed run_id=%s", run_id)


async def _resume_after_probe(session_factory, settings, queue, *, run_id: str, user_id: str, idempotency_key: str | None) -> bool:
    """Flip a successfully probed run back to RUNNING and re-enqueue
    execute_run (same payload as the resume route). An enqueue failure rolls
    the run back to PAUSED so it never wedges in RUNNING with no executor."""
    from proseforge.infrastructure.database.models.agents import AgentRunModel
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        run = await uow.session.scalar(
            select(AgentRunModel).where(AgentRunModel.id == run_id).with_for_update().execution_options(populate_existing=True)
        )
        # A manual resume/cancel may have raced the probe: only PAUSED runs resume.
        if run is None or run.status != "PAUSED":
            return False
        run.status = "RUNNING"
        await _add_run_event(uow, run, "run.resumed", {"probe": True})
        await uow.commit()
    try:
        await queue.enqueue("proseforge.agents.execute_run", {"run_id": run_id, "user_id": user_id})
    except Exception:  # broker failures surface as arbitrary exceptions; roll back like the resume route
        logger.exception("auto-resume enqueue failed run_id=%s", run_id)
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            run = await uow.session.scalar(
                select(AgentRunModel).where(AgentRunModel.id == run_id).with_for_update().execution_options(populate_existing=True)
            )
            if run is not None and run.status == "RUNNING":
                run.status = "PAUSED"
                await _add_run_event(uow, run, "run.queue_failed", {"reason": "queue unavailable", "probe": True})
                await uow.commit()
        return False
    await append_run_chat_notice(
        session_factory, settings, run_id=run_id, idempotency_key=idempotency_key,
        text="\n\n总调度：模型已恢复，自动继续写作。",
    )
    return True


async def probe_auto_paused_runs(session_factory, settings, queue, *, max_probes: int = MAX_RESUME_PROBES, limit: int = PROBE_SCAN_LIMIT) -> int:
    """One probe round over auto-paused runs. Returns the number probed."""
    from proseforge.infrastructure.database.models.agents import (
        AgentEventModel,
        AgentRunModel,
    )
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

    candidates: list[dict[str, Any]] = []
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        paused = list(await uow.session.scalars(select(AgentRunModel).where(AgentRunModel.status == "PAUSED").limit(limit)))
        if not paused:
            return 0
        run_ids = [run.id for run in paused]
        auto_paused_ids: set[str] = set()
        probe_counts: dict[str, int] = {}
        for event_row in await uow.session.scalars(
            select(AgentEventModel).where(
                AgentEventModel.run_id.in_(run_ids),
                AgentEventModel.event_type.in_(("run.auto_paused", "run.resume_probe")),
            )
        ):
            if event_row.event_type == "run.auto_paused":
                auto_paused_ids.add(event_row.run_id)
            else:
                probe_counts[event_row.run_id] = probe_counts.get(event_row.run_id, 0) + 1
        for run in paused:
            # Manual pause (no run.auto_paused event) or probe budget spent
            # (>= max_probes failures -> manual resume only): skip.
            if run.id not in auto_paused_ids or probe_counts.get(run.id, 0) >= max_probes:
                continue
            candidates.append({
                "run_id": run.id,
                "user_id": run.user_id,
                "idempotency_key": run.idempotency_key,
                # run row columns win (same rule as the executor); defaults
                # match the executor's fallback.
                "provider": run.provider or "openai",
                "model": run.model or "gpt-4.1-mini",
                "probe_no": probe_counts.get(run.id, 0) + 1,
            })

    probed = 0
    for candidate in candidates:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            provider = await resolve_run_provider(uow, settings, str(candidate["user_id"]), str(candidate["provider"]))
        error: str | None = None
        if provider is None:
            error = "credential-missing"
        else:
            try:
                await probe_model(provider, str(candidate["model"]))
            except Exception as exc:
                error = type(exc).__name__
        if error is None:
            if await _resume_after_probe(
                session_factory, settings, queue,
                run_id=str(candidate["run_id"]), user_id=str(candidate["user_id"]),
                idempotency_key=candidate["idempotency_key"],
            ):
                probed += 1
            continue
        probed += 1
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            run = await uow.session.scalar(select(AgentRunModel).where(AgentRunModel.id == str(candidate["run_id"])))
            # Raced with a manual resume/cancel: leave the new state alone.
            if run is None or run.status != "PAUSED":
                continue
            await _add_run_event(uow, run, "run.resume_probe", {"probe": candidate["probe_no"], "error": error})
            await uow.commit()
    if probed:
        logger.info("auto-resume probe round: %d run(s) probed", probed)
    return probed


async def probe_auto_paused_runs_entry(payload: dict[str, object]) -> int:
    """Queue entry for the auto-resume prober (celery beat).

    Builds its own engine/session in the worker process, same style as
    sweeper.py's sweep_stale_run_messages_entry."""
    from proseforge.infrastructure.database.session import (
        create_engine_and_sessionmaker,
    )
    from proseforge.infrastructure.tasks.celery import CeleryTaskQueue
    from proseforge.settings import get_settings

    del payload
    settings = get_settings()
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        return await probe_auto_paused_runs(session_factory, settings, CeleryTaskQueue())
    finally:
        await engine.dispose()

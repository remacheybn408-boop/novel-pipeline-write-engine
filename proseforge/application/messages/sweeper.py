"""Swarm assistant-message sweeper (placeholder self-healing).

A swarm chat run links a placeholder assistant message via
``messages.agent_run_id``; the executor writes the deterministic run
summary back to it at run end. Two failure modes strand that placeholder
in a non-terminal state forever:

1. the run reached a terminal status but the writeback commit failed
   (agent_executor only logs the exception) -> message stays PENDING;
2. the worker died mid-run -> run stuck RUNNING, message stuck PENDING.

``sweep_stale_run_messages`` replays the same writeback the executor uses
for case 1 and marks case-2 runs FAILED (only when every task lease has
expired, so an in-flight run is never touched) before replaying. Both
callers go through ``writeback_run_message`` here — the single shared
implementation the executor delegates to — so a replay is byte-identical
to the live writeback.

A third failure mode is the chapter writeback: the executor commits
run.completed BEFORE writing the chapter, so a writeback commit failure
leaves a COMPLETED run whose chapter body never landed (only a
``chapter.writeback_failed`` event). ``sweep_missed_chapter_writebacks``
replays the shared ``writeback_chapter_for_run`` for those runs —
idempotent via the ``chapter.written_back`` event, with a cooldown
marker so a persistently failing replay is not retried every sweep.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

logger = logging.getLogger(__name__)

TERMINAL_RUN_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "BUDGET_EXHAUSTED"})
TERMINAL_MESSAGE_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
RUNNING_RUN_THRESHOLD_SECONDS = 600  # no event/lease activity for 10 min -> executor presumed dead
# A failed chapter-writeback replay is retried at most once per cooldown:
# the chapter.writeback_replayed marker event (payload "at") is the dedupe.
WRITEBACK_REPLAY_COOLDOWN_SECONDS = 900


async def writeback_run_message(session_factory, settings, run_id: str, final_status: str, terminal_reason: str | None) -> None:
    """Write the deterministic run summary back to the swarm
    assistant message linked via messages.agent_run_id, then
    publish the same completion/failure event shape generate_chat
    uses so the ChatPage subscription refreshes. Never raises:
    a writeback failure must not break terminal bookkeeping."""
    from proseforge.application.agents.quality_gate import evaluate_gate
    from proseforge.application.agents.run_summary import (
        infer_intent,
        render_run_summary,
    )
    from proseforge.infrastructure.database.models.agents import (
        AgentArtifactModel,
        AgentEventModel,
        AgentRunModel,
        AgentTaskModel,
    )
    from proseforge.infrastructure.database.models.conversation import MessageModel
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
    from proseforge.infrastructure.events.hybrid import HybridEventStream

    try:
        async with SqlAlchemyUnitOfWork(session_factory) as wb_uow:
            message = await wb_uow.session.scalar(select(MessageModel).where(MessageModel.agent_run_id == run_id))
            if message is None:
                return
            task_rows = list(await wb_uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run_id)))
            task_key_by_id = {row.id: row.task_key for row in task_rows}
            artifact_rows = list(await wb_uow.session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.run_id == run_id)))
            task_dicts = [{"task_key": row.task_key, "role": row.role, "status": row.status, "last_error": row.last_error} for row in task_rows]
            artifact_dicts = [{"task_key": task_key_by_id.get(row.task_id, ""), "artifact_type": row.artifact_type, "preview": row.preview, "payload": row.payload} for row in artifact_rows]
            gate_info: dict | None = None
            chapter_info: dict | None = None
            for event_row in await wb_uow.session.scalars(select(AgentEventModel).where(AgentEventModel.run_id == run_id, AgentEventModel.event_type.in_(("gate.evaluated", "chapter.written_back"))).order_by(AgentEventModel.sequence)):
                event_data = json.loads(event_row.payload)
                if event_row.event_type == "gate.evaluated":
                    gate_info = event_data
                else:
                    chapter_info = event_data
            if gate_info and not gate_info.get("passed") and final_status == "COMPLETED":
                # Post-rewrite re-check: the FINAL draft (rewrite wins,
                # then the select winner / legacy scene draft) against the
                # same gate, with the recheck report as the review input.
                # Feeds the summary's "带警告交付" line when the rewrite
                # still falls short.
                goal = str(await wb_uow.session.scalar(select(AgentRunModel.goal).where(AgentRunModel.id == run_id)) or "")
                parsed: dict[str, list[dict]] = {}
                for item in artifact_dicts:
                    try:
                        payload = json.loads(str(item.get("payload") or "{}"))
                    except ValueError:
                        continue
                    if isinstance(payload, dict):
                        parsed.setdefault(str(item.get("task_key", "")), []).append(payload)
                final_payload = next(
                    (p for key in ("rewrite", "select", "scene") for p in parsed.get(key, []) if isinstance(p.get("content"), str) and p["content"].strip()),
                    None,
                )
                post = evaluate_gate(goal=goal, scene_payload=final_payload, review_payloads=parsed.get("recheck", []))
                gate_info = {**gate_info, "post_passed": post.passed, "post_reasons": post.reasons}
            summary = render_run_summary(intent=infer_intent(task_dicts), status=final_status, terminal_reason=terminal_reason, tasks=task_dicts, artifacts=artifact_dicts, gate=gate_info, chapter=chapter_info)
            message.content = summary
            if message.status == "CANCELLED":
                # A user-cancelled message wins over run terminal bookkeeping:
                # write the summary content for context but never flip the
                # status back to COMPLETED/FAILED and never emit a
                # completion/failure event (the chat UI already rendered
                # 已取消 without the retry button).
                await wb_uow.commit()
                return
            # CANCELLED stays CANCELLED: the message state machine has the
            # state and the chat UI renders it without the retry button
            # (retry on a CANCELLED run 409s). Everything else non-terminal
            # maps to FAILED.
            message_status = final_status if final_status in {"COMPLETED", "CANCELLED"} else "FAILED"
            message.status = message_status
            conversation_id = await wb_uow.conversations.conversation_id_for_message(message.id)
            await wb_uow.commit()
            message_id = message.id
        event_stream = HybridEventStream(session_factory, settings.redis_url)
        event: dict[str, object] = {"event": "message.completed" if final_status == "COMPLETED" else "message.failed", "message_id": message_id, "status": message_status}
        if final_status != "COMPLETED":
            event["reason"] = terminal_reason or final_status.lower()
        await event_stream.publish(f"message:{message_id}", event)
        if conversation_id:
            await event_stream.publish(f"conversation:{conversation_id}", event)
    except Exception:
        logger.exception("agent run message writeback failed run_id=%s", run_id)


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


async def sweep_stale_run_messages(session_factory, settings, *, running_threshold_seconds: float = RUNNING_RUN_THRESHOLD_SECONDS, limit: int = 50) -> int:
    """Repair swarm messages stranded in a non-terminal state.

    Pass 1 replays the writeback for terminal runs whose linked message
    never flipped. Pass 2 fails RUNNING runs idle past the threshold with
    no live task lease (worker died) and replays their writeback too.
    Pass 3 replays missed chapter writebacks on COMPLETED write-pipeline
    runs. Returns the number of writebacks replayed."""
    from proseforge.infrastructure.database.models.agents import (
        AgentRunModel,
        AgentTaskModel,
    )
    from proseforge.infrastructure.database.models.conversation import MessageModel
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

    repaired = 0
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        stranded = list(await uow.session.scalars(
            select(AgentRunModel)
            .join(MessageModel, MessageModel.agent_run_id == AgentRunModel.id)
            .where(AgentRunModel.status.in_(TERMINAL_RUN_STATUSES), MessageModel.status.notin_(TERMINAL_MESSAGE_STATUSES))
            .limit(limit)
        ))
        replays = [(row.id, row.status, row.terminal_reason) for row in stranded]
    for run_id, status, reason in replays:
        await writeback_run_message(session_factory, settings, run_id, status, reason)
        repaired += 1

    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=running_threshold_seconds)
    failed_run_ids: list[str] = []
    failed_run_owners: dict[str, str] = {}
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        candidates = list(await uow.session.scalars(
            select(AgentRunModel).where(AgentRunModel.status == "RUNNING", AgentRunModel.updated_at < cutoff).limit(limit)
        ))
        for run in candidates:
            # A live lease means a worker is still on this run: leave it alone.
            live_lease = await uow.session.scalar(
                select(AgentTaskModel.id)
                .where(
                    AgentTaskModel.run_id == run.id,
                    AgentTaskModel.status == "RUNNING",
                    AgentTaskModel.lease_expires_at.isnot(None),
                    AgentTaskModel.lease_expires_at > now,
                )
                .limit(1)
            )
            if live_lease is not None:
                continue
            # Backoff exemption: a run whose pending tasks are scheduled for a
            # future retry (retryable provider error) is waiting, not stranded —
            # sweeping it FAILED would silently skip the chapter mid-outage.
            backoff_waiting = await uow.session.scalar(
                select(AgentTaskModel.id)
                .where(
                    AgentTaskModel.run_id == run.id,
                    AgentTaskModel.status == "PENDING",
                    AgentTaskModel.next_attempt_at.isnot(None),
                    AgentTaskModel.next_attempt_at > now,
                )
                .limit(1)
            )
            if backoff_waiting is not None:
                continue
            run.status = "FAILED"
            run.terminal_reason = "executor lost; marked failed by message sweeper"
            await _add_run_event(uow, run, "run.failed", {"reason": run.terminal_reason, "sweeper": True})
            failed_run_ids.append(run.id)
            failed_run_owners[run.id] = run.user_id
        await uow.commit()
    for run_id in failed_run_ids:
        await writeback_run_message(session_factory, settings, run_id, "FAILED", "executor lost; marked failed by message sweeper")
        # A swept batch chapter run must still advance its chain (the
        # dispatcher hook dedupes via the chapter idempotency key).
        from proseforge.application.agents.batch_dispatch import on_run_terminal

        await on_run_terminal(session_factory, settings, run_id=run_id, user_id=failed_run_owners[run_id], status="FAILED")
        repaired += 1
    repaired += await sweep_missed_chapter_writebacks(session_factory, settings, limit=limit)
    if repaired:
        logger.info("message sweeper replayed %d stranded swarm message writeback(s)", repaired)
    return repaired


async def sweep_missed_chapter_writebacks(session_factory, settings, *, cooldown_seconds: float = WRITEBACK_REPLAY_COOLDOWN_SECONDS, limit: int = 50) -> int:
    """Replay the chapter writeback for COMPLETED write-pipeline runs
    whose writeback never landed.

    The executor writes the chapter back AFTER committing run.completed;
    a commit failure there only logs + drops a chapter.writeback_failed
    event, leaving the run COMPLETED with the chapter body lost forever.
    Candidates: run COMPLETED + a scene-class task + no
    chapter.written_back event. Idempotent: a written-back run is never
    rewritten (the shared writeback_for_run re-checks the event inside
    its own transaction), and a failed replay is retried at most once
    per cooldown via the chapter.writeback_replayed marker event."""
    from sqlalchemy import exists, or_

    from proseforge.infrastructure.database.models.agents import (
        AgentEventModel,
        AgentRunModel,
        AgentTaskModel,
    )
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
    from proseforge.workflows.agent_executor import writeback_chapter_for_run

    scene_task = exists(
        select(AgentTaskModel.id).where(
            AgentTaskModel.run_id == AgentRunModel.id,
            or_(AgentTaskModel.task_key == "scene", AgentTaskModel.task_key.like("scene\\_%", escape="\\")),
        )
    )
    written_back = exists(
        select(AgentEventModel.id).where(AgentEventModel.run_id == AgentRunModel.id, AgentEventModel.event_type == "chapter.written_back")
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        candidates = list(await uow.session.scalars(
            select(AgentRunModel).where(AgentRunModel.status == "COMPLETED", scene_task, ~written_back).limit(limit)
        ))
        rows = [(run.id, run.user_id) for run in candidates]
    replayed = 0
    now = datetime.now(UTC)
    for run_id, owner_id in rows:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            marker = await uow.session.scalar(
                select(AgentEventModel.payload)
                .where(AgentEventModel.run_id == run_id, AgentEventModel.event_type == "chapter.writeback_replayed")
                .order_by(AgentEventModel.sequence.desc())
                .limit(1)
            )
        if marker is not None:
            try:
                marked_at = datetime.fromisoformat(str(json.loads(marker).get("at") or ""))
            except ValueError:
                marked_at = None
            if marked_at is not None and marked_at.tzinfo is None:
                marked_at = marked_at.replace(tzinfo=UTC)
            if marked_at is not None and (now - marked_at).total_seconds() < cooldown_seconds:
                continue  # 冷却期内：上一轮重放刚失败过，不每轮都重打
        written = await writeback_chapter_for_run(session_factory, settings, run_id=run_id, user_id=owner_id)
        if not written:
            # Marker = cooldown + audit; on success chapter.written_back
            # itself takes the run off the candidate list.
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                run = await uow.session.get(AgentRunModel, run_id)
                if run is not None:
                    await _add_run_event(uow, run, "chapter.writeback_replayed", {"outcome": "failed", "at": now.isoformat()})
                    await uow.commit()
        replayed += 1
    if replayed:
        logger.info("chapter writeback sweeper replayed %d missed chapter writeback(s)", replayed)
    return replayed


async def sweep_stale_run_messages_entry(payload: dict[str, object]) -> int:
    """Queue entry for the swarm-message sweeper (celery beat).

    Builds its own engine/session in the worker process, same style as
    tasks.py's recover_expired / sweep_pending_retrieval_jobs entries."""
    from proseforge.infrastructure.database.session import (
        create_engine_and_sessionmaker,
    )
    from proseforge.settings import get_settings

    del payload
    settings = get_settings()
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        return await sweep_stale_run_messages(session_factory, settings)
    finally:
        await engine.dispose()

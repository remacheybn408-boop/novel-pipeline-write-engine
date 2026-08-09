"""V3 agent run 持久执行器（蓝图 V3-004/005）。

把一个持久化的 V3 task graph 按 checkpoint 逐批执行到终态：
- 每次调用先一次性回收滞留 RUNNING 任务（task.recovered；只收 lease
  已过期或无 lease 时间戳的，在飞任务不动），循环顶与提交前都尊重
  PAUSED/CANCELLED（任务重置回 PENDING）；
- 依赖就绪（depends_on 全部 SUCCEEDED）的任务按 profile 并行度上限
  （``max_parallel_tasks``：native 4 / server、test 16）有界认领，经
  ``bounded_parallel`` 信号量并行执行；
- 模型调用发生在任何数据库事务之外；每个任务在自己的短事务里提交
  Artifact + 事件 + 实测 usage 结算 + run.checkpoint_id（提交阶段由
  commit_lock 串行，保证 (run_id, sequence) 唯一且单调递增）；
- 有任务 FAILED 且无 PENDING 可重试时 run 以 FAILED 收场（不误报 COMPLETED）；
  唯一例外是尾评审 recheck 任务：最终 attempt 仍失败时降级为 SUCCEEDED +
  “评审不可用”警告 artifact，让 run 能 COMPLETED 交付写作产物；
- Chief Editor 由 application/agents/chief_handler.py 的注册 handler 产出
  MergeCandidate + V2 RevisionProposal（未裁定冲突时 guard_status=blocked）；
- 角色 handler 抛 PolicyDenied 视为确定性拒绝：任务立即 FAILED（不重试），
  并留 policy.denied 审计事件。
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import UTC, datetime, timedelta
from functools import partial

from proseforge.application.agents.memory_service import load_memory_slice
from proseforge.application.agents.parallel import bounded_parallel
from proseforge.application.agents.role_handlers import (
    RoleResult,
    allowed_artifact_types,
    handler_for,
    resolve_max_output_tokens,
    validate_artifact_payload,
)
from proseforge.domain.agents.policy import PolicyDenied

logger = logging.getLogger(__name__)

MAX_PARALLEL_TASKS = 16  # server/test profile 的并行度上限（蓝图 V3-004：native 4 / server 16）
EXECUTOR_VERSION = "v3-exec-1"
TASK_LEASE_TTL_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 3  # AgentTaskModel 尚无 max_attempts 列，沿用 AgentTaskSpec 默认
# Retryable provider errors (5xx/429/timeout) back off exponentially on a
# dedicated counter — they never consume the DEFAULT_MAX_ATTEMPTS budget
# (2026-08-04 DeepSeek 503 storm: three instant retries all hit the wall).
RETRYABLE_MAX_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS = (30, 120, 300)  # beyond the schedule: stay at 300s, ±20% jitter
# Consecutive retryable provider failures (run-wide, across tasks) that
# auto-pause the run with a chief-dispatcher chat notice.
AUTO_PAUSE_STREAK = 3
AUTO_PAUSE_MAX_NOTICES = 3
# Lane-budget tightening thresholds: past 70% of a lane's own budget its
# input_budget halves; past 90% it quarters. Only that lane is squeezed.
LANE_TIGHTEN_MID = 0.7
LANE_TIGHTEN_HIGH = 0.9
# run.budget_limit = Σ lane budgets × headroom (revise-loop re-runs reuse
# the same task rows, so lane usage can exceed the fixed node count).
RUN_BUDGET_HEADROOM = 1.5
# Tail-review task key: the only task allowed to degrade to a warning
# artifact instead of FAILED after its final attempt (see run_claimed).
RECHECK_TASK_KEY = "recheck"
# Bounded revise loop (quality over speed): the final gate after recheck
# re-runs rewrite/recheck at most this many rounds before the draft ships
# with a chapter.quality_degraded audit event.
MAX_REVISE_ROUNDS = 2


def _retry_backoff_seconds(retryable_attempts: int) -> float:
    """Exponential backoff for retryable provider errors, ±20% jitter."""
    base = RETRY_BACKOFF_SECONDS[min(retryable_attempts, len(RETRY_BACKOFF_SECONDS)) - 1]
    return base * random.uniform(0.8, 1.2)

# Narrative-RAG retrieval intent (query distillation) moved up to the shared
# retrieval proxy: proseforge/application/retrieval/proxy.py.


def max_parallel_tasks(profile: str) -> int:
    """并行度上限按运行时 profile 取值：native 4，server/test 16（蓝图 V3-004）。"""
    from proseforge.runtime.profile import RuntimeProfile

    return 4 if RuntimeProfile(profile) is RuntimeProfile.NATIVE else MAX_PARALLEL_TASKS


def validate_role_result(role: str, result: RoleResult) -> str | None:
    """服务端校验：先查角色 allowlist（domain/agents/roles.py），再查类型 schema。"""
    if result.artifact_type not in allowed_artifact_types(role):
        return f"artifact type {result.artifact_type} not allowed for role {role}"
    return validate_artifact_payload(result.artifact_type, result.payload)


def _lease_expired(task, now, lease_ttl_supported: bool) -> bool:
    """RUNNING 任务是否滞留可回收：只收 lease 已过期（或无 lease 时间戳）的，
    在飞任务（lease 未过期）不动。无 lease 列（旧库）保持原语义全收。"""
    if not lease_ttl_supported:
        return True
    expires = task.lease_expires_at
    if expires is None:
        return True
    if expires.tzinfo is None:  # SQLite 读回 naive datetime，按 UTC 解释
        expires = expires.replace(tzinfo=now.tzinfo)
    return expires < now


def _aware_dt(value, ref):
    """SQLite 读回 naive datetime 时按 ref 的时区解释（与 _lease_expired 同规则）。"""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=ref.tzinfo)
    return value


def _retry_waiting(task, now) -> bool:
    """任务是否处于可重试供应商错误的退避等待期（未到 next_attempt_at）。"""
    due = _aware_dt(task.next_attempt_at, now)
    return due is not None and due > now


async def writeback_chapter_for_run(session_factory, settings, *, run_id: str, user_id: str) -> bool:
    """Write-pipeline runs persist their final draft as a project
    chapter (rewrite wins, then the select winner / legacy scene
    draft), link it on the run row and enqueue REAL indexing. Never
    overturns the run: failure -> chapter.writeback_failed event +
    warning log. Returns True when the chapter is durably written back
    (including an already-written-back run — the replay is idempotent).

    Module-level (not nested in execute_run) so the message sweeper can
    replay the exact same writeback for COMPLETED runs whose
    post-completion writeback commit failed — the same shared-
    implementation pattern as sweeper.writeback_run_message."""
    import json
    from datetime import UTC, datetime

    from sqlalchemy import select

    from proseforge.application.agents.review_target import parse_chapter_no
    from proseforge.domain.chapter.entity import Chapter
    from proseforge.domain.common.ids import new_id
    from proseforge.infrastructure.database.models.agents import (
        AgentArtifactModel,
        AgentEventModel,
        AgentRunModel,
        AgentTaskModel,
    )
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
    from proseforge.infrastructure.tasks.factory import create_task_queue

    async def add_event(uow, run, event_type: str, data: dict[str, object] | None = None) -> None:
        # run 行锁串行分配 sequence（与 execute_run 内 add_event 同一套行锁语义）
        locked = await uow.session.scalar(
            select(AgentRunModel).where(AgentRunModel.id == run.id).with_for_update().execution_options(populate_existing=True)
        )
        sequence = int(locked.event_cursor) + 1
        uow.session.add(AgentEventModel(id=new_id(), run_id=locked.id, sequence=sequence, event_type=event_type, payload=json.dumps(data or {}, sort_keys=True)))
        locked.event_cursor = sequence
        locked.updated_at = datetime.now(UTC)

    try:
        job_id: str | None = None
        summarize_job_id: str | None = None
        promise_sync: tuple[str, int, str] | None = None
        async with SqlAlchemyUnitOfWork(session_factory) as cb_uow:
            run = await cb_uow.session.scalar(select(AgentRunModel).where(AgentRunModel.id == run_id))
            if run is None:
                return False
            already = await cb_uow.session.scalar(
                select(AgentEventModel.id).where(AgentEventModel.run_id == run_id, AgentEventModel.event_type == "chapter.written_back").limit(1)
            )
            if already is not None:
                # 幂等：写回事件在 = 章节已落库，sweeper 重放/重复调用绝不二次写版本。
                return True
            task_rows = list(await cb_uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run_id)))
            # Write pipelines carry scene drafts: the legacy single
            # "scene" task or the production scene_a/b/c/d parallel drafts.
            if not any(task.task_key == "scene" or task.task_key.startswith("scene_") for task in task_rows):
                return False  # not a write pipeline
            task_key_by_id = {task.id: task.task_key for task in task_rows}
            scene_payload: dict | None = None
            rewrite_payload: dict | None = None
            for artifact_row in await cb_uow.session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.run_id == run_id).order_by(AgentArtifactModel.id)):
                try:
                    artifact_payload = json.loads(artifact_row.payload)
                except ValueError:
                    continue
                if not isinstance(artifact_payload, dict) or not isinstance(artifact_payload.get("content"), str) or not artifact_payload["content"].strip():
                    continue
                artifact_key = task_key_by_id.get(artifact_row.task_id, "")
                if artifact_key in {"select", "scene"} and scene_payload is None:
                    scene_payload = artifact_payload
                elif artifact_key == "rewrite":
                    # Ordered by id (time-ordered): with bounded revise
                    # rounds the LATEST rewrite artifact is the final draft.
                    rewrite_payload = artifact_payload
            final = rewrite_payload or scene_payload
            if final is None:
                return False
            title = str(final.get("title") or "").strip()
            content = final["content"]
            # Reuse review_target.parse_chapter_no: both digits and
            # Chinese numerals ("写第三章") resolve; only an unparsable
            # goal falls back to max+1.
            chapter_no = parse_chapter_no(run.goal or "")
            chapters = await cb_uow.chapters.list_owned(run.project_id, user_id)
            if chapter_no is None:
                chapter_no = max((chapter.chapter_no for chapter in chapters), default=0) + 1
            target = next((chapter for chapter in chapters if chapter.chapter_no == chapter_no), None)
            if target is None:
                target = await cb_uow.chapters.add(Chapter.create(project_id=run.project_id, chapter_no=chapter_no, title=title or f"第{chapter_no}章"))
            version = await cb_uow.chapters.append_version(chapter_id=target.id, content=content)
            await cb_uow.chapters.set_active_version(target.id, version.id)
            run.chapter_id = target.id
            # 卷一等公民（迁移 0052）：按 goal 卷标签解析本章卷序号落库；
            # 无标签保持 NULL，查询侧回退 goal 正则 / 固定 10 章一卷。
            from proseforge.application.work.rollup_recap import (
                parse_volume_spans,
                resolve_volume_span,
                volume_index,
            )
            from proseforge.infrastructure.database.models.chapter import ChapterModel

            volume_spans = parse_volume_spans(run.goal or "")
            if volume_spans:
                resolved_volume_no = volume_index(resolve_volume_span(chapter_no, volume_spans), volume_spans)
                chapter_row = await cb_uow.session.get(ChapterModel, target.id)
                if chapter_row is not None and chapter_row.volume_no != resolved_volume_no:
                    chapter_row.volume_no = resolved_volume_no
            job = await cb_uow.retrieval.enqueue_job(project_id=run.project_id, job_type="index_chapter", source_type="chapter", source_id=target.id)
            # Batch path must also enqueue the summarize job (摘要/角色提取/
            # 状态台账 chapter_fact/character_state) — previously only the
            # proposal-approval path did, leaving batch chapters unsummarized.
            summarize_job = await cb_uow.retrieval.enqueue_job(
                project_id=run.project_id, job_type="summarize_chapter",
                source_type="chapter_version", source_id=version.id,
            )
            await add_event(cb_uow, run, "chapter.written_back", {"chapter_id": target.id, "chapter_no": chapter_no, "version_id": version.id, "title": target.title})
            # Detach plain values for the post-commit promise sync.
            promise_sync = (run.project_id, chapter_no, run.goal or "")
            await cb_uow.commit()
            job_id = job.id
            summarize_job_id = summarize_job.id
        if promise_sync is not None:
            # Best-effort promise lifecycle sync (伏笔/钩子 goal line):
            # failures are logged, never overturn the writeback.
            try:
                from proseforge.application.story_bible.promise_tracker import (
                    sync_promises_from_goal,
                )

                async with SqlAlchemyUnitOfWork(session_factory) as promise_uow:
                    await sync_promises_from_goal(
                        promise_uow.session,
                        project_id=promise_sync[0], chapter_no=promise_sync[1], goal_text=promise_sync[2],
                    )
                    await promise_uow.commit()
            except Exception:
                logger.warning("promise sync failed run_id=%s", run_id, exc_info=True)
        if job_id:
            queue = create_task_queue(settings, session_factory)
            await queue.enqueue("proseforge.retrieval.index_document", {"job_id": job_id, "user_id": user_id})
        if summarize_job_id:
            queue = create_task_queue(settings, session_factory)
            await queue.enqueue("proseforge.work.summarize_chapter", {"job_id": summarize_job_id, "user_id": user_id})
        return True
    except Exception as exc:
        logger.warning("chapter writeback failed run_id=%s: %s", run_id, exc)
        try:
            async with SqlAlchemyUnitOfWork(session_factory) as err_uow:
                run = await err_uow.session.scalar(select(AgentRunModel).where(AgentRunModel.id == run_id))
                if run is not None:
                    await add_event(err_uow, run, "chapter.writeback_failed", {"error": type(exc).__name__})
                    await err_uow.commit()
        except Exception:
            logger.exception("chapter.writeback_failed event also failed run_id=%s", run_id)
        return False


async def execute_run(payload: dict[str, object]) -> str:
    import asyncio
    import base64
    import hashlib
    import json
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func, select, update

    from proseforge.application.agents.quality_gate import evaluate_gate
    from proseforge.application.models.cluster_config import (
        agent_role_to_cluster_role,
        available_model_refs,
        get_effective_cluster_config,
        reasoning_level_for,
        resolve_from_config,
    )
    from proseforge.application.models.reasoning_policy import (
        resolve_task_reasoning,
    )
    from proseforge.application.models.resolve_model import resolve_capabilities
    from proseforge.application.retrieval.proxy import retrieve_for_context
    from proseforge.application.work.retriever import narrative_rag_switch_enabled
    from proseforge.domain.common.errors import RetryableProviderError
    from proseforge.domain.common.ids import new_id
    from proseforge.domain.model.capabilities import (
        CONTEXT_INPUT_RATIO,
        context_budget_cap,
    )
    from proseforge.infrastructure.database.models.agents import (
        AgentArtifactModel,
        AgentEventModel,
        AgentRunModel,
        AgentTaskModel,
    )
    from proseforge.infrastructure.database.models.chapter import ChapterModel
    from proseforge.infrastructure.database.models.conversation import MessageModel
    from proseforge.infrastructure.database.models.project import ProjectModel
    from proseforge.infrastructure.database.session import (
        create_engine_and_sessionmaker,
    )
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
    from proseforge.infrastructure.security.credential_cipher import (
        CredentialCipher,
        derive_key,
    )
    from proseforge.providers.errors import classify_provider_error
    from proseforge.providers.factory import build_provider
    from proseforge.settings import get_settings

    settings = get_settings()
    engine, session_factory = create_engine_and_sessionmaker(settings)
    # 认领/信号量并行度按运行时 profile 取值（native 4 / server、test 16）
    max_parallel = max_parallel_tasks(settings.runtime_profile)
    run_id, user_id = str(payload["run_id"]), str(payload.get("user_id", ""))
    execution_id = str(payload.get("task_id") or new_id())
    # run 行上的 provider/model 优先（建 run 时落库），payload 其次，缺省保持
    # 原硬编码 openai / gpt-4.1-mini；在 run 加载后于循环内最终确定。
    provider_id = str(payload.get("provider") or "openai")
    model_id = str(payload.get("model") or "gpt-4.1-mini")
    provider = None
    secret: dict[str, object] = {}
    # Cluster mode: one RoleModels per run + a provider cache keyed by
    # (provider, model) — at most 3 providers per run. role_models stays
    # None on the legacy (no effective cluster config) path.
    role_models = None
    role_secrets: dict[str, dict[str, object]] = {}
    role_providers: dict = {}
    cluster_checked = False
    # Per-seat reasoning (思考强度) block from the effective cluster config;
    # None until the config is read once at the first claimed batch.
    cluster_reasoning: object = None
    # Two-tier budget ledger (2026-08-05): per-lane budgets are independent —
    # write/review/revise/analyst/orchestrator/promise(奥莉维亚独立账本)。
    # lane budget = int(min(700K, lane model window) * 0.65) * lane node count;
    # lane overspending tightens ONLY that lane's input_budget (never starves
    # Olivia's promise pipeline the way the shared 300K pool did on 08-04).
    lane_budgets: dict[str, int] | None = None
    # Narrative-RAG scene pack: built at most once per run (NOT per
    # task) and cached here; None when disabled/failed.
    scene_pack_sections: dict[str, str] | None = None
    scene_pack_attempted = False
    # Quality gate between the review battery and the revise stage:
    # evaluated at most once per run. gate_reasons carries the FAIL
    # reasons into the revise-stage task contexts (rewrite reads them).
    gate_evaluated = False
    gate_reasons: list[str] = []
    # Final gate after recheck + bounded revise loop (quality over speed):
    # the rewritten draft must clear the same bar; each FAIL re-runs
    # rewrite/recheck until MAX_REVISE_ROUNDS, then ships degraded-marked.
    revise_round = 1
    final_gate_done = False
    commit_lock = asyncio.Lock()  # 提交串行化：事件 sequence 与 checkpoint 写不竞争
    # 认领时写 lease_expires_at（TTL=TASK_LEASE_TTL_SECONDS）；每轮迭代的回收
    # 逻辑只回收 lease 已过期的 RUNNING 任务，在飞任务（lease 未过期）不动。
    lease_ttl_supported = hasattr(AgentTaskModel, "lease_expires_at")

    async def add_event(uow, run, event_type: str, data: dict[str, object] | None = None) -> None:
        # run 行锁串行分配 sequence（并行提交/重复投递下不撞 uq_agent_events_run_sequence）
        locked = await uow.session.scalar(
            select(AgentRunModel).where(AgentRunModel.id == run.id).with_for_update().execution_options(populate_existing=True)
        )
        sequence = int(locked.event_cursor) + 1
        uow.session.add(AgentEventModel(id=new_id(), run_id=locked.id, sequence=sequence, event_type=event_type, payload=json.dumps(data or {}, sort_keys=True)))
        locked.event_cursor = sequence
        locked.updated_at = datetime.now(UTC)

    async def fail_run(uow, run, status: str, reason: str, event_type: str = "run.failed", data: dict[str, object] | None = None) -> None:
        run.status = status
        run.terminal_reason = reason
        await add_event(uow, run, event_type, data or {"reason": reason})

    async def retryable_failure_streak(uow, run) -> int:
        """run 级连续可重试供应商失败数（跨任务）：从最新事件往回数，遇
        task.succeeded 或非可重试失败即断。resume 后自然从 0 重计。"""
        rows = await uow.session.scalars(
            select(AgentEventModel)
            .where(AgentEventModel.run_id == run.id, AgentEventModel.event_type.in_(("task.failed", "task.succeeded")))
            .order_by(AgentEventModel.sequence.desc())
            .limit(50)
        )
        streak = 0
        for row in rows:
            if row.event_type == "task.succeeded":
                break
            if json.loads(row.payload).get("retryable_provider"):
                streak += 1
            else:
                break
        return streak

    def budget_lane(role: str, task_key: str) -> str:
        """Budget ledger lane: promise tasks get Olivia's own ledger even
        though they share the orchestrator model seat."""
        if str(task_key).startswith("promise_"):
            return "promise"
        return agent_role_to_cluster_role(role, task_key)

    def lane_model_ref(lane: str):
        """Model seat backing a budget lane (promise follows orchestrator)."""
        if role_models is None:
            return (provider_id, model_id)
        seat = "orchestrator" if lane == "promise" else lane
        return getattr(role_models, seat, None) or (provider_id, model_id)

    async def ensure_lane_budgets(uow, run, tasks) -> None:
        """Build per-lane budgets once (first claim round, models resolved)
        and recompute run.budget_limit as Σ lane budgets × headroom — the
        hard circuit breaker stays, just sized to the lane ledgers."""
        nonlocal lane_budgets
        if lane_budgets is not None:
            return
        lane_node_counts: dict[str, int] = {}
        for task in tasks:
            lane = budget_lane(str(task.role), str(task.task_key))
            lane_node_counts[lane] = lane_node_counts.get(lane, 0) + 1
        budgets: dict[str, int] = {}
        for lane, count in lane_node_counts.items():
            ref = lane_model_ref(lane)
            catalog_row = await uow.model_catalog.get(ref[0], ref[1])
            window = resolve_capabilities(catalog_row).context_window
            budgets[lane] = int(min(context_budget_cap(), window) * CONTEXT_INPUT_RATIO) * count
        lane_budgets = budgets
        # Never lower an externally raised limit: a BUDGET_EXHAUSTED retry
        # bumps budget_limit via the API, and a re-executed run lands here
        # again — overwriting it would re-trip the same circuit breaker.
        run.budget_limit = max(int(run.budget_limit or 0), int(sum(budgets.values()) * RUN_BUDGET_HEADROOM))
        await add_event(uow, run, "run.budget_recomputed", {"lane_budgets": budgets, "budget_limit": run.budget_limit})

    async def lane_usage(uow, run, tasks) -> dict[str, int]:
        """Settled tokens per budget lane (task.usage events carry task_id)."""
        lane_by_task = {task.id: budget_lane(str(task.role), str(task.task_key)) for task in tasks}
        usage: dict[str, int] = {}
        rows = await uow.session.scalars(select(AgentEventModel.payload).where(AgentEventModel.run_id == run.id, AgentEventModel.event_type == "task.usage"))
        for event_payload in rows:
            data = json.loads(event_payload)
            lane = lane_by_task.get(data.get("task_id"))
            if lane:
                usage[lane] = usage.get(lane, 0) + int(data.get("total_tokens") or 0)
        return usage

    async def notify_auto_pause(*, run_id: str, idempotency_key: str | None, streak: int, provider: str, model: str, error_text: str) -> None:
        """总调度自动暂停提醒：追加到 swarm 会话消息（章节 run 无消息则经批次
        idempotency_key 找父分析 run 的消息）并发 SSE，ChatPage 订阅即刷新。
        同一 run 最多提醒 AUTO_PAUSE_MAX_NOTICES 次，第 N 次文案建议换模型。"""
        try:
            from proseforge.application.agents.batch_dispatch import parse_batch_key
            from proseforge.infrastructure.events.hybrid import HybridEventStream

            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                notices = len(list(await uow.session.scalars(select(AgentEventModel).where(AgentEventModel.run_id == run_id, AgentEventModel.event_type == "run.auto_paused"))))
                if notices > AUTO_PAUSE_MAX_NOTICES:
                    return  # 已暂停但不再刷屏（事件仍落账）
                message = await uow.session.scalar(select(MessageModel).where(MessageModel.agent_run_id == run_id))
                if message is None and idempotency_key:
                    parsed = parse_batch_key(idempotency_key)
                    if parsed is not None:
                        message = await uow.session.scalar(select(MessageModel).where(MessageModel.agent_run_id == parsed[0]))
                if message is None:
                    return
                text = (
                    f"\n\n总调度：模型访问不稳定（{provider}/{model} 连续 {streak} 次请求失败：{error_text[:80]}），"
                    "已自动暂停本章写作。系统每 10 分钟自动试探恢复（最多 2 次），也可在右侧进度面板手动「恢复」。"
                )
                if notices >= AUTO_PAUSE_MAX_NOTICES:
                    text += "已多次自动暂停，建议在设置中更换模型后再恢复。"
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
            # 提醒失败绝不拖垮执行器：PAUSED 状态与 run.auto_paused 事件已落账
            logger.exception("auto-pause notify failed run_id=%s", run_id)

    async def load_scene_pack(project_id: str, goal: str) -> dict[str, str] | None:
        """Narrative-RAG scene pack sections, built at most once per
        run. Any failure degrades to None (same silent-fallback style
        as generate_chat)."""
        nonlocal scene_pack_sections, scene_pack_attempted
        if scene_pack_attempted:
            return scene_pack_sections
        scene_pack_attempted = True
        try:
            async with SqlAlchemyUnitOfWork(session_factory) as rag_uow:
                project_mode = await rag_uow.session.scalar(select(ProjectModel.mode).where(ProjectModel.id == project_id))
                rag_enabled = project_mode == "work" and await narrative_rag_switch_enabled(rag_uow, user_id)
                if rag_enabled:
                    indexable = int(await rag_uow.session.scalar(select(func.count(ChapterModel.id)).where(ChapterModel.project_id == project_id, ChapterModel.active_version_id.isnot(None))) or 0)
                    if not indexable:
                        # Empty index: retrieval provably returns nothing
                        # — skip the embedding spend, leave an audit event.
                        run_row = await rag_uow.session.scalar(select(AgentRunModel).where(AgentRunModel.id == run_id))
                        if run_row is not None:
                            await add_event(rag_uow, run_row, "rag.skipped_empty_index", {})
                            await rag_uow.commit()
                        rag_enabled = False
            if rag_enabled:
                # Retrieval intent: the orchestrator slot (cluster mode) or
                # the run model (legacy) distills the goal into a short
                # retrieval query inside the unified proxy; any failure
                # falls back to the raw goal.
                if role_models is not None:
                    # Unconfigured orchestrator slot follows the write model
                    # (same fallback as swarm_entry's intent classification).
                    intent_ref = role_models.orchestrator or role_models.write
                    intent_provider, intent_model = role_providers.get(intent_ref), intent_ref[1]
                else:
                    intent_provider, intent_model = provider, model_id
                # Swarm chat runs carry a placeholder assistant message
                # linked at run creation (messages.agent_run_id): attach the
                # retrieval snapshot to it so the cluster output's reference
                # sources are visible. Headless runs (no linked message)
                # keep the snapshot's conversation/message ids NULL.
                snapshot_conversation_id: str | None = None
                snapshot_message_id: str | None = None
                async with SqlAlchemyUnitOfWork(session_factory) as link_uow:
                    swarm_message_id = await link_uow.session.scalar(select(MessageModel.id).where(MessageModel.agent_run_id == run_id))
                    if swarm_message_id is not None:
                        snapshot_message_id = swarm_message_id
                        snapshot_conversation_id = await link_uow.conversations.conversation_id_for_message(swarm_message_id)

                async def on_query(_distilled: str, intent_event: dict[str, object]) -> None:
                    # Audit hook: the rag.query_intent event lands (committed)
                    # after distillation and before the pack build — same
                    # ordering as the pre-proxy inline flow.
                    async with SqlAlchemyUnitOfWork(session_factory) as intent_uow:
                        run_row = await intent_uow.session.scalar(select(AgentRunModel).where(AgentRunModel.id == run_id))
                        if run_row is not None:
                            await add_event(intent_uow, run_row, "rag.query_intent", intent_event)
                            await intent_uow.commit()

                async with SqlAlchemyUnitOfWork(session_factory) as proxy_uow:
                    pack = await retrieve_for_context(
                        proxy_uow, project_id=project_id, query=goal,
                        orchestrator_ref=(intent_provider, intent_model),
                        conversation_id=snapshot_conversation_id, message_id=snapshot_message_id,
                        on_query=on_query,
                    )
                if pack is not None and pack.text:
                    scene_pack_sections = dict(pack.sections)
        except Exception:
            logger.exception("agent run scene pack failed run_id=%s; continuing without", run_id)
        return scene_pack_sections

    async def writeback_run_message(final_status: str, terminal_reason: str | None) -> None:
        """Write the deterministic run summary back to the swarm
        assistant message linked via messages.agent_run_id, then
        publish the same completion/failure event shape generate_chat
        uses so the ChatPage subscription refreshes. Never raises:
        a writeback failure must not break terminal bookkeeping.

        The implementation lives in application/messages/sweeper.py so the
        message sweeper can replay the exact same writeback for messages
        stranded when this call's commit fails."""
        from proseforge.application.messages.sweeper import (
            writeback_run_message as shared_writeback,
        )

        await shared_writeback(session_factory, settings, run_id, final_status, terminal_reason)

    async def run_terminal_hooks(final_status: str, terminal_reason: str | None) -> None:
        """Shared terminal-exit bookkeeping: the swarm message writeback,
        then the batch-write dispatcher hook (analyze run -> serial
        per-chapter write-run chain; batch chapter run -> next chapter).
        The hook never raises, so it cannot overturn terminal state."""
        await writeback_run_message(final_status, terminal_reason)
        from proseforge.application.agents.batch_dispatch import on_run_terminal

        await on_run_terminal(session_factory, settings, run_id=run_id, user_id=user_id, status=final_status)

    async def maybe_evaluate_gate(uow, run, tasks) -> bool:
        """Quality gate between the review battery and the revise stage.

        Returns True when the caller must `continue` (gate PASSED: the
        council + revise-stage tasks were SKIPPED and committed — 合议只在
        NEEDS_REVISE 时才有活干，PASS 不触发不计费). NEEDS_REVISE
        returns False — the gate.evaluated event commits with the claim
        batch and review_council/merge are claimed normally.
        """
        nonlocal gate_evaluated, gate_reasons
        if gate_evaluated:
            return False
        by_key = {task.task_key: task for task in tasks}
        review_keys = [key for key in ("review_continuity", "review_adversarial", "review_style") if key in by_key]
        merge_task = by_key.get("merge")
        if not review_keys or merge_task is None or merge_task.status != "PENDING":
            return False
        if any(by_key[key].status not in {"SUCCEEDED", "SKIPPED"} for key in review_keys):
            return False  # reviewers still in flight; FAILED ends the run elsewhere
        gate_evaluated = True
        task_key_by_id = {task.id: task.task_key for task in tasks}
        scene_payload: dict | None = None
        review_payloads: list[dict] = []
        for artifact_row in await uow.session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.run_id == run.id)):
            try:
                artifact_payload = json.loads(artifact_row.payload)
            except ValueError:
                continue
            if not isinstance(artifact_payload, dict):
                continue
            artifact_key = task_key_by_id.get(artifact_row.task_id, "")
            # Draft under review: the select winner (production scene_a/b/c
            # graph) or the legacy single "scene" task's draft.
            if artifact_key in {"select", "scene"} and scene_payload is None:
                scene_payload = artifact_payload
            elif artifact_key in review_keys:
                review_payloads.append(artifact_payload)
        result = evaluate_gate(goal=run.goal or "", scene_payload=scene_payload, review_payloads=review_payloads)
        await add_event(uow, run, "gate.evaluated", {"passed": result.passed, "reasons": result.reasons, "warnings": result.warnings})
        if not result.passed:
            gate_reasons = list(result.reasons)
            return False
        for key in ("review_council", "merge", "rewrite", "recheck"):
            target = by_key.get(key)
            if target is not None and target.status == "PENDING":
                target.status = "SKIPPED"
                await add_event(uow, run, "task.skipped", {"task_id": target.id, "task_key": key, "reason": "gate passed"})
        await uow.commit()
        return True

    async def maybe_evaluate_final_gate(uow, run, tasks) -> bool:
        """Final quality gate after recheck (quality over speed).

        The rewritten draft must clear the same bar as the initial gate —
        word floor + required clues (quality_gate, on the rewrite content)
        plus evidenced-high findings from the recheck review. FAIL re-runs
        rewrite/recheck (merge is reused: same reviews, same buckets) up to
        MAX_REVISE_ROUNDS; at the cap the draft ships with a
        chapter.quality_degraded audit event — a marked chapter beats a
        missing one for batch continuity, and the run detail shows what
        stayed unresolved. Returns True when the caller must `continue`
        (a new revise round was scheduled and committed).
        """
        nonlocal revise_round, final_gate_done, gate_reasons
        if final_gate_done:
            return False
        by_key = {task.task_key: task for task in tasks}
        recheck_task = by_key.get("recheck")
        if recheck_task is None or by_key.get("rewrite") is None or recheck_task.status != "SUCCEEDED":
            return False
        final_gate_done = True
        task_key_by_id = {task.id: task.task_key for task in tasks}
        rewrite_payload: dict | None = None
        recheck_payload: dict | None = None
        # Ordered by id (time-ordered new_id): the LATEST rewrite artifact
        # is the current round's final draft.
        for artifact_row in await uow.session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.run_id == run.id).order_by(AgentArtifactModel.id)):
            try:
                artifact_payload = json.loads(artifact_row.payload)
            except ValueError:
                continue
            if not isinstance(artifact_payload, dict):
                continue
            artifact_key = task_key_by_id.get(artifact_row.task_id, "")
            if artifact_key == "rewrite":
                rewrite_payload = artifact_payload
            elif artifact_key == "recheck":
                recheck_payload = artifact_payload
        if rewrite_payload is None:
            return False  # recheck without a rewrite artifact: let the run finish
        result = evaluate_gate(goal=run.goal or "", scene_payload=rewrite_payload, review_payloads=[recheck_payload] if recheck_payload else [])
        if result.passed:
            await add_event(uow, run, "gate.final_passed", {"round": revise_round, "warnings": result.warnings})
            await uow.commit()
            return False
        if revise_round >= MAX_REVISE_ROUNDS:
            await add_event(uow, run, "chapter.quality_degraded", {"rounds": revise_round, "reasons": result.reasons})
            await uow.commit()
            return False
        # Next revise round: reset rewrite/recheck; the rewrite prompt reads
        # the final-gate reasons via context["gate_reasons"], so round 2
        # knows exactly what the final draft still lacks.
        gate_reasons = list(result.reasons)
        for key in ("rewrite", "recheck"):
            target = by_key[key]
            target.status = "PENDING"
            target.lease_owner = None
        await add_event(uow, run, "gate.recheck_failed", {"round": revise_round, "reasons": result.reasons})
        revise_round += 1
        final_gate_done = False
        await uow.commit()
        return True

    async def writeback_chapter() -> None:
        """Delegate to the module-level shared implementation so the
        message sweeper can replay a byte-identical writeback when this
        post-completion call's commit fails (run already COMPLETED)."""
        await writeback_chapter_for_run(session_factory, settings, run_id=run_id, user_id=user_id)

    try:
        while True:
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                run = await uow.session.scalar(select(AgentRunModel).where(AgentRunModel.id == run_id, AgentRunModel.user_id == user_id))
                if run is None:
                    return "run-not-found"
                # run 级模型选择覆盖 payload 缺省（None/空串 → 沿用 payload/默认）
                if run.provider:
                    provider_id = str(run.provider)
                if run.model:
                    model_id = str(run.model)
                if run.status in {"CANCELLED", "PAUSED"}:
                    if run.status == "CANCELLED":
                        await run_terminal_hooks("CANCELLED", run.terminal_reason)
                    return run.status.lower()
                if run.status in {"COMPLETED", "FAILED", "BUDGET_EXHAUSTED"}:
                    # 重复投递/并发 executor：run 已被另一实例收场，直接退出，
                    # 避免重复 writeback 与重复 run.completed 事件。
                    return run.status.lower()
                if run.fault_mode == "provider_timeout":
                    raise TimeoutError("injected provider timeout")
                if run.fault_mode == "malformed_json":
                    json.loads("{malformed agent output")
                if run.fault_mode == "budget_exhaustion":
                    run.status = "BUDGET_EXHAUSTED"
                    run.terminal_reason = "injected budget exhaustion"
                    await add_event(uow, run, "run.budget_exhausted", {"injected": True})
                    await uow.commit()
                    return "budget-exhausted"
                if run.status == "PENDING":
                    run.status = "RUNNING"
                    await add_event(uow, run, "run.started")
                tasks = list(await uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run.id).order_by(AgentTaskModel.id)))
                # 每轮迭代都回收 lease 过期的在飞任务（不止首轮）：worker 重启导致
                # celery 重投时，新 executor 首轮看到的 lease 可能尚未过期，真正的
                # 过期回收发生在后续迭代；在飞任务的 renewer 会持续续期，不会误收。
                now = datetime.now(UTC)
                recovered = [task for task in tasks if task.status == "RUNNING" and _lease_expired(task, now, lease_ttl_supported)]
                for task in recovered:
                    task.status = "PENDING"
                    task.last_error = "worker restarted before checkpoint commit"
                    task.lease_owner = None
                    await add_event(uow, run, "task.recovered", {"task_id": task.id, "task_key": task.task_key})
                if recovered:
                    await uow.commit()
                    continue
                if await maybe_evaluate_gate(uow, run, tasks):
                    continue
                if await maybe_evaluate_final_gate(uow, run, tasks):
                    continue
                succeeded = {task.task_key for task in tasks if task.status in {"SUCCEEDED", "SKIPPED"}}
                pending_all = [task for task in tasks if task.status == "PENDING"]
                if not pending_all:
                    failed = [task for task in tasks if task.status == "FAILED"]
                    if failed:
                        # 有任务 FAILED 且无 PENDING 可重试：run 必须 FAILED 收场
                        await fail_run(uow, run, "FAILED", "task(s) failed without retry", data={"reason": "task(s) failed without retry", "failed_tasks": [task.task_key for task in failed]})
                        await uow.commit()
                        await run_terminal_hooks("FAILED", run.terminal_reason)
                        return "failed"
                    run.status = "COMPLETED"
                    await add_event(uow, run, "run.completed")
                    await uow.commit()
                    await writeback_chapter()
                    await run_terminal_hooks("COMPLETED", None)
                    return "completed"
                # 退避等待中的 PENDING（next_attempt_at 在未来）不参与本轮认领；
                # 全部在等时睡到最近到期时间再重扫，绝不误判死局 FAILED。等待期间
                # 顺手刷新 run.updated_at——sweeper 按 updated_at 过期清扫滞留 run，
                # 退避等待不是滞留（sweeper 侧另有 next_attempt_at 豁免双保险）。
                waiting = [task for task in pending_all if _retry_waiting(task, now)]
                pending = [task for task in pending_all if task not in waiting]
                if not pending:
                    next_due = min(_aware_dt(task.next_attempt_at, now) for task in waiting)
                    run.updated_at = datetime.now(UTC)
                    await uow.commit()
                    await asyncio.sleep(min(max(1.0, (next_due - datetime.now(UTC)).total_seconds()), 30.0))
                    continue
                ready = [task for task in pending if set(json.loads(task.depends_on)) <= succeeded]
                if not ready:
                    if any(task.status == "RUNNING" for task in tasks):
                        # 在飞任务由并发/重投 executor 持有（lease 未到期）：本迭代
                        # 无可认领，等一个租约周期再重扫——lease 过期后由上方回收
                        # 逻辑转 PENDING，而不是把 run 误判为死局 FAILED。
                        await asyncio.sleep(max(5, TASK_LEASE_TTL_SECONDS // 3))
                        continue
                    if waiting:
                        # 就绪任务都在退避等待（依赖它们的下游也未到期）：睡到
                        # 最近到期时间重扫，不死局 FAILED。
                        next_due = min(_aware_dt(task.next_attempt_at, now) for task in waiting)
                        run.updated_at = datetime.now(UTC)
                        await uow.commit()
                        await asyncio.sleep(min(max(1.0, (next_due - datetime.now(UTC)).total_seconds()), 30.0))
                        continue
                    await fail_run(uow, run, "FAILED", "task dependency could not be satisfied")
                    await uow.commit()
                    await run_terminal_hooks("FAILED", run.terminal_reason)
                    return "failed"
                claimed: list[AgentTaskModel] = []
                for task in ready:
                    if len(claimed) >= max_parallel:
                        break
                    if lane_budgets is not None and run.budget_used + task.token_budget > run.budget_limit:
                        # 起前估算（token_budget）超额：durable BUDGET_EXHAUSTED。
                        # lane_budgets 未建（首轮认领前）不查——旧占位上限会误杀；
                        # 建账后该上限 = Σ车道预算×headroom，作失控熔断器保留。
                        task.status = "FAILED"
                        task.last_error = "budget exhausted"
                        for claimed_task in claimed:
                            # Same rollback as the missing-credential path:
                            # tasks already claimed in this batch go back to
                            # PENDING instead of sitting RUNNING until their
                            # lease expires.
                            claimed_task.status = "PENDING"
                            claimed_task.lease_owner = None
                        run.status = "BUDGET_EXHAUSTED"
                        run.terminal_reason = "task token budget exceeds remaining run budget"
                        await add_event(uow, run, "run.budget_exhausted", {"task_id": task.id, "required": task.token_budget, "remaining": run.budget_limit - run.budget_used})
                        await uow.commit()
                        await run_terminal_hooks("BUDGET_EXHAUSTED", run.terminal_reason)
                        return "budget-exhausted"
                    next_attempt = int(task.attempts or 0) + 1
                    lease_owner = f"celery:{run.id}:{task.task_key}:{execution_id}"
                    values = {
                        "status": "RUNNING",
                        "attempts": next_attempt,
                        "checkpoint_id": f"{run.id}:{task.task_key}:{next_attempt}",
                        "lease_owner": lease_owner,
                        "next_attempt_at": None,  # 认领即清退避等待标记（到期才进 ready）
                    }
                    if lease_ttl_supported:
                        values["lease_expires_at"] = datetime.now(UTC) + timedelta(seconds=TASK_LEASE_TTL_SECONDS)
                    claimed_update = await uow.session.execute(
                        update(AgentTaskModel)
                        .where(AgentTaskModel.id == task.id, AgentTaskModel.status == "PENDING")
                        .values(**values)
                    )
                    if claimed_update.rowcount != 1:
                        continue
                    await uow.session.refresh(task)
                    await add_event(uow, run, "task.started", {"task_id": task.id, "task_key": task.task_key, "role": task.role})
                    await add_event(uow, run, "task.lease_acquired", {"task_id": task.id, "task_key": task.task_key, "lease_owner": task.lease_owner, "lease_ttl_seconds": TASK_LEASE_TTL_SECONDS})
                    claimed.append(task)
                if claimed and not cluster_checked:
                    # The effective cluster config is read ONCE at the start
                    # of the execution loop. Swarm ignores the writing-model
                    # lock (locked=None): cluster-mode models come from the
                    # cluster config card only (project override > global).
                    # single_model runs (normal-mode dispatch) skip this —
                    # every lane stays on the run row's model.
                    cluster_checked = True
                    effective_cluster = await get_effective_cluster_config(uow, user_id, run.project_id)
                    # Reasoning applies regardless of mode: single-model runs
                    # get the same elastic matrix on the run row's model.
                    cluster_reasoning = effective_cluster.config.get("reasoning")
                    if not run.single_model and effective_cluster.source != "none" and effective_cluster.config.get("mode") == "cluster":
                        pool = await available_model_refs(uow, user_id)
                        role_models = resolve_from_config(
                            effective_cluster.config, pool=pool, locked=None,
                            requested=(provider_id, model_id),
                        )
                if claimed and role_models is not None and not role_secrets:
                    # Decrypt one credential per needed provider (<= 5).
                    raw_key = derive_key(settings.master_key.get_secret_value())
                    for role_provider_id in sorted({ref[0] for ref in (role_models.write, role_models.review, role_models.revise, role_models.orchestrator, role_models.analyst) if ref is not None}):
                        role_credential = await uow.credentials.get_for_user(user_id, role_provider_id)
                        if role_credential is None:
                            for task in claimed:
                                task.status = "PENDING"
                                task.lease_owner = None
                            await fail_run(uow, run, "FAILED", "provider-or-project-not-configured")
                            await uow.commit()
                            await run_terminal_hooks("FAILED", run.terminal_reason)
                            return "provider-or-project-not-configured"
                        role_associated = f"{user_id}:{role_provider_id}:{role_credential.id}".encode()
                        role_secrets[role_provider_id] = json.loads(CredentialCipher(raw_key).decrypt(base64.b64decode(role_credential.encrypted_payload), associated_data=role_associated))
                if claimed and role_models is None and provider is None:
                    # 解析 run owner 凭据（与 generate_novel 同一解密流程）；模型调用仍在事务外
                    credential = await uow.credentials.get_for_user(user_id, provider_id)
                    if credential is None:
                        for task in claimed:
                            task.status = "PENDING"
                            task.lease_owner = None
                        await fail_run(uow, run, "FAILED", "provider-or-project-not-configured")
                        await uow.commit()
                        await run_terminal_hooks("FAILED", run.terminal_reason)
                        return "provider-or-project-not-configured"
                    associated = f"{user_id}:{provider_id}:{credential.id}".encode()
                    raw_key = derive_key(settings.master_key.get_secret_value())
                    secret = json.loads(CredentialCipher(raw_key).decrypt(base64.b64decode(credential.encrypted_payload), associated_data=associated))
                task_key_by_id = {task.id: task.task_key for task in tasks}
                artifacts_snapshot = [
                    {"id": row.id, "task_key": task_key_by_id.get(row.task_id, ""), "artifact_type": row.artifact_type, "preview": row.preview}
                    for row in await uow.session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.run_id == run.id))
                ]
                claimed_snapshot = [{"id": task.id, "task_key": task.task_key, "role": task.role, "token_budget": task.token_budget, "lease_owner": task.lease_owner} for task in claimed]
                # Two-tier ledger: build lane budgets once (needs resolved
                # models), then read settled lane usage for tightening below.
                await ensure_lane_budgets(uow, run, tasks)
                used_by_lane = await lane_usage(uow, run, tasks)
                # Per-role input budget = int(min(budget cap, model window) *
                # CONTEXT_INPUT_RATIO), resolved in THIS transaction —
                # per-task catalog sessions inside run_claimed would
                # serialize sqlite connections and cap parallelism.
                for info in claimed_snapshot:
                    if role_models is not None:
                        # task_key wins over the role: review_continuity is a
                        # review-lane task while recheck (same role) is revise.
                        budget_ref = getattr(role_models, agent_role_to_cluster_role(str(info["role"]), str(info["task_key"])))
                    else:
                        budget_ref = (provider_id, model_id)
                    catalog_row = await uow.model_catalog.get(budget_ref[0], budget_ref[1])
                    task_capabilities = resolve_capabilities(catalog_row)
                    info["input_budget"] = int(min(context_budget_cap(), task_capabilities.context_window) * CONTEXT_INPUT_RATIO)
                    # Lane self-tightening: only the overspending lane's calls
                    # shrink; sibling lanes (and Olivia's promise ledger) are
                    # untouched. Audit event per tightening, never silent.
                    lane = budget_lane(str(info["role"]), str(info["task_key"]))
                    lane_budget = (lane_budgets or {}).get(lane)
                    if lane_budget:
                        lane_ratio = used_by_lane.get(lane, 0) / lane_budget
                        tighten_scale = 1.0
                        if lane_ratio > LANE_TIGHTEN_HIGH:
                            tighten_scale = 0.25
                        elif lane_ratio > LANE_TIGHTEN_MID:
                            tighten_scale = 0.5
                        if tighten_scale < 1.0:
                            info["input_budget"] = int(info["input_budget"] * tighten_scale)
                            await add_event(uow, run, "context.budget_tightened", {"task_key": info["task_key"], "lane": lane, "lane_used": used_by_lane.get(lane, 0), "lane_budget": lane_budget, "scale": tighten_scale})
                    # Elastic reasoning (集群思考强度): the seat level from the
                    # cluster config is the ceiling; task type decides the
                    # actual tier — prose tasks (scene_*/rewrite) keep the seat
                    # level with a max_output reserve, small JSON tasks drop to
                    # none/low so thinking can never burn their output budget
                    # (the deepseek-v4 empty-JSON lesson). Unsupported tiers
                    # step down with a warning; unknown profiles fall to auto.
                    seat_level = reasoning_level_for(cluster_reasoning, agent_role_to_cluster_role(str(info["role"]), str(info["task_key"])))
                    base_max_output = resolve_max_output_tokens(info, str(info["role"]))
                    task_reasoning = resolve_task_reasoning(str(info["task_key"]), seat_level, task_capabilities, base_max_output)
                    info["reasoning"] = task_reasoning["provider_parameter"]
                    info["max_output_boost"] = int(task_reasoning["max_output"]) - base_max_output
                    if task_reasoning["level"] != "auto" or task_reasoning["warnings"]:
                        await add_event(uow, run, "reasoning.resolved", {"task_key": info["task_key"], "role": info["role"], "level": task_reasoning["level"], "max_output_boost": info["max_output_boost"], "warnings": task_reasoning["warnings"]})
                run_snapshot = {"id": run.id, "goal": run.goal, "goal_hash": run.goal_hash, "graph_revision": run.graph_revision, "project_id": run.project_id, "chapter_id": run.chapter_id, "base_version_id": run.base_version_id}
                await uow.commit()

            if provider is None and (role_models is None or not role_providers):
                try:
                    if role_models is not None:
                        # One provider instance per distinct role ModelRef.
                        # analyst included: it runs on its own seat when
                        # configured, otherwise on the orchestrator's model.
                        for ref in {role_models.write, role_models.review, role_models.revise, role_models.orchestrator, role_models.analyst}:
                            role_providers[ref] = build_provider(ref[0], str(role_secrets[ref[0]]["api_key"]), role_secrets[ref[0]].get("base_url"))
                    else:
                        provider = build_provider(provider_id, str(secret["api_key"]), secret.get("base_url"))
                except KeyError:
                    async with SqlAlchemyUnitOfWork(session_factory) as uow:
                        run = await uow.session.scalar(select(AgentRunModel).where(AgentRunModel.id == run_id, AgentRunModel.user_id == user_id))
                        if run is None:
                            return "run-not-found"
                        for info in claimed_snapshot:
                            task = await uow.session.get(AgentTaskModel, info["id"])
                            if task is not None and task.status == "RUNNING":
                                task.status = "PENDING"
                                task.lease_owner = None
                        await fail_run(uow, run, "FAILED", "provider-not-supported")
                        await uow.commit()
                    await run_terminal_hooks("FAILED", "provider-not-supported")
                    return "provider-not-supported"

            async def run_claimed(info: dict[str, object], pack_sections: dict[str, str] | None) -> str:
                # B023 false positive: this closure is fully consumed in the same
                # while-iteration via `await bounded_parallel(...)` below, so the
                # loop variables it captures are never rebound before it runs.
                if role_models is not None:  # noqa: B023 -- consumed in-iteration, see above
                    # Per-task model: (role, task_key) -> cluster role ->
                    # cached provider instance (unknown roles fall back to write).
                    task_ref = getattr(role_models, agent_role_to_cluster_role(str(info["role"]), str(info["task_key"])))  # noqa: B023 -- consumed in-iteration, see above
                    task_provider = role_providers[task_ref]
                    task_provider_id, task_model = task_ref
                else:
                    task_provider, task_provider_id, task_model = provider, provider_id, model_id  # noqa: B023 -- consumed in-iteration, see above
                context = {
                    "run": run_snapshot,  # noqa: B023 -- consumed in-iteration, see above
                    "task": info,
                    "provider": task_provider,
                    "provider_id": task_provider_id,
                    "model": task_model,
                    "uow_factory": lambda: SqlAlchemyUnitOfWork(session_factory),
                    "artifacts": artifacts_snapshot,  # noqa: B023 -- consumed in-iteration, see above
                    "scene_pack": pack_sections,
                    "input_budget": info.get("input_budget"),
                    "reasoning": info.get("reasoning"),
                    "gate_reasons": list(gate_reasons),
                }
                result: RoleResult | None = None
                error: Exception | None = None
                async def renew_task_lease() -> None:
                    while True:
                        await asyncio.sleep(max(5, TASK_LEASE_TTL_SECONDS // 3))
                        async with SqlAlchemyUnitOfWork(session_factory) as lease_uow:
                            live = await lease_uow.session.get(AgentTaskModel, str(info["id"]))
                            if live is None or live.status != "RUNNING" or live.lease_owner != info["lease_owner"]:
                                return
                            live.lease_expires_at = datetime.now(UTC) + timedelta(seconds=TASK_LEASE_TTL_SECONDS)
                            await lease_uow.commit()

                renewer = asyncio.create_task(renew_task_lease())
                try:
                    # 模型调用在任何数据库事务之外
                    result = await handler_for(str(info["role"]))(context)
                except Exception as exc:  # 解析/模型错误按 malformed_json 语义重试或失败
                    error = exc
                finally:
                    renewer.cancel()
                    await asyncio.gather(renewer, return_exceptions=True)
                async with commit_lock, SqlAlchemyUnitOfWork(session_factory) as uow:
                    run = await uow.session.scalar(select(AgentRunModel).where(AgentRunModel.id == run_id, AgentRunModel.user_id == user_id))
                    task = await uow.session.get(AgentTaskModel, info["id"])
                    if run is None or task is None:
                        return "run-not-found"
                    if task.lease_owner != info["lease_owner"]:
                        return "lease-unavailable"
                    if run.status != "RUNNING":
                        # PAUSED/CANCELLED（或异常终态）：退回 PENDING，不留半成品
                        task.status = "PENDING"
                        task.lease_owner = None
                        await uow.commit()
                        return run.status.lower()
                    if error is not None:
                        task.last_error = f"{type(error).__name__}: {str(error)[:200]}"
                        task.lease_owner = None
                        classified_error = classify_provider_error(error)
                        retryable_provider = isinstance(classified_error, RetryableProviderError) and not isinstance(error, PolicyDenied)
                        # 有效不可重试次数 = attempts − retryable_attempts（attempts 认领
                        # 时 +1；可重试失败只涨 retryable_attempts，不吃 3 次额度）。
                        # 认领提交与失败提交之间崩溃允许 ±1 误差，恢复重试兜底。
                        nonretry_attempts = int(task.attempts or 0) - int(task.retryable_attempts or 0)
                        auto_pause_streak = 0
                        auto_pause_key: str | None = None
                        if isinstance(error, PolicyDenied):
                            # 策略拒绝是确定性的：立即 FAILED（不重试），留 policy.denied 审计事件
                            task.status = "FAILED"
                            await add_event(uow, run, "policy.denied", {"task_id": task.id, "task_key": task.task_key, "role": task.role, "decision": "deny", "reason": str(error)[:200]})
                            await add_event(uow, run, "task.failed", {"task_id": task.id, "task_key": task.task_key, "error": type(error).__name__, "retry": False})
                        elif retryable_provider and int(task.retryable_attempts or 0) < RETRYABLE_MAX_ATTEMPTS:
                            # 可重试供应商错误（5xx/429/超时）：指数退避重排，独立计数
                            task.retryable_attempts = int(task.retryable_attempts or 0) + 1
                            delay = _retry_backoff_seconds(int(task.retryable_attempts))
                            task.status = "PENDING"
                            task.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
                            await add_event(uow, run, "task.failed", {"task_id": task.id, "task_key": task.task_key, "error": type(error).__name__, "retry": True, "retryable_provider": True, "delay_seconds": round(delay, 1)})
                            # run 级连败达阈值：自动暂停 + 总调度聊天提醒（主防线；
                            # 任务级 RETRYABLE_MAX_ATTEMPTS 是 resume 后的兜底）
                            auto_pause_streak = await retryable_failure_streak(uow, run)
                            if auto_pause_streak >= AUTO_PAUSE_STREAK:
                                run.status = "PAUSED"
                                auto_pause_key = run.idempotency_key  # commit 后属性过期，提前取
                                await add_event(uow, run, "run.auto_paused", {"streak": auto_pause_streak, "provider": task_provider_id, "model": task_model, "error": str(error)[:120]})
                        elif not retryable_provider and nonretry_attempts < DEFAULT_MAX_ATTEMPTS:
                            task.status = "PENDING"
                            task.next_attempt_at = None
                            await add_event(uow, run, "task.failed", {"task_id": task.id, "task_key": task.task_key, "error": type(error).__name__, "retry": True})
                        elif str(task.task_key) == RECHECK_TASK_KEY:
                            # Tail-review degradation: the recheck task audits
                            # the rewrite AFTER the writing product already
                            # exists, so a terminally failing recheck must not
                            # drag the whole run to FAILED. Mark it SUCCEEDED
                            # with a "review unavailable" warning artifact and
                            # let the run deliver COMPLETED. Only recheck gets
                            # this; every other task keeps the FAILED semantics.
                            warning_payload = {
                                "report_type": "ContinuityReport",
                                "summary": f"评审不可用：尾评审任务连续 {DEFAULT_MAX_ATTEMPTS} 次失败（{type(error).__name__}: {str(error)[:120]}），已跳过本次评审，写作产物不受影响。",
                                "issues": [],
                                "review_ids": [],
                                "verdict": "UNSUPPORTED",
                                "degraded": True,
                            }
                            raw_warning = json.dumps(warning_payload, ensure_ascii=False, sort_keys=True)
                            artifact = AgentArtifactModel(
                                id=new_id(), run_id=run.id, task_id=task.id, artifact_type="report",
                                sha256=hashlib.sha256(raw_warning.encode()).hexdigest(),
                                provenance=json.dumps({"task_id": task.id, "task_key": task.task_key, "role": task.role, "degraded": True}, sort_keys=True),
                                preview=raw_warning[:200],
                                payload=raw_warning,
                            )
                            uow.session.add(artifact)
                            task.status = "SUCCEEDED"
                            task.checkpoint_id = f"{run.id}:{task.task_key}:committed"
                            await add_event(uow, run, "task.degraded", {"task_id": task.id, "task_key": task.task_key, "error": type(error).__name__, "reason": "recheck review unavailable"})
                            await add_event(uow, run, "artifact.committed", {"artifact_id": artifact.id, "task_id": task.id, "sha256": artifact.sha256})
                            await add_event(uow, run, "task.succeeded", {"task_id": task.id, "task_key": task.task_key})
                        else:
                            task.status = "FAILED"
                            await add_event(uow, run, "task.failed", {"task_id": task.id, "task_key": task.task_key, "error": type(error).__name__, "retry": False})
                        await uow.commit()
                        if auto_pause_streak >= AUTO_PAUSE_STREAK:
                            # PAUSED 已落账（事务内）：提醒走事务外 best-effort，
                            # 在飞任务提交时会发现 run 非 RUNNING 退回 PENDING。
                            await notify_auto_pause(run_id=str(run_id), idempotency_key=auto_pause_key, streak=auto_pause_streak, provider=task_provider_id, model=task_model, error_text=str(error))
                            return "paused"
                        return "failed"
                    validation_error = validate_role_result(task.role, result)
                    if validation_error is not None:
                        # 服务端校验失败：任务 FAILED，run 继续其余任务
                        task.status = "FAILED"
                        task.last_error = validation_error
                        task.lease_owner = None
                        await add_event(uow, run, "task.failed", {"task_id": task.id, "task_key": task.task_key, "error": validation_error, "retry": False})
                        await uow.commit()
                        return "failed"
                    raw = json.dumps(result.payload, ensure_ascii=False, sort_keys=True).encode()
                    # preview 存 payload 内容摘要（下游 prompt 注入靠 preview 拿真实内容），
                    # 不再写 "role artifact_type" 字面量标签。
                    preview = json.dumps(result.payload, ensure_ascii=False, sort_keys=True)[:200]
                    artifact = AgentArtifactModel(
                        id=new_id(), run_id=run.id, task_id=task.id, artifact_type=result.artifact_type,
                        sha256=hashlib.sha256(raw).hexdigest(),
                        provenance=json.dumps({"task_id": task.id, "task_key": task.task_key, "role": task.role, "model": task_model, "provider": task_provider_id}, sort_keys=True),
                        preview=preview,
                        payload=json.dumps(result.payload, ensure_ascii=False, sort_keys=True),
                    )
                    uow.session.add(artifact)
                    task.status = "SUCCEEDED"
                    task.checkpoint_id = f"{run.id}:{task.task_key}:committed"
                    task.lease_owner = None
                    run.budget_used += result.used_tokens  # 实测 usage 结算（替代申报 token_budget 记账）
                    await add_event(uow, run, "task.usage", {"task_id": task.id, "task_key": task.task_key, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens, "total_tokens": result.used_tokens})
                    for extra in result.extra_events:
                        await add_event(uow, run, str(extra.get("event", "task.event")), {key: value for key, value in extra.items() if key != "event"})
                    await add_event(uow, run, "artifact.committed", {"artifact_id": artifact.id, "task_id": task.id, "sha256": artifact.sha256})
                    await add_event(uow, run, "task.succeeded", {"task_id": task.id, "task_key": task.task_key})
                    crash_after_artifact_commit = run.fault_mode == "crash_after_artifact_commit"
                    if crash_after_artifact_commit:
                        # 与 artifact/checkpoint 同一事务清开关：redelivery 观察到
                        # SUCCEEDED，只补 run.completed，不再产第二个 artifact。
                        run.fault_mode = None
                    done = sorted(await uow.session.scalars(select(AgentTaskModel.task_key).where(AgentTaskModel.run_id == run.id, AgentTaskModel.status == "SUCCEEDED")))
                    run.checkpoint_id = f"graph:{run.graph_revision}|done:{','.join(done)}|cursor:{run.event_cursor}|exec:{EXECUTOR_VERSION}"
                    await uow.commit()
                    if crash_after_artifact_commit:
                        import os
                        os._exit(137)
                    return "succeeded"

            pack_sections = await load_scene_pack(str(run_snapshot["project_id"]), str(run_snapshot.get("goal") or ""))
            # 记忆优先（先查再想）：每批次认领时快照一次已批准记忆切片，
            # 全部角色 handler 经 context 统一读取，不再各自查库。
            run_snapshot["memory_slice"] = await load_memory_slice(lambda: SqlAlchemyUnitOfWork(session_factory), run_snapshot)
            await bounded_parallel([partial(run_claimed, info, pack_sections) for info in claimed_snapshot], max_parallel)
    except Exception as exc:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            run = await uow.session.scalar(select(AgentRunModel).where(AgentRunModel.id == run_id, AgentRunModel.user_id == user_id))
            if run is not None:
                if run.status in {"CANCELLED", "PAUSED", "COMPLETED", "FAILED", "BUDGET_EXHAUSTED"}:
                    # A user-cancelled/paused (or already terminal) run keeps
                    # its status: only append the audit event, never overturn
                    # it to FAILED and never touch the linked message.
                    await add_event(uow, run, "run.error", {"reason": type(exc).__name__, "preserved_status": run.status})
                    await uow.commit()
                else:
                    run.status = "FAILED"
                    run.terminal_reason = type(exc).__name__
                    await add_event(uow, run, "run.failed", {"reason": type(exc).__name__})
                    await uow.commit()
                    await run_terminal_hooks("FAILED", run.terminal_reason)
        raise
    finally:
        await engine.dispose()

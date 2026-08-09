from __future__ import annotations

import asyncio
import logging
import os

from celery import Celery

from proseforge.application.agents.auto_resume import probe_auto_paused_runs_entry
from proseforge.application.messages.sweeper import sweep_stale_run_messages_entry
from proseforge.workflows.tasks import (
    HANDLERS,
    execute_agent_run,
    generate_chat,
    generate_novel,
    index_retrieval_document,
    recover_expired,
    rollup_recap_task,
    should_abort_workflow,
    summarize_chapter_task,
    sweep_pending_retrieval_jobs,
    sync_all_models,
)
from proseforge.workflows.tasks import (
    healthcheck as run_healthcheck,
)
from proseforge.workflows.v2_tasks import execute_v2_run, recover_expired_v2


def _setup_worker_logging() -> None:
    """Attach the app.log rotating file handler in the worker process.

    Mirrors the API lifespan bootstrap env resolution so both processes
    write to the same log_dir. Logging must never block worker startup.
    """
    try:
        from proseforge.runtime.logging import setup_logging
        from proseforge.runtime.paths import resolve_paths
        from proseforge.runtime.profile import RuntimeProfile
        from proseforge.settings import get_settings

        settings = get_settings()
        env = dict(os.environ)
        if settings.data_dir:
            env["PROSEFORGE_DATA_DIR"] = settings.data_dir
        env["PROSEFORGE_DATABASE_URL"] = settings.database_url
        paths = resolve_paths(RuntimeProfile(settings.runtime_profile), env)
        setup_logging(paths.log_dir)
    except Exception:  # logging setup is best-effort at worker startup
        logging.getLogger(__name__).debug("worker file logging not configured", exc_info=True)


_setup_worker_logging()


celery = Celery(
    "proseforge",
    broker=os.getenv("PROSEFORGE_REDIS_URL", "redis://redis:6379/0"),
    backend=os.getenv("PROSEFORGE_REDIS_URL", "redis://redis:6379/0"),
)
celery.conf.update(
    task_track_started=True,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={"visibility_timeout": 6 * 60 * 60},
    beat_schedule={
        "sync-provider-model-catalog-daily": {
            "task": "proseforge.providers.sync_all_models",
            "schedule": 24 * 60 * 60,
        },
        "recover-expired-workflows": {
            "task": "proseforge.workflows.recover_expired",
            "schedule": 60.0,
        },
        "recover-expired-v2-workflow-nodes": {
            "task": "proseforge.workflows.recover_expired_v2",
            "schedule": 60.0,
        },
        "sweep-pending-retrieval-jobs": {
            "task": "proseforge.retrieval.sweep_pending_jobs",
            "schedule": 300.0,
        },
        "sweep-stale-agent-run-messages": {
            "task": "proseforge.messages.sweep_stale_run_messages",
            "schedule": 300.0,
        },
        "probe-auto-paused-agent-runs": {
            "task": "proseforge.agents.probe_auto_paused_runs",
            "schedule": 600.0,
        },
    },
)


@celery.task(name="proseforge.workflows.generate_novel", bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=3)
def generate_novel_workflow(self, payload: dict[str, object]) -> str:
    # 注入当前 celery task id：handler 用它对照 checkpoint.active_task_id
    # 判断自己是否已被 resume/retry 的继任任务接替（旧任务须让位）。
    return asyncio.run(generate_novel({**payload, "task_id": str(self.request.id)}))


@celery.task(name="proseforge.healthcheck")
def healthcheck() -> str:
    return asyncio.run(run_healthcheck({}))


@celery.task(name="proseforge.providers.sync_all_models", bind=True, max_retries=0)
def sync_all_provider_models(self, payload: dict[str, object] | None = None) -> dict[str, int]:
    del self
    return asyncio.run(sync_all_models(payload or {}))


@celery.task(name="proseforge.workflows.recover_expired", bind=True, max_retries=0)
def recover_expired_workflows(self) -> int:
    del self
    return asyncio.run(recover_expired({}))


@celery.task(name="proseforge.chat.generate", bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=3)
def generate_chat_task(self, payload: dict[str, object]) -> str:
    """Run one durable chat generation task in the worker process."""
    del self
    return asyncio.run(generate_chat(payload))


@celery.task(name="proseforge.agents.execute_run", bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=3)
def execute_agent_run_task(self, payload: dict[str, object]) -> str:
    return asyncio.run(execute_agent_run({**payload, "task_id": str(self.request.id)}))


@celery.task(name="proseforge.workflows.execute_v2_run", bind=True, max_retries=0)
def execute_v2_run_task(self, payload: dict[str, object]) -> str:
    return asyncio.run(execute_v2_run({**payload, "task_id": str(self.request.id)}))


@celery.task(name="proseforge.workflows.recover_expired_v2", bind=True, max_retries=0)
def recover_expired_v2_workflow_nodes(self) -> dict[str, int]:
    del self
    return asyncio.run(recover_expired_v2({}))


@celery.task(name="proseforge.retrieval.index_document", bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=3)
def index_retrieval_document_task(self, payload: dict[str, object]) -> str:
    del self
    return asyncio.run(index_retrieval_document(payload))


@celery.task(name="proseforge.retrieval.sweep_pending_jobs", bind=True, max_retries=0)
def sweep_pending_retrieval_jobs_task(self) -> int:
    del self
    return asyncio.run(sweep_pending_retrieval_jobs({}))


@celery.task(name="proseforge.messages.sweep_stale_run_messages", bind=True, max_retries=0)
def sweep_stale_agent_run_messages_task(self) -> int:
    del self
    return asyncio.run(sweep_stale_run_messages_entry({}))


@celery.task(name="proseforge.agents.probe_auto_paused_runs", bind=True, max_retries=0)
def probe_auto_paused_agent_runs_task(self) -> int:
    """Beat prober for auto-paused runs (every 10 min, max 2 probes per run)."""
    del self
    return asyncio.run(probe_auto_paused_runs_entry({}))


@celery.task(name="proseforge.work.summarize_chapter", bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=3)
def summarize_chapter_celery_task(self, payload: dict[str, object]) -> dict[str, object]:
    del self
    return asyncio.run(summarize_chapter_task(payload))


@celery.task(name="proseforge.work.rollup_recap", bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=3)
def rollup_recap_celery_task(self, payload: dict[str, object]) -> dict[str, object]:
    del self
    return asyncio.run(rollup_recap_task(payload))


__all__ = ["HANDLERS", "celery", "should_abort_workflow"]

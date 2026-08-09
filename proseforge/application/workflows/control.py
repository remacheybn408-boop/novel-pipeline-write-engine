from __future__ import annotations

import json
from dataclasses import dataclass

from proseforge.domain.workflow.state import ALLOWED_TRANSITIONS

TASK_NAME = "proseforge.workflows.generate_novel"


@dataclass(frozen=True)
class WorkflowControlResult:
    run: object
    task_id: str | None = None
    idempotent_replay: bool = False

    @property
    def status(self) -> str:
        return str(self.run.status)


def workflow_command(*, user_id: str, chapter_numbers: list[int], provider: str, model: str, editor_model: str) -> dict[str, object]:
    return {
        "user_id": user_id,
        "chapter_numbers": chapter_numbers,
        "provider": provider,
        "model": model,
        "editor_model": editor_model,
    }


class WorkflowControlService:
    def __init__(self, uow, queue):
        self.uow = uow
        self.queue = queue

    async def execute(self, workflow_id: str, user_id: str, action: str, idempotency_key: str | None = None) -> WorkflowControlResult:
        targets = {"pause": "PAUSED", "cancel": "CANCELLED"}
        run = await self.uow.workflows.get_owned(workflow_id, user_id, lock=True)
        if run is None:
            raise LookupError("workflow not found")

        if idempotency_key is not None:
            stored = decode_checkpoint(run.checkpoint).get("control_results", {})
            if isinstance(stored, dict) and idempotency_key in stored:
                result = stored[idempotency_key]
                task_id = result.get("task_id") if isinstance(result, dict) else None
                return WorkflowControlResult(run=run, task_id=task_id, idempotent_replay=True)

        task_id = None
        if action in {"resume", "retry"}:
            command = await self.uow.workflows.get_command(run)
            if command is None:
                raise ValueError("workflow has no durable command")
            target = "QUEUED" if "QUEUED" in ALLOWED_TRANSITIONS.get(run.status, set()) else "RETRYING"
            payload = {"workflow_id": run.id, **command}
            if target not in ALLOWED_TRANSITIONS.get(run.status, set()):
                raise ValueError(f"invalid workflow transition: {run.status} -> {target}")
            # A successor always receives a fresh delivery owner. Clear the
            # paused predecessor's lease before enqueueing it, otherwise the
            # new task cannot acquire the run for up to the old TTL.
            run.lease_owner = None
            run.lease_expires_at = None
            run.heartbeat_at = None
            await self.uow.workflows.transition(run, target)
            await self.uow.commit()
            try:
                task_id = await self.queue.enqueue(TASK_NAME, payload)
            except Exception as exc:
                # The transition is durable before broker I/O. Convert it to a
                # visible retryable terminal state instead of leaving QUEUED.
                run.status = "FAILED"
                run.last_error = f"enqueue failed: {type(exc).__name__}"
                await self.uow.workflows.append_event(run.id, "FAILED", {"status": "FAILED", "reason": "enqueue_failed"})
                await self.uow.commit()
                raise
            await self.uow.workflows.set_task(run, task_id)
            self._store_control_result(run, idempotency_key, action, task_id)
            await self.uow.commit()
            return WorkflowControlResult(run=run, task_id=task_id)

        target = targets.get(action)
        if target is None:
            raise LookupError("workflow action not found")
        if target not in ALLOWED_TRANSITIONS.get(run.status, set()):
            raise ValueError(f"invalid workflow transition: {run.status} -> {target}")
        await self.uow.workflows.transition(run, target)
        self._store_control_result(run, idempotency_key, action, None)
        await self.uow.commit()
        return WorkflowControlResult(run=run)

    @staticmethod
    def _store_control_result(run, idempotency_key: str | None, action: str, task_id: str | None) -> None:
        """幂等键结果持久化在 checkpoint JSON 的 control_results 下（与 v2 同语义：
        重复键返回首次结果，不重复副作用）。无键时保持既有行为不变。"""
        if idempotency_key is None:
            return
        document = decode_checkpoint(run.checkpoint)
        results = document.setdefault("control_results", {})
        if isinstance(results, dict):
            results[idempotency_key] = {"action": action, "status": str(run.status), "task_id": task_id}
            run.checkpoint = json.dumps(document, ensure_ascii=False)


def decode_checkpoint(checkpoint: str | None) -> dict[str, object]:
    if not checkpoint:
        return {}
    try:
        value = json.loads(checkpoint)
    except json.JSONDecodeError:
        return {"phase": checkpoint}
    return value if isinstance(value, dict) else {}

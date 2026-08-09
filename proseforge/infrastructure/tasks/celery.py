from __future__ import annotations


class CeleryTaskQueue:
    """Application port backed by Redis/Celery in production.

    server profile 的队列实现：enqueue 永远走 broker，绝不回退本地
    SQLite 队列（回退防护在 Settings/runtime profile 校验层）。
    start/stop 为 no-op——worker 生命周期由独立 celery 进程管理。
    """

    def __init__(self, client=None):
        self.client = client

    def _client(self):
        client = self.client
        if client is None:
            from proseforge.workflows.celery_app import celery
            client = celery
        return client

    async def enqueue(self, task_name: str, payload: dict[str, object]) -> str:
        result = self._client().send_task(task_name, args=[payload])
        return str(result.id)

    async def cancel(self, task_id: str) -> bool:
        self._client().control.revoke(task_id, terminate=True)
        return True

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

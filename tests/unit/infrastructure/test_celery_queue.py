import pytest

from proseforge.infrastructure.tasks.celery import CeleryTaskQueue


@pytest.mark.asyncio
async def test_celery_queue_returns_broker_task_id(monkeypatch):
    class Result:
        id = "broker-task-1"

    class FakeCelery:
        def send_task(self, name, args):
            assert name == "proseforge.chat.generate"
            assert args == [{"message_id": "m1"}]
            return Result()

    assert await CeleryTaskQueue(FakeCelery()).enqueue("proseforge.chat.generate", {"message_id": "m1"}) == "broker-task-1"

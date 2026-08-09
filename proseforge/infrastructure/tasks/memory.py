from __future__ import annotations


class InMemoryTaskQueue:
    def __init__(self):
        self.tasks: list[tuple[str, dict[str, object]]] = []
        self.cancelled: set[str] = set()
        self.started = False

    async def enqueue(self, task_name: str, payload: dict[str, object]) -> str:
        self.tasks.append((task_name, payload))
        return f"task-{len(self.tasks)}"

    async def cancel(self, task_id: str) -> bool:
        for index, _entry in enumerate(self.tasks):
            if f"task-{index + 1}" == task_id and task_id not in self.cancelled:
                self.cancelled.add(task_id)
                return True
        return False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

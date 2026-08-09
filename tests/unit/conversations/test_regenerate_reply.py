from __future__ import annotations

import pytest

from proseforge.application.conversations.regenerate_reply import RegenerateReply
from proseforge.domain.conversation.entity import Message


class Repo:
    def __init__(self, siblings: int):
        self.siblings = siblings
        self.appended = []

    async def count_assistant_siblings(self, branch_id, parent_message_id):
        return self.siblings

    async def append_message(self, branch_id, role, content, client_request_id=None, status="COMPLETED", *, parent_message_id=None, generation_attempt=1):
        message = Message(f"m{len(self.appended)}", branch_id, role, content, status=status, parent_message_id=parent_message_id, generation_attempt=generation_attempt)
        self.appended.append(message)
        return message


class Uow:
    def __init__(self, repo): self.conversations = repo
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def commit(self): pass


class Queue:
    def __init__(self): self.enqueued = []

    async def enqueue(self, task, payload):
        self.enqueued.append((task, payload))
        return "task-1"


@pytest.mark.asyncio
async def test_regenerate_appends_sibling_with_incremented_attempt():
    repo = Repo(siblings=1)  # original assistant candidate already exists
    queue = Queue()
    message, task_id = await RegenerateReply(lambda: Uow(repo), queue).execute(branch_id="b", parent_message_id="u1", user_id="u", provider="openai", model="m", reasoning_level="auto")
    assert message.generation_attempt == 2
    assert message.parent_message_id == "u1"  # 同分支候选，不 fork
    assert message.branch_id == "b"
    assert message.status == "PENDING"
    assert task_id == "task-1"
    assert queue.enqueued[0][1]["message_id"] == message.id


@pytest.mark.asyncio
async def test_regenerate_enqueues_the_resolved_reasoning_level():
    # worker 端不再靠 payload.get("reasoning_level", "auto") 兜底——键总是显式带上。
    repo = Repo(siblings=1)
    queue = Queue()
    await RegenerateReply(lambda: Uow(repo), queue).execute(branch_id="b", parent_message_id="u1", user_id="u", provider="openai", model="m", reasoning_level="deep")
    assert queue.enqueued[0][1]["reasoning_level"] == "deep"


@pytest.mark.asyncio
async def test_regenerate_third_candidate_gets_attempt_three():
    repo = Repo(siblings=2)
    message, _ = await RegenerateReply(lambda: Uow(repo), Queue()).execute(branch_id="b", parent_message_id="u1", user_id="u", provider="openai", model="m", reasoning_level="auto")
    assert message.generation_attempt == 3


class LockingRepo(Repo):
    """Spy repo：记录 lock_regenerate 调用与 lock/count 顺序。"""

    def __init__(self, siblings: int):
        super().__init__(siblings)
        self.locks = []
        self.events = []

    async def lock_regenerate(self, parent_message_id):
        self.locks.append(parent_message_id)
        self.events.append("lock")

    async def count_assistant_siblings(self, branch_id, parent_message_id):
        self.events.append("count")
        return await super().count_assistant_siblings(branch_id, parent_message_id)


@pytest.mark.asyncio
async def test_regenerate_locks_parent_before_counting():
    repo = LockingRepo(siblings=1)
    message, _ = await RegenerateReply(lambda: Uow(repo), Queue()).execute(branch_id="b", parent_message_id="u1", user_id="u", provider="openai", model="m", reasoning_level="auto")
    assert repo.locks == ["u1"]
    assert repo.events[:2] == ["lock", "count"]  # 锁先于计数，串行化并发 regenerate
    assert message.generation_attempt == 2


class SequentialRepo(LockingRepo):
    """计数反映已追加候选，模拟并发下锁内的真实读取。"""

    async def count_assistant_siblings(self, branch_id, parent_message_id):
        self.events.append("count")
        return sum(1 for message in self.appended if message.parent_message_id == parent_message_id and message.role == "assistant")


@pytest.mark.asyncio
async def test_sequential_regenerates_increment_attempts_under_lock():
    repo = SequentialRepo(siblings=0)
    service = RegenerateReply(lambda: Uow(repo), Queue())
    first, _ = await service.execute(branch_id="b", parent_message_id="u1", user_id="u", provider="openai", model="m", reasoning_level="auto")
    second, _ = await service.execute(branch_id="b", parent_message_id="u1", user_id="u", provider="openai", model="m", reasoning_level="auto")
    assert [first.generation_attempt, second.generation_attempt] == [1, 2]
    assert repo.locks == ["u1", "u1"]

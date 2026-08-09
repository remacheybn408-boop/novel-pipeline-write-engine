import pytest

from proseforge.application.conversations.send_message import SendMessage


class Repo:
    def __init__(self):
        self.calls = []

    async def append_message(self, *args, **kwargs):
        self.calls.append(args)
        return type("Message", (), {"id": str(len(self.calls))})()


class Uow:
    def __init__(self, repo): self.conversations = repo
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def commit(self): pass


class Queue:
    def __init__(self):
        self.enqueued = []

    async def enqueue(self, name, payload):
        self.enqueued.append((name, payload))
        return "task-1"


@pytest.mark.asyncio
async def test_send_message_commits_user_and_pending_assistant_before_queueing():
    repo, queue = Repo(), Queue()
    result = await SendMessage(lambda: Uow(repo), queue).execute(branch_id="b", content="hello", client_request_id="req-1")
    assert [call[1] for call in repo.calls] == ["user", "assistant"]
    assert repo.calls[0][3:] == ("req-1", "COMPLETED")
    assert repo.calls[1][3:] == (None, "PENDING")
    assert result[2] == "task-1"


class FailingQueue:
    async def enqueue(self, name, payload):
        raise RuntimeError("queue down")


class StatusRepo(Repo):
    def __init__(self):
        super().__init__()
        self.statuses = []

    async def set_message_status(self, message_id, status):
        self.statuses.append((message_id, status))

    async def conversation_id_for_message(self, message_id):
        return "conv-1"


class EventStream:
    def __init__(self):
        self.published = []

    async def publish(self, topic, payload):
        self.published.append((topic, payload))


@pytest.mark.asyncio
async def test_enqueue_failure_marks_assistant_failed_and_publishes_event():
    # enqueue 抛错时 user+assistant 已落库：占位消息必须翻 FAILED（不留无任务
    # 无事件的 PENDING 孤儿），并广播 message.failed 让 SSE live tail 收尾。
    repo, events = StatusRepo(), EventStream()
    with pytest.raises(RuntimeError, match="queue down"):
        await SendMessage(lambda: Uow(repo), FailingQueue(), events).execute(branch_id="b", content="hello", client_request_id="req-1")
    assert repo.statuses == [("2", "FAILED")]  # "2" = assistant 占位消息
    failed = [payload for _, payload in events.published if payload["event"] == "message.failed"]
    assert failed  # message + conversation 双 topic 各一条（既有发布模式）
    assert {payload["message_id"] for payload in failed} == {"2"}
    assert {payload["reason"] for payload in failed} == {"queue-unavailable"}
    assert {topic for topic, _ in events.published} == {"message:2", "conversation:conv-1"}


@pytest.mark.asyncio
async def test_enqueue_failure_marks_assistant_failed_without_event_stream():
    # 未接 event_stream 的调用方：状态兜底仍然生效，只是不发事件。
    repo = StatusRepo()
    with pytest.raises(RuntimeError, match="queue down"):
        await SendMessage(lambda: Uow(repo), FailingQueue()).execute(branch_id="b", content="hello", client_request_id="req-1")
    assert repo.statuses == [("2", "FAILED")]

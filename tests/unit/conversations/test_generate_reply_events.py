from __future__ import annotations

import hashlib

import pytest

import proseforge.application.conversations.generate_reply as generate_reply_module
from proseforge.application.conversations.generate_reply import (
    GenerateReply,
    _CancelProbe,
)
from proseforge.domain.conversation.entity import Message
from proseforge.domain.ports.model_provider import GenerationEvent


class Repo:
    def __init__(self):
        self.statuses = []
        self.chunks = []
        self.hashes = []
        self.content = ""

    async def set_message_status(self, message_id, status):
        self.statuses.append(status)

    async def append_chunk(self, message_id, index, event_type, text):
        self.chunks.append((message_id, index, event_type, text))
        self.content += text

    async def chunk_count(self, message_id):
        return len(self.chunks)

    async def conversation_id_for_message(self, message_id):
        return "conv-1"

    async def get_message(self, message_id):
        return Message(id=message_id, branch_id="b1", role="assistant", content=self.content)

    async def set_content_hash(self, message_id, content_hash):
        self.hashes.append(content_hash)


class Uow:
    def __init__(self, repo):
        self.conversations = repo

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def commit(self):
        return None


class EventStream:
    def __init__(self):
        self.events = []

    async def publish(self, topic, payload):
        self.events.append((topic, payload))


class TwoChunkProvider:
    async def stream(self, request):
        yield GenerationEvent("content.delta", "one")
        yield GenerationEvent("content.delta", "two")


class UsageRepo:
    def __init__(self):
        self.records = []

    async def record(self, **kwargs):
        self.records.append(kwargs)


class UsageUow(Uow):
    def __init__(self, repo, usage):
        super().__init__(repo)
        self.usage = usage


class FailingProvider:
    async def stream(self, request):
        yield GenerationEvent("content.delta", "one")
        raise RuntimeError("network interruption")


@pytest.mark.asyncio
async def test_started_and_completed_events_wrap_the_stream():
    repo, events = Repo(), EventStream()
    await GenerateReply(lambda: Uow(repo), TwoChunkProvider(), events).execute(message_id="m1", request=object())
    names = [payload["event"] for _, payload in events.events]
    assert names[0] == "message.started"
    assert names[-1] == "message.completed"
    # 两个 delta 各自发到 message 与 conversation 两个 topic（既有行为不回归）
    delta_topics = [topic for topic, payload in events.events if payload["event"] == "content.delta"]
    assert delta_topics == ["message:m1", "conversation:conv-1", "message:m1", "conversation:conv-1"]
    # started must be published before the first delta
    assert names.index("message.started") < names.index("content.delta")
    # both message and conversation topics receive the lifecycle events
    started_topics = {topic for topic, payload in events.events if payload["event"] == "message.started"}
    assert started_topics == {"message:m1", "conversation:conv-1"}
    completed = next(payload for _, payload in events.events if payload["event"] == "message.completed")
    assert completed["content_hash"] == hashlib.sha256(b"onetwo").hexdigest()
    assert repo.hashes == [hashlib.sha256(b"onetwo").hexdigest()]


@pytest.mark.asyncio
async def test_failed_event_is_published_after_terminal_status():
    repo, events = Repo(), EventStream()
    with pytest.raises(RuntimeError):
        await GenerateReply(lambda: Uow(repo), FailingProvider(), events).execute(message_id="m1", request=object())
    names = [payload["event"] for _, payload in events.events]
    assert names[0] == "message.started"
    assert names[-1] == "message.failed"
    failed = next(payload for _, payload in events.events if payload["event"] == "message.failed")
    assert failed["status"] == "PARTIAL"
    assert repo.statuses[-1] == "PARTIAL"


@pytest.mark.asyncio
async def test_completion_without_provider_usage_records_missing_source():
    repo, events, usage = Repo(), EventStream(), UsageRepo()
    await GenerateReply(lambda: UsageUow(repo, usage), TwoChunkProvider(), events).execute(message_id="m1", request=object())
    # provider 全程未回 usage → 落一条 source="missing" 的记录，绝不假装是 provider 值
    assert len(usage.records) == 1
    delta = usage.records[0]["delta"]
    assert delta.source == "missing"
    assert delta.final is True
    usage_events = [payload for _, payload in events.events if payload["event"] == "usage.updated"]
    assert [payload["source"] for payload in usage_events] == ["missing", "missing"]  # message + conversation 双 topic（既有发布模式）


@pytest.mark.asyncio
async def test_provider_usage_is_not_overwritten_with_missing():
    class UsageProvider:
        async def stream(self, request):
            yield GenerationEvent("content.delta", "one")
            yield GenerationEvent("usage.updated", data={"usage": {"input_tokens": 3, "output_tokens": 4}})

    repo, events, usage = Repo(), EventStream(), UsageRepo()
    await GenerateReply(lambda: UsageUow(repo, usage), UsageProvider(), events).execute(message_id="m1", request=object())
    assert [record["delta"].source for record in usage.records] == ["provider"]


class FailedEventProvider:
    async def stream(self, request):
        yield GenerationEvent("content.delta", "one")
        yield GenerationEvent("response.failed", data={"response": {"error": {"message": "model overloaded"}}})


class NeutralEventProvider:
    async def stream(self, request):
        yield GenerationEvent("response.in_progress", data={"response": {"id": "r1"}})
        yield GenerationEvent("response.output_item.added", data={"item": {}})
        yield GenerationEvent("content.delta", "ok")
        yield GenerationEvent("response.completed", data={"id": "r1"})


@pytest.mark.asyncio
async def test_provider_failed_event_takes_error_path():
    repo, events = Repo(), EventStream()
    with pytest.raises(RuntimeError, match="model overloaded"):
        await GenerateReply(lambda: Uow(repo), FailedEventProvider(), events).execute(message_id="m1", request=object())
    # 已有一个 chunk → PARTIAL（而非 FAILED），且广播 message.failed，绝不静默 COMPLETED
    assert repo.statuses[-1] == "PARTIAL"
    names = [payload["event"] for _, payload in events.events]
    assert names[-1] == "message.failed"
    assert "message.completed" not in names


@pytest.mark.asyncio
async def test_neutral_stream_events_are_ignored_not_failed():
    repo, events = Repo(), EventStream()
    await GenerateReply(lambda: Uow(repo), NeutralEventProvider(), events).execute(message_id="m1", request=object())
    assert repo.statuses[-1] == "COMPLETED"
    assert repo.chunks == [("m1", 0, "content.delta", "ok")]


class CancellableRepo(Repo):
    """Repo whose message_status replays a scripted status sequence."""

    def __init__(self, statuses_by_call: list[str]):
        super().__init__()
        self._statuses_by_call = list(statuses_by_call)
        self.status_checks = 0

    async def message_status(self, message_id):
        self.status_checks += 1
        if self._statuses_by_call:
            return self._statuses_by_call.pop(0)
        return "STREAMING"


class ThreeChunkProvider:
    async def stream(self, request):
        yield GenerationEvent("content.delta", "one")
        yield GenerationEvent("content.delta", "two")
        yield GenerationEvent("content.delta", "three")


@pytest.mark.asyncio
async def test_cancelled_before_start_publishes_nothing_and_keeps_status():
    # 用户在 worker 起流前 stop：不得把 CANCELLED 翻回 STREAMING，也不得发布
    # message.started（否则前端气泡永久卡「生成中」）。
    repo, events = CancellableRepo(["CANCELLED"]), EventStream()
    result = await GenerateReply(lambda: Uow(repo), ThreeChunkProvider(), events).execute(message_id="m1", request=object())
    assert result == 0
    assert repo.statuses == []  # set_message_status("STREAMING") 从未执行
    assert repo.chunks == []
    assert events.events == []


@pytest.mark.asyncio
async def test_stream_stops_publishing_late_frames_after_cancel(monkeypatch: pytest.MonkeyPatch):
    # 流式途中被取消：探针发现 CANCELLED 后立即退出循环，迟到帧不再发布。
    repo, events = CancellableRepo(["STREAMING", "STREAMING", "CANCELLED"]), EventStream()

    class _EagerProbe:
        # 逐帧查询（不节流），聚焦验证接线；节流语义由下方探针单测覆盖。
        def __init__(self, *args, **kwargs):
            pass

        async def cancelled(self):
            return await repo.message_status("m1") == "CANCELLED"

    monkeypatch.setattr(generate_reply_module, "_CancelProbe", _EagerProbe)
    result = await GenerateReply(lambda: Uow(repo), ThreeChunkProvider(), events).execute(message_id="m1", request=object())
    assert result == 1  # 只有第一帧通过
    names = [payload["event"] for _, payload in events.events]
    assert names[0] == "message.started"
    assert names.count("content.delta") == 2  # 一帧 × message/conversation 双 topic
    assert "message.completed" not in names


@pytest.mark.asyncio
async def test_cancel_probe_throttles_status_queries():
    # 节流：interval 窗口内重复查询走缓存，不每帧一次 DB 往返。
    repo = CancellableRepo(["STREAMING"])
    probe = _CancelProbe(lambda: Uow(repo), "m1", interval=60.0)
    assert await probe.cancelled() is False
    assert await probe.cancelled() is False
    assert repo.status_checks == 1


@pytest.mark.asyncio
async def test_cancel_probe_latches_cancelled_without_requery():
    repo = CancellableRepo(["CANCELLED"])
    probe = _CancelProbe(lambda: Uow(repo), "m1", interval=60.0)
    assert await probe.cancelled() is True
    assert await probe.cancelled() is True
    assert repo.status_checks == 1  # 已取消状态锁存，不再查库

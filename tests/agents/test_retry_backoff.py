"""可重试供应商错误退避 + 连败自动暂停 + sweeper 豁免 测试。

覆盖 proseforge/workflows/agent_executor.py 的 2026-08 新行为：
- 可重试供应商错误（5xx/429/超时，经 classify_provider_error 判定
  RetryableProviderError）独立计数 retryable_attempts、指数退避
  next_attempt_at 重排，不吃 attempts 的 DEFAULT_MAX_ATTEMPTS 额度；
- 退避等待中的任务不被主循环认领，全部在等时睡到最近到期（测试用
  fast-forward 假 sleep 把时间快进，绝不真等 30s）；
- run 级连败 >= AUTO_PAUSE_STREAK 自动 PAUSED + run.auto_paused 事件 +
  总调度聊天提醒（不刷屏）；连败被 task.succeeded 打断；
- 不可重试错误（JSONDecodeError）维持原 attempts 语义；
- sweeper 豁免「有 PENDING 任务 next_attempt_at>now」的滞留 RUNNING run。

fixture/种子模式沿用 tests/agents/test_agent_executor.py 与
tests/agents/test_agent_message_sweeper.py（sqlite+aiosqlite 真实落库）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, update

from proseforge.application.agents.role_handlers import ROLE_HANDLERS, RoleResult
from proseforge.application.messages.sweeper import sweep_stale_run_messages
from proseforge.domain.common.ids import new_id
from proseforge.domain.ports.model_provider import GenerationEvent
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.agents import (
    AgentEventModel,
    AgentRunModel,
    AgentTaskModel,
)
from proseforge.infrastructure.database.models.conversation import (
    ConversationBranchModel,
    ConversationModel,
    MessageModel,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.security.credential_cipher import CredentialCipher
from proseforge.settings import Settings, get_settings
from proseforge.workflows.agent_executor import (
    AUTO_PAUSE_STREAK,
    DEFAULT_MAX_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    _retry_backoff_seconds,
    _retry_waiting,
    execute_run,
)

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture()
def executor_settings(tmp_path, monkeypatch):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'retry.db').as_posix()}"
    monkeypatch.setenv("PROSEFORGE_DATABASE_URL", database_url)
    monkeypatch.setenv("PROSEFORGE_RUNTIME_PROFILE", "native")
    monkeypatch.setenv("PROSEFORGE_MASTER_KEY", MASTER_KEY)
    get_settings.cache_clear()
    yield Settings(
        database_url=database_url,
        runtime_profile="native",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
    )
    get_settings.cache_clear()


class FakeProvider:
    """按 task_key 定制输出的假 provider（malformed 输出触发 JSONDecodeError）。"""

    provider_id = "fake"

    def __init__(self, payloads: dict[str, object] | None = None, usage: tuple[int, int] = (4, 2)):
        self._payloads = payloads or {}
        self._input, self._output = usage

    async def stream(self, request):
        payload = self._payloads.get(request.metadata.get("task_key", ""), {"summary": "ok"})
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        yield GenerationEvent("response.started")
        yield GenerationEvent("content.delta", text=text)
        yield GenerationEvent("response.completed", data={"usage": {"input_tokens": self._input, "output_tokens": self._output, "total_tokens": self._input + self._output}})

    async def list_models(self):
        return []

    async def validate_credentials(self):
        return {"valid": True}

    async def count_tokens(self, request):
        return 1


def _patch_provider(monkeypatch, provider: FakeProvider) -> None:
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: provider)


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=httpx.Response(status, request=request))


async def _seed_run(
    settings: Settings,
    tasks: list[dict[str, object]],
    *,
    budget_limit: int = 1000,
    events: list[dict[str, object]] | None = None,
) -> dict[str, str]:
    """一个 run + 指定任务；events 可预置 agent_events（连败计数测试用），
    run.event_cursor 同步为预置事件数。"""
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"retry-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            credential_id = f"cred-{uuid.uuid4().hex[:8]}"
            associated = f"{user.id}:openai:{credential_id}".encode()
            encrypted = CredentialCipher(base64.b64decode(MASTER_KEY)).encrypt(json.dumps({"api_key": "sk-test"}).encode(), associated_data=associated)
            await uow.credentials.create(user.id, "openai", base64.b64encode(encrypted).decode(), record_id=credential_id)
            now = datetime.now(UTC)
            uow.session.add(ProjectModel(id="project-1", owner_id=user.id, slug="project-1", title="Retry Test Project"))
            await uow.session.flush()
            run = AgentRunModel(
                id=new_id(), user_id=user.id, project_id="project-1", goal_hash="g" * 64,
                graph_revision=1, status="PENDING", budget_limit=budget_limit,
                event_cursor=len(events or []),
                created_at=now, updated_at=now,
            )
            uow.session.add(run)
            await uow.session.flush()
            for item in tasks:
                uow.session.add(AgentTaskModel(
                    id=new_id(), run_id=run.id, task_key=str(item["id"]), role=str(item["role"]),
                    status="PENDING", token_budget=int(item.get("token_budget", 1)),
                    depends_on=json.dumps(item.get("depends_on", [])),
                ))
            for sequence, event in enumerate(events or [], start=1):
                uow.session.add(AgentEventModel(
                    id=new_id(), run_id=run.id, sequence=sequence,
                    event_type=str(event["event_type"]),
                    payload=json.dumps(event.get("payload", {}), sort_keys=True),
                ))
            await uow.commit()
            return {"run_id": run.id, "user_id": user.id}
    finally:
        await engine.dispose()


async def _seed_swarm_message(settings: Settings, run_id: str, project_id: str) -> str:
    """Conversation + branch + 关联 run 的占位 assistant 消息。"""
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            uow.session.add(ConversationModel(id="conv-1", project_id=project_id, title="swarm"))
            await uow.session.flush()
            uow.session.add(ConversationBranchModel(id="branch-1", conversation_id="conv-1", name="Main"))
            await uow.session.flush()
            uow.session.add(MessageModel(id="msg-1", branch_id="branch-1", role="assistant", content="", sequence_no=1, status="PENDING", agent_run_id=run_id))
            await uow.commit()
            return "msg-1"
    finally:
        await engine.dispose()


async def _read_state(settings: Settings, run_id: str):
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            run_snapshot = {key: getattr(run, key) for key in ("id", "status", "terminal_reason", "event_cursor")}
            tasks = [
                {key: getattr(task, key) for key in ("id", "task_key", "role", "status", "attempts", "retryable_attempts", "next_attempt_at", "last_error")}
                for task in await uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run_id).order_by(AgentTaskModel.id))
            ]
            events = [
                {key: getattr(event, key) for key in ("sequence", "event_type", "payload")}
                for event in await uow.session.scalars(select(AgentEventModel).where(AgentEventModel.run_id == run_id).order_by(AgentEventModel.sequence))
            ]
            return run_snapshot, tasks, events
    finally:
        await engine.dispose()


async def _read_message(settings: Settings, message_id: str) -> dict[str, str]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            message = await uow.session.get(MessageModel, message_id)
            return {"status": message.status, "content": message.content}
    finally:
        await engine.dispose()


def _patch_fast_forward_sleep(monkeypatch, settings: Settings, run_id: str) -> dict[str, int]:
    """把 asyncio.sleep 换成「时间快进」：每睡一次就把本 run 所有
    next_attempt_at 拉到过去（模拟退避到期），返回 fast_forwards 计数。

    主循环退避等待分支睡到最近到期时间；快进后下一轮迭代任务到期被
    重新认领——全程不真 sleep。renewer 的 sleep 在任务 RUNNING 期间
    （next_attempt_at 为 NULL）不会命中快进，可据此区分。"""
    counters = {"fast_forwards": 0}
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        engine, factory = create_engine_and_sessionmaker(settings)
        try:
            async with SqlAlchemyUnitOfWork(factory) as uow:
                result = await uow.session.execute(
                    update(AgentTaskModel)
                    .where(AgentTaskModel.run_id == run_id, AgentTaskModel.next_attempt_at.isnot(None))
                    .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
                )
                await uow.commit()
                if result.rowcount:
                    counters["fast_forwards"] += 1
        finally:
            await engine.dispose()
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return counters


def _flaky_handler(error_factory, *, fail_times: int, fail_key: str = "planner"):
    """前 fail_times 次调用抛 error_factory()，之后返回合法 report。"""
    calls = {"count": 0}

    async def handler(context):
        if str(context["task"]["task_key"]) == fail_key:
            calls["count"] += 1
            if calls["count"] <= fail_times:
                raise error_factory()
        return RoleResult(artifact_type="report", payload={"summary": "ok"}, used_tokens=5, input_tokens=3, output_tokens=2)

    return handler, calls


def _seconds_until(naive_dt: datetime) -> float:
    """SQLite 读回 naive datetime：按 UTC 解释后求与现在的差。"""
    return (naive_dt.replace(tzinfo=UTC) - datetime.now(UTC)).total_seconds()


# ---------------------------------------------------------------------------
# 1) 退避档位：_retry_backoff_seconds 分布与抖动边界
# ---------------------------------------------------------------------------


def test_retry_backoff_seconds_schedule_and_jitter(monkeypatch):
    """第 1/2/3/4+ 次可重试失败的基准延迟为 30/120/300/300s，±20% 抖动。"""
    expected_base = {1: 30, 2: 120, 3: 300, 4: 300, 6: 300}
    for attempts, base in expected_base.items():
        samples = [_retry_backoff_seconds(attempts) for _ in range(300)]
        assert min(samples) >= base * 0.8, f"attempts={attempts} 抖动下界破防"
        assert max(samples) <= base * 1.2, f"attempts={attempts} 抖动上界破防"
    # 固定抖动因子后取精确基准值（排程表之外停留在 300s）
    monkeypatch.setattr("proseforge.workflows.agent_executor.random.uniform", lambda low, high: 1.0)
    assert [_retry_backoff_seconds(n) for n in (1, 2, 3, 4, 5, 10)] == [30, 120, 300, 300, 300, 300]
    assert RETRY_BACKOFF_SECONDS == (30, 120, 300)


# ---------------------------------------------------------------------------
# 2) _retry_waiting 单测：退避等待判定（含 SQLite naive datetime 规则）
# ---------------------------------------------------------------------------


def test_retry_waiting_unit():
    now = datetime.now(UTC)
    assert _retry_waiting(SimpleNamespace(next_attempt_at=None), now) is False
    assert _retry_waiting(SimpleNamespace(next_attempt_at=now + timedelta(seconds=30)), now) is True
    assert _retry_waiting(SimpleNamespace(next_attempt_at=now - timedelta(seconds=1)), now) is False
    assert _retry_waiting(SimpleNamespace(next_attempt_at=now), now) is False  # 到期即不认领等待
    # SQLite 读回 naive datetime：按 now 的时区（UTC）解释
    naive_future = (now + timedelta(seconds=30)).replace(tzinfo=None)
    naive_past = (now - timedelta(seconds=1)).replace(tzinfo=None)
    assert _retry_waiting(SimpleNamespace(next_attempt_at=naive_future), now) is True
    assert _retry_waiting(SimpleNamespace(next_attempt_at=naive_past), now) is False


# ---------------------------------------------------------------------------
# 3) 可重试错误集成：退避重排 + 等待期间不认领 + 到期恢复（不假死 FAILED）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_factory",
    [
        pytest.param(lambda: _http_status_error(503), id="http-503"),
        pytest.param(lambda: _http_status_error(429), id="http-429"),
        pytest.param(lambda: httpx.ReadTimeout("read timed out"), id="read-timeout"),
        pytest.param(lambda: httpx.ConnectError("connection refused"), id="connect-error"),
    ],
)
async def test_retryable_error_backoff_then_recovers(executor_settings, monkeypatch, error_factory):
    """可重试供应商错误（503/429/超时/连接失败）：任务回 PENDING、
    next_attempt_at 排在未来、retryable_attempts 独立计数、attempts 不吃额度；
    等待期间主循环不认领（走 fast-forward 快进），到期重试成功后 run COMPLETED。"""
    handler, calls = _flaky_handler(error_factory, fail_times=1)
    monkeypatch.setitem(ROLE_HANDLERS, "chief_planner", handler)
    provider = FakeProvider(usage=(1, 1))
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [
        {"id": "planner", "role": "chief_planner", "token_budget": 1},
        {"id": "follow", "role": "chief_planner", "depends_on": ["planner"], "token_budget": 1},
    ])
    counters = _patch_fast_forward_sleep(monkeypatch, executor_settings, seeded["run_id"])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, tasks, events = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "COMPLETED"  # 绝不因退避等待误判死局 FAILED
    assert run["terminal_reason"] != "task dependency could not be satisfied"
    planner = next(task for task in tasks if task["task_key"] == "planner")
    assert planner["status"] == "SUCCEEDED"
    # 失败 1 次（可重试）+ 到期重试成功：attempts=2、retryable_attempts=1，
    # 有效不可重试次数 = 2-1 = 1，没动 DEFAULT_MAX_ATTEMPTS 额度
    assert planner["attempts"] == 2
    assert planner["retryable_attempts"] == 1
    assert planner["next_attempt_at"] is None  # 重新认领时清退避标记
    # 失败只发生一次：handler 对 planner 恰好调 2 次（失败时没有立即重认领，
    # 是退避到期后才第二次调用）
    assert calls["count"] == 2
    assert counters["fast_forwards"] >= 1  # 主循环确实进了退避等待分支
    # task.failed 事件契约：retryable_provider + delay_seconds 在 30s±20% 区间
    failed = [json.loads(event["payload"]) for event in events if event["event_type"] == "task.failed"]
    assert len(failed) == 1
    assert failed[0]["task_key"] == "planner"
    assert failed[0]["retry"] is True
    assert failed[0]["retryable_provider"] is True
    assert 24 <= failed[0]["delay_seconds"] <= 36
    # 下游任务在 planner 成功后才启动（退避期间依赖不就绪、不误判死局）
    assert all(task["status"] == "SUCCEEDED" for task in tasks)


@pytest.mark.asyncio
async def test_retryable_failure_does_not_consume_attempts_budget(executor_settings, monkeypatch):
    """连续 2 次可重试失败后再成功：attempts=3 但 retryable_attempts=2，
    有效不可重试次数仍为 1——可重试失败不回退也不消耗 attempts 额度。"""
    handler, calls = _flaky_handler(lambda: _http_status_error(503), fail_times=2)
    monkeypatch.setitem(ROLE_HANDLERS, "chief_planner", handler)
    _patch_provider(monkeypatch, FakeProvider(usage=(1, 1)))
    seeded = await _seed_run(executor_settings, [{"id": "planner", "role": "chief_planner", "token_budget": 1}])
    counters = _patch_fast_forward_sleep(monkeypatch, executor_settings, seeded["run_id"])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, tasks, events = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "COMPLETED"
    task = tasks[0]
    assert task["attempts"] == 3
    assert task["retryable_attempts"] == 2
    assert calls["count"] == 3
    assert counters["fast_forwards"] >= 2  # 两轮退避等待都快进过
    delays = [json.loads(event["payload"])["delay_seconds"] for event in events if event["event_type"] == "task.failed"]
    assert len(delays) == 2
    assert 24 <= delays[0] <= 36  # 第 1 次：30s 档
    assert 96 <= delays[1] <= 144  # 第 2 次：120s 档


# ---------------------------------------------------------------------------
# 4) 连败 3 次 → run PAUSED + run.auto_paused + 总调度提醒（不刷屏）
# ---------------------------------------------------------------------------


def _retryable_failed_payload() -> dict[str, object]:
    return {"task_id": "seeded", "task_key": "planner", "error": "HTTPStatusError", "retry": True, "retryable_provider": True, "delay_seconds": 30.0}


@pytest.mark.asyncio
async def test_auto_pause_after_retryable_failure_streak(executor_settings, monkeypatch):
    """run 级连败（预置 2 条 retryable task.failed + 本次失败 = AUTO_PAUSE_STREAK）
    → run PAUSED + run.auto_paused 事件 + execute_run 返回 "paused"。
    （聊天提醒断言在 test_auto_pause_notification_appends_chat_notice。）"""
    handler, _calls = _flaky_handler(lambda: _http_status_error(503), fail_times=99)
    monkeypatch.setitem(ROLE_HANDLERS, "chief_planner", handler)
    _patch_provider(monkeypatch, FakeProvider(usage=(1, 1)))
    seeded = await _seed_run(
        executor_settings,
        [{"id": "planner", "role": "chief_planner", "token_budget": 1}],
        events=[
            {"event_type": "task.failed", "payload": _retryable_failed_payload()},
            {"event_type": "task.failed", "payload": _retryable_failed_payload()},
        ],
    )
    await _seed_swarm_message(executor_settings, seeded["run_id"], "project-1")

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "paused"
    run, tasks, events = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "PAUSED"
    task = tasks[0]
    # 本次可重试失败照常落退避：PENDING + 独立计数 + 未来到期时间（30s±20%）
    assert task["status"] == "PENDING"
    assert task["attempts"] == 1
    assert task["retryable_attempts"] == 1
    assert task["next_attempt_at"] is not None
    assert 20 <= _seconds_until(task["next_attempt_at"]) <= 36
    # run.auto_paused 事件带连败数；第 3 条 task.failed 带 retryable_provider
    auto_paused = [json.loads(event["payload"]) for event in events if event["event_type"] == "run.auto_paused"]
    assert len(auto_paused) == 1
    assert auto_paused[0]["streak"] == AUTO_PAUSE_STREAK == 3
    failed = [json.loads(event["payload"]) for event in events if event["event_type"] == "task.failed" and event["sequence"] > 2]
    assert len(failed) == 1 and failed[0]["retryable_provider"] is True


@pytest.mark.asyncio
async def test_auto_pause_notification_appends_chat_notice(executor_settings, monkeypatch):
    """连败自动暂停的总调度提醒：占位消息恰好追加一次（AUTO_PAUSE_MAX_NOTICES
    内，不刷屏）。"""
    handler, _calls = _flaky_handler(lambda: _http_status_error(503), fail_times=99)
    monkeypatch.setitem(ROLE_HANDLERS, "chief_planner", handler)
    _patch_provider(monkeypatch, FakeProvider(usage=(1, 1)))
    seeded = await _seed_run(
        executor_settings,
        [{"id": "planner", "role": "chief_planner", "token_budget": 1}],
        events=[
            {"event_type": "task.failed", "payload": _retryable_failed_payload()},
            {"event_type": "task.failed", "payload": _retryable_failed_payload()},
        ],
    )
    message_id = await _seed_swarm_message(executor_settings, seeded["run_id"], "project-1")

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "paused"
    message = await _read_message(executor_settings, message_id)
    assert message["content"].count("总调度：模型访问不稳定") == 1
    assert "已自动暂停本章写作" in message["content"]


@pytest.mark.asyncio
async def test_retryable_failure_streak_broken_by_task_succeeded(executor_settings, monkeypatch):
    """连败计数被 task.succeeded 打断：预置 failed/failed/succeeded 事件序列后，
    本次失败从尾部数 streak=1（<3）→ 不自动暂停；退避到期重试成功，run COMPLETED。"""
    handler, _calls = _flaky_handler(lambda: _http_status_error(503), fail_times=1)
    monkeypatch.setitem(ROLE_HANDLERS, "chief_planner", handler)
    _patch_provider(monkeypatch, FakeProvider(usage=(1, 1)))
    seeded = await _seed_run(
        executor_settings,
        [{"id": "planner", "role": "chief_planner", "token_budget": 1}],
        events=[
            {"event_type": "task.failed", "payload": _retryable_failed_payload()},
            {"event_type": "task.failed", "payload": _retryable_failed_payload()},
            {"event_type": "task.succeeded", "payload": {"task_id": "seeded", "task_key": "other"}},
        ],
    )
    message_id = await _seed_swarm_message(executor_settings, seeded["run_id"], "project-1")
    counters = _patch_fast_forward_sleep(monkeypatch, executor_settings, seeded["run_id"])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, tasks, events = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "COMPLETED"
    assert tasks[0]["status"] == "SUCCEEDED"
    assert not any(event["event_type"] == "run.auto_paused" for event in events)
    assert counters["fast_forwards"] >= 1  # streak=1 不暂停：走退避等待后恢复
    message = await _read_message(executor_settings, message_id)
    assert "总调度：模型访问不稳定" not in message["content"]


# ---------------------------------------------------------------------------
# 5) 不可重试错误维持原语义：attempts 额度 3 次耗尽 → FAILED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_retryable_error_keeps_attempts_semantics(executor_settings, monkeypatch):
    """JSONDecodeError 不是 RetryableProviderError：走 attempts 的
    DEFAULT_MAX_ATTEMPTS 额度、3 次后 FAILED，全程无 next_attempt_at、
    retryable_attempts 恒 0、事件不带 retryable_provider。"""
    provider = FakeProvider(payloads={"fragile": "<<not json>>"}, usage=(1, 1))
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [{"id": "fragile", "role": "chief_planner", "token_budget": 1}])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "failed"
    run, tasks, events = await _read_state(executor_settings, seeded["run_id"])
    task = tasks[0]
    assert task["status"] == "FAILED"
    assert task["attempts"] == DEFAULT_MAX_ATTEMPTS == 3
    assert task["retryable_attempts"] == 0
    assert task["next_attempt_at"] is None
    assert "JSONDecodeError" in (task["last_error"] or "")
    assert run["status"] == "FAILED"
    assert run["terminal_reason"] == "task(s) failed without retry"
    failed = [json.loads(event["payload"]) for event in events if event["event_type"] == "task.failed"]
    assert [item["retry"] for item in failed] == [True, True, False]
    assert all("retryable_provider" not in item for item in failed)
    assert not any(event["event_type"] == "run.auto_paused" for event in events)


# ---------------------------------------------------------------------------
# 6) sweeper 豁免：退避等待中的滞留 RUNNING run 不被清扫
# ---------------------------------------------------------------------------


async def _seed_sweeper_run(settings: Settings, *, next_attempt_at: datetime | None) -> dict[str, str]:
    """RUNNING + updated_at 过期 + 无活 lease 的 run，带一个 PENDING 任务
    （next_attempt_at 由用例控制）+ 关联占位消息。"""
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"sweep-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            stale = datetime.now(UTC) - timedelta(hours=1)
            uow.session.add(ProjectModel(id="project-1", owner_id=user.id, slug="project-1", title="Sweeper Retry Project"))
            await uow.session.flush()
            run = AgentRunModel(
                id=new_id(), user_id=user.id, project_id="project-1", goal_hash="g" * 64,
                graph_revision=1, status="RUNNING", budget_limit=1000,
                created_at=stale, updated_at=stale,
            )
            uow.session.add(run)
            await uow.session.flush()
            uow.session.add(AgentTaskModel(
                id=new_id(), run_id=run.id, task_key="planner", role="chief_planner",
                status="PENDING", token_budget=1, depends_on="[]",
                retryable_attempts=1, next_attempt_at=next_attempt_at,
            ))
            uow.session.add(ConversationModel(id="conv-1", project_id="project-1", title="swarm"))
            await uow.session.flush()
            uow.session.add(ConversationBranchModel(id="branch-1", conversation_id="conv-1", name="Main"))
            await uow.session.flush()
            uow.session.add(MessageModel(id="msg-1", branch_id="branch-1", role="assistant", content="", sequence_no=1, status="PENDING", agent_run_id=run.id))
            await uow.commit()
            return {"run_id": run.id, "message_id": "msg-1"}
    finally:
        await engine.dispose()


async def _sweep(settings: Settings) -> int:
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        return await sweep_stale_run_messages(session_factory, settings)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sweeper_exempts_run_waiting_on_retry_backoff(executor_settings):
    """RUNNING + updated_at 过期 + 无活 lease，但 PENDING 任务
    next_attempt_at 在未来（退避等待中）→ sweeper 豁免，run 保持 RUNNING。"""
    seeded = await _seed_sweeper_run(executor_settings, next_attempt_at=datetime.now(UTC) + timedelta(seconds=300))

    repaired = await _sweep(executor_settings)

    assert repaired == 0
    run, tasks, events = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "RUNNING"  # 退避等待不是滞留，绝不能标 FAILED
    assert tasks[0]["status"] == "PENDING"
    assert not any(event["event_type"] == "run.failed" for event in events)
    message = await _read_message(executor_settings, seeded["message_id"])
    assert message["status"] == "PENDING"


@pytest.mark.asyncio
async def test_sweeper_sweeps_run_with_expired_backoff(executor_settings):
    """对照：next_attempt_at 已过期（退避早该执行、executor 仍死了）→
    不豁免，run 被正常清扫 FAILED 并重放 writeback。"""
    seeded = await _seed_sweeper_run(executor_settings, next_attempt_at=datetime.now(UTC) - timedelta(seconds=60))

    repaired = await _sweep(executor_settings)

    assert repaired == 1
    run, _tasks, events = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "FAILED"
    assert run["terminal_reason"] == "executor lost; marked failed by message sweeper"
    assert any(event["event_type"] == "run.failed" for event in events)
    message = await _read_message(executor_settings, seeded["message_id"])
    assert message["status"] == "FAILED"

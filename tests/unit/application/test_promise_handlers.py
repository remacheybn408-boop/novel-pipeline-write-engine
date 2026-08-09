"""promise_keeper（奥莉维亚·动态承诺）handler 单元测试。

sqlite+aiosqlite 真实落库 + FakeProvider 假模型（无网络、无 PG），fixture 模式
沿用 tests/agents/test_chief_handler.py。覆盖：契约卡组装（台账 + goal 钩子行
进 prompt）、verify 打勾（含 required_fulfillments 多次兑现计数与状态机）、
register 语义去重（duplicate_of 并入 + 新行 confidence/source 契约）、artifact
缺失/模型失败降级（不注入、不抛错）。
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from proseforge.application.agents import promise_handlers
from proseforge.application.agents.promise_handlers import (
    promise_keeper_handler,
    render_promise_contract,
)
from proseforge.domain.common.ids import new_id
from proseforge.domain.ports.model_provider import GenerationEvent
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.agents import (
    AgentArtifactModel,
    AgentRunModel,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.retrieval import (
    RetrievalChunkModel,
    RetrievalDocumentModel,
)
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.settings import Settings, get_settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()
GOAL = "写第3章《回城》\n伏笔/钩子：埋入：玉佩裂痕；回收：师父的遗物\n目标字数：不少于 2500 字"


@pytest.fixture()
def handler_settings(tmp_path, monkeypatch):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'promise.db').as_posix()}"
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
    """按 task_key 定制输出的假 provider（复制自 tests/agents 同款）。"""

    provider_id = "fake"

    def __init__(self, payloads: dict[str, object] | None = None, *, fail: bool = False):
        self._payloads = payloads or {}
        self._fail = fail
        self.requests: list[dict[str, str]] = []
        self.user_prompts: list[str] = []

    async def stream(self, request):
        self.requests.append(dict(request.metadata))
        self.user_prompts.append(str(request.input_blocks[0]["text"]))
        if self._fail:
            raise RuntimeError("model down")
        payload = self._payloads.get(request.metadata.get("task_key", ""), {"summary": "ok"})
        if isinstance(payload, list):
            # 列表形态：同一 task_key 逐次弹出（多次调用不同输出）。
            payload = payload.pop(0) if len(payload) > 1 else payload[0]
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        yield GenerationEvent("response.started")
        yield GenerationEvent("content.delta", text=text)
        yield GenerationEvent("response.completed", data={"usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}})

    async def list_models(self):
        return []

    async def validate_credentials(self):
        return {"valid": True}

    async def count_tokens(self, request):
        return 1


def _promise_row(project_id: str, key: str, value: dict[str, object], *, status: str = "open", version: int = 1) -> StoryBibleEntryModel:
    now = datetime.now(UTC)
    return StoryBibleEntryModel(
        id=new_id(), project_id=project_id, kind="promise", key=key,
        value_json=json.dumps(value, ensure_ascii=False), status=status,
        confidence=1.0, source="auto", pinned=False, version=version,
        created_at=now, updated_at=now,
    )


async def _seed(
    settings: Settings,
    *,
    promises: list[StoryBibleEntryModel] | None = None,
    artifacts: list[tuple[str, str, dict[str, object]]] | None = None,
    extra_rows: list[object] | None = None,
) -> dict[str, object]:
    """Seed project + run (+ 台账行 + artifact 行 + 任意附加行)；返回构造 context 所需的 id。"""
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"promise-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            project_id = "project-1"
            uow.session.add(ProjectModel(id=project_id, owner_id=user.id, slug=project_id, title="Promise Test"))
            await uow.session.flush()
            now = datetime.now(UTC)
            run = AgentRunModel(
                id=new_id(), user_id=user.id, project_id=project_id,
                goal_hash="g" * 64, goal=GOAL, graph_revision=1, status="RUNNING",
                budget_limit=1000, created_at=now, updated_at=now,
            )
            uow.session.add(run)
            await uow.session.flush()
            artifact_ids: dict[str, str] = {}
            for task_key, _artifact_type, payload in artifacts or []:
                artifact_id = new_id()
                uow.session.add(AgentArtifactModel(
                    id=artifact_id, run_id=run.id, task_id=None, artifact_type="report",
                    sha256="s" * 64, preview=str(payload.get("summary") or "")[:100],
                    payload=json.dumps(payload, ensure_ascii=False),
                ))
                artifact_ids[task_key] = artifact_id
            for row in promises or []:
                uow.session.add(row)
            for row in extra_rows or []:
                # 逐行 flush 保插入顺序：retrieval document 必须先于其 chunks（FK）。
                uow.session.add(row)
                await uow.session.flush()
            await uow.commit()
            return {"run_id": run.id, "project_id": project_id, "artifact_ids": artifact_ids}
    finally:
        await engine.dispose()


def _context(settings: Settings, seeded: dict[str, object], provider: FakeProvider, task_key: str) -> dict[str, object]:
    engine, factory = create_engine_and_sessionmaker(settings)
    # engine 随 settings 生命周期：测试进程内不显式 dispose（tmp_path 库，逐个测试隔离）。
    context_engine_holder.append(engine)
    artifacts = [
        {"id": artifact_id, "task_key": key, "artifact_type": "report", "preview": ""}
        for key, artifact_id in seeded["artifact_ids"].items()
    ]
    return {
        "run": {"id": seeded["run_id"], "goal": GOAL, "goal_hash": "g" * 64, "project_id": seeded["project_id"]},
        "task": {"id": "task-1", "role": "promise_keeper", "task_key": task_key},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": artifacts,
        "uow_factory": lambda: SqlAlchemyUnitOfWork(factory),
    }


context_engine_holder: list[object] = []


async def _read_promises(settings: Settings) -> dict[str, dict[str, object]]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            rows = (await uow.session.scalars(select(StoryBibleEntryModel))).all()
            return {
                row.key: {"status": row.status, "version": row.version, "confidence": row.confidence, "source": row.source, "value": json.loads(row.value_json)}
                for row in rows
            }
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# promise_contract：契约卡组装
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_assembles_ledger_and_hooks(handler_settings):
    provider = FakeProvider(payloads={
        "promise_contract": {
            "summary": "本章契约",
            "due": [{"key": "师父的遗物", "source_chapter": 1, "evidence": "师父临终留下木匣", "required_fulfillments": 1, "remaining": 1, "reason": "goal 钩子行回收项"}],
            "plant": [{"hook": "玉佩裂痕", "note": "埋入项"}],
            "watch": [{"topic": "主角左臂带伤", "note": "上一章受伤未愈"}],
        },
    })
    seeded = await _seed(handler_settings, promises=[
        _promise_row("project-1", "师父的遗物", {"note": "木匣来历", "introduced_chapter": 1}, status="developing"),
    ])

    result = await promise_keeper_handler(_context(handler_settings, seeded, provider, "promise_contract"))

    assert result.artifact_type == "report"
    assert [item["key"] for item in result.payload["due"]] == ["师父的遗物"]
    assert [item["hook"] for item in result.payload["plant"]] == ["玉佩裂痕"]
    # 台账与 goal 钩子行都进了 prompt（双路之台账直查；sqlite 下 vector leg 缺位）
    prompt = provider.user_prompts[0]
    assert "师父的遗物（developing，已兑现 0 次）" in prompt
    assert "回收：师父的遗物" in prompt and "埋入：玉佩裂痕" in prompt
    # 契约卡渲染：due 带来源章与依据，供 scene_writer 注入
    card = render_promise_contract(result.payload)
    assert "「师父的遗物」" in card and "埋设于第1章" in card and "玉佩裂痕" in card and "左臂带伤" in card


@pytest.mark.asyncio
async def test_contract_model_failure_degrades_to_empty_card(handler_settings):
    provider = FakeProvider(fail=True)
    seeded = await _seed(handler_settings)

    result = await promise_keeper_handler(_context(handler_settings, seeded, provider, "promise_contract"))

    # 降级：artifact 仍产出（任务不失败），degraded 标记使注入处不注入
    assert result.payload["degraded"] is True
    assert result.payload["due"] == [] and result.payload["plant"] == []
    assert render_promise_contract(result.payload) == ""


# ---------------------------------------------------------------------------
# promise_verify：逐条核对 + 打勾（多次兑现计数）
# ---------------------------------------------------------------------------


def _select_artifact() -> tuple[str, str, dict[str, object]]:
    return ("select", "report", {"title": "回城", "content": "他跪下来，接过那只木匣。", "selected_from": ["scene_a", "scene_b"]})


def _contract_artifact(due: list[dict[str, object]]) -> tuple[str, str, dict[str, object]]:
    return ("promise_contract", "report", {"summary": "契约", "due": due, "plant": [], "watch": []})


@pytest.mark.asyncio
async def test_verify_single_fulfillment_marks_resolved(handler_settings):
    provider = FakeProvider(payloads={
        "promise_verify": {"verdicts": [{"key": "师父的遗物", "fulfilled": True, "quote": "接过那只木匣"}]},
    })
    seeded = await _seed(
        handler_settings,
        promises=[_promise_row("project-1", "师父的遗物", {"note": "木匣来历"})],
        artifacts=[_select_artifact(), _contract_artifact([{"key": "师父的遗物", "reason": "goal 回收"}])],
    )

    result = await promise_keeper_handler(_context(handler_settings, seeded, provider, "promise_verify"))

    rows = await _read_promises(handler_settings)
    row = rows["师父的遗物"]
    assert row["status"] == "resolved"  # 默认 required_fulfillments=1：open→developing→resolved 打勾
    assert row["version"] == 2
    # 兑现 evidence 现在顺手补段落锚点（paragraph_id/content_hash），
    # chapter/quote 语义不变。
    fulfillment = row["value"]["fulfillments"][0]
    assert fulfillment["chapter"] == 3 and fulfillment["quote"] == "接过那只木匣"
    assert fulfillment["paragraph_id"].startswith("p") and len(fulfillment["content_hash"]) == 64
    assert row["value"]["resolved_chapter"] == 3
    assert result.payload["resolved"] == ["师父的遗物"]
    assert result.extra_events == []  # 全部兑现，无 promise_missed


@pytest.mark.asyncio
async def test_verify_multi_fulfillment_counts_before_resolving(handler_settings):
    provider = FakeProvider(payloads={
        "promise_verify": [
            {"verdicts": [{"key": "三次救恩", "fulfilled": True, "quote": "他挡在她身前"}]},
            {"verdicts": [{"key": "三次救恩", "fulfilled": True, "quote": "他再次挡在她身前"}]},
        ],
    })
    seeded = await _seed(
        handler_settings,
        promises=[_promise_row("project-1", "三次救恩", {"note": "须三次", "required_fulfillments": 2})],
        artifacts=[_select_artifact(), _contract_artifact([{"key": "三次救恩", "remaining": 2}])],
    )

    first = await promise_keeper_handler(_context(handler_settings, seeded, provider, "promise_verify"))

    rows = await _read_promises(handler_settings)
    row = rows["三次救恩"]
    assert row["status"] == "developing"  # 1/2 次：open→developing，未打勾
    assert row["version"] == 2
    assert len(row["value"]["fulfillments"]) == 1
    assert first.payload["resolved"] == []

    second = await promise_keeper_handler(_context(handler_settings, seeded, provider, "promise_verify"))

    rows = await _read_promises(handler_settings)
    row = rows["三次救恩"]
    assert row["status"] == "resolved"  # 累计 2 次达到 required_fulfillments 才打勾
    assert row["version"] == 3
    assert len(row["value"]["fulfillments"]) == 2
    assert second.payload["resolved"] == ["三次救恩"]


@pytest.mark.asyncio
async def test_verify_missed_records_advisory_event(handler_settings):
    provider = FakeProvider(payloads={
        "promise_verify": {"verdicts": [{"key": "师父的遗物", "fulfilled": False, "quote": ""}]},
    })
    seeded = await _seed(
        handler_settings,
        promises=[_promise_row("project-1", "师父的遗物", {"note": "木匣来历"})],
        artifacts=[_select_artifact(), _contract_artifact([{"key": "师父的遗物"}])],
    )

    result = await promise_keeper_handler(_context(handler_settings, seeded, provider, "promise_verify"))

    rows = await _read_promises(handler_settings)
    assert rows["师父的遗物"]["status"] == "open"  # 未兑现不动状态机
    assert rows["师父的遗物"]["version"] == 1
    assert result.extra_events == [{"event": "chapter.promise_missed", "key": "师父的遗物", "chapter": 3}]


# ---------------------------------------------------------------------------
# promise_register：新承诺登记 + 语义判重
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_creates_and_merges(handler_settings):
    provider = FakeProvider(payloads={
        "promise_register": {
            "promises": [
                {"key": "玉佩裂痕", "category": "钩子", "note": "玉佩出现裂痕", "duplicate_of": None},
                {"key": "遗物的新说法", "category": "受伤", "note": "木匣其实有两层", "duplicate_of": "师父的遗物"},
                {"key": "师父的遗物", "category": "伏笔", "note": "key 完全重复", "duplicate_of": None},
            ],
        },
    })
    seeded = await _seed(
        handler_settings,
        promises=[_promise_row("project-1", "师父的遗物", {"note": "木匣来历"}, status="developing")],
        artifacts=[_select_artifact()],
    )

    result = await promise_keeper_handler(_context(handler_settings, seeded, provider, "promise_register"))

    assert result.payload["registered"] == ["玉佩裂痕"]
    # 语义判重 + key 完全重复两条都并入同一既有行
    assert len(result.payload["merged"]) == 2 and set(result.payload["merged"]) == {"师父的遗物"}
    rows = await _read_promises(handler_settings)
    new_row = rows["玉佩裂痕"]
    assert new_row["status"] == "open" and new_row["source"] == "auto" and new_row["confidence"] == 0.6
    assert new_row["value"]["category"] == "钩子" and new_row["value"]["introduced_chapter"] == 3
    old_row = rows["师父的遗物"]
    assert old_row["status"] == "developing"  # 并入不改状态、不删行
    assert old_row["version"] == 3  # 两次并入各 +1
    assert "木匣来历" in old_row["value"]["note"] and "木匣其实有两层" in old_row["value"]["note"] and "key 完全重复" in old_row["value"]["note"]


@pytest.mark.asyncio
async def test_register_unknown_category_falls_back(handler_settings):
    provider = FakeProvider(payloads={
        "promise_register": {"promises": [{"key": "神秘低语", "category": "悬念", "note": "不是五类"}]},
    })
    seeded = await _seed(handler_settings, artifacts=[_select_artifact()])

    await promise_keeper_handler(_context(handler_settings, seeded, provider, "promise_register"))

    rows = await _read_promises(handler_settings)
    assert rows["神秘低语"]["value"]["category"] == "伏笔"


# ---------------------------------------------------------------------------
# 降级路径：artifact 缺失 / 模型失败不抛错、不注入
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_without_artifacts_skips_quietly(handler_settings):
    provider = FakeProvider()
    seeded = await _seed(handler_settings)  # 无终稿 artifact

    result = await promise_keeper_handler(_context(handler_settings, seeded, provider, "promise_verify"))

    assert "跳过" in result.payload["summary"]
    assert result.payload["verdicts"] == []
    assert provider.requests == []  # 不调模型


@pytest.mark.asyncio
async def test_verify_model_failure_degrades_without_writes(handler_settings):
    provider = FakeProvider(fail=True)
    seeded = await _seed(
        handler_settings,
        promises=[_promise_row("project-1", "师父的遗物", {"note": "木匣来历"})],
        artifacts=[_select_artifact(), _contract_artifact([{"key": "师父的遗物"}])],
    )

    result = await promise_keeper_handler(_context(handler_settings, seeded, provider, "promise_verify"))

    assert result.payload["degraded"] is True
    rows = await _read_promises(handler_settings)
    assert rows["师父的遗物"]["status"] == "open" and rows["师父的遗物"]["version"] == 1
    assert result.extra_events == []  # 判不了就不漏报


@pytest.mark.asyncio
async def test_register_model_failure_degrades(handler_settings):
    provider = FakeProvider(fail=True)
    seeded = await _seed(handler_settings, artifacts=[_select_artifact()])

    result = await promise_keeper_handler(_context(handler_settings, seeded, provider, "promise_register"))

    assert result.payload["degraded"] is True
    assert await _read_promises(handler_settings) == {}


@pytest.mark.asyncio
async def test_render_contract_empty_payload_renders_nothing():
    assert render_promise_contract({}) == ""
    assert render_promise_contract({"due": [], "plant": [], "watch": []}) == ""
    assert render_promise_contract("not-a-dict") == ""


# ---------------------------------------------------------------------------
# promise_verify RAG 定向取证（PROSEFORGE_PROMISE_RAG_VERIFY）+ 降级路径
# ---------------------------------------------------------------------------


@pytest.fixture()
def rag_handler_settings(handler_settings, monkeypatch):
    """开关开启变体：handler 走 get_settings() 读 env，故 setenv + 清缓存。"""
    monkeypatch.setenv("PROSEFORGE_PROMISE_RAG_VERIFY", "true")
    get_settings.cache_clear()
    yield handler_settings
    get_settings.cache_clear()


def _chapter_rows(project_id: str, contents: list[str], *, chapter_no: int) -> list[object]:
    """已索引历史章节的 retrieval document + chunks（本章草稿不在索引里，只种旧章）。"""
    now = datetime.now(UTC)
    document = RetrievalDocumentModel(
        id=new_id(), project_id=project_id, source_type="chapter", source_id=f"chapter-{chapter_no}",
        source_version="v1", title=f"第{chapter_no}章", status="active",
        chapter_from=chapter_no, chapter_to=chapter_no, created_at=now, updated_at=now,
    )
    chunks = [
        RetrievalChunkModel(
            id=new_id(), project_id=project_id, document_id=document.id, chunk_index=index,
            content=content, metadata_json=json.dumps({"chapter_no": chapter_no}),
            content_hash=f"hash-{chapter_no}-{index}", status="active", created_at=now, updated_at=now,
        )
        for index, content in enumerate(contents)
    ]
    return [document, *chunks]


def _rag_select_artifact() -> tuple[str, str, dict[str, object]]:
    return ("select", "report", {
        "title": "回城",
        "content": "他跪下来，接过那只木匣。\n\n师父的遗物终于交到他手上。",
        "selected_from": ["scene_a", "scene_b"],
    })


@pytest.mark.asyncio
async def test_verify_rag_targeted_evidence_hit(rag_handler_settings):
    """RAG 取证命中路径：本章（第3章）未索引，历史承诺（埋设于第1章）照常 RAG 取证。"""
    provider = FakeProvider(payloads={
        "promise_verify": {"verdicts": [{"key": "师父的遗物", "fulfilled": True, "quote": "接过那只木匣"}]},
    })
    seeded = await _seed(
        rag_handler_settings,
        promises=[_promise_row("project-1", "师父的遗物", {"note": "木匣来历"})],
        artifacts=[
            _rag_select_artifact(),
            _contract_artifact([{"key": "师父的遗物", "reason": "goal 回收", "evidence": "师父临终留下木匣"}]),
        ],
        extra_rows=_chapter_rows("project-1", ["师父临终前留下一只木匣，嘱咐他务必带回城中。"], chapter_no=1),
    )

    result = await promise_keeper_handler(_context(rag_handler_settings, seeded, provider, "promise_verify"))

    prompt = provider.user_prompts[0]
    assert "承诺定向取证证据" in prompt and "终稿正文：" not in prompt  # 定向取证替代全章通读
    assert "师父临终前留下一只木匣" in prompt  # 历史章节 RAG 证据（索引里只有第1章，本章未索引）
    assert "本章相关段落" in prompt and "师父的遗物终于交到他手上" in prompt  # 本章直查分段扫描
    rag_events = [event for event in result.extra_events if event["event"] == "chapter.promise_rag_verify"]
    assert len(rag_events) == 1
    assert rag_events[0]["chapter"] == 3 and rag_events[0]["rag_passages"] >= 1 and rag_events[0]["scanned_paragraphs"] >= 1
    rows = await _read_promises(rag_handler_settings)
    assert rows["师父的遗物"]["status"] == "resolved"  # 判定/打勾语义不变


@pytest.mark.asyncio
async def test_verify_rag_empty_evidence_falls_back(rag_handler_settings):
    """取证为空（索引无命中 + 本章直查无匹配段落）→ 显式回落台账+全章直查旧路径 + 降级事件。"""
    provider = FakeProvider(payloads={
        "promise_verify": {"verdicts": [{"key": "师父的遗物", "fulfilled": True, "quote": "接过那只木匣"}]},
    })
    seeded = await _seed(
        rag_handler_settings,
        promises=[_promise_row("project-1", "师父的遗物", {"note": "木匣来历"})],
        # 索引为空；due 条目只有 key，终稿段落不含「师父/遗物」词项 → 两路取证皆空
        artifacts=[_select_artifact(), _contract_artifact([{"key": "师父的遗物"}])],
    )

    result = await promise_keeper_handler(_context(rag_handler_settings, seeded, provider, "promise_verify"))

    prompt = provider.user_prompts[0]
    assert "终稿正文：" in prompt and "承诺定向取证证据" not in prompt  # 回落旧路径
    assert {"event": "chapter.promise_rag_verify_fallback", "chapter": 3, "reason": "no_evidence"} in result.extra_events
    rows = await _read_promises(rag_handler_settings)
    assert rows["师父的遗物"]["status"] == "resolved"  # 回落后 verify 照常出结果


@pytest.mark.asyncio
async def test_verify_rag_index_error_falls_back(rag_handler_settings, monkeypatch):
    """索引异常 → 回落旧路径 + index_error 事件，异常绝不影响 verify 出结果。"""
    async def _boom(*args: object, **kwargs: object) -> list[str]:
        raise RuntimeError("index down")

    monkeypatch.setattr(promise_handlers, "_rag_evidence", _boom)
    provider = FakeProvider(payloads={
        "promise_verify": {"verdicts": [{"key": "师父的遗物", "fulfilled": True, "quote": "接过那只木匣"}]},
    })
    seeded = await _seed(
        rag_handler_settings,
        promises=[_promise_row("project-1", "师父的遗物", {"note": "木匣来历"})],
        artifacts=[_select_artifact(), _contract_artifact([{"key": "师父的遗物"}])],
    )

    result = await promise_keeper_handler(_context(rag_handler_settings, seeded, provider, "promise_verify"))

    prompt = provider.user_prompts[0]
    assert "终稿正文：" in prompt
    assert {"event": "chapter.promise_rag_verify_fallback", "chapter": 3, "reason": "index_error"} in result.extra_events
    rows = await _read_promises(rag_handler_settings)
    assert rows["师父的遗物"]["status"] == "resolved"


@pytest.mark.asyncio
async def test_verify_rag_switch_off_uses_full_text(handler_settings):
    """开关关闭（默认）：即使索引里有可命中证据，也走全章通读旧路径、不落 RAG 事件。"""
    provider = FakeProvider(payloads={
        "promise_verify": {"verdicts": [{"key": "师父的遗物", "fulfilled": True, "quote": "接过那只木匣"}]},
    })
    seeded = await _seed(
        handler_settings,
        promises=[_promise_row("project-1", "师父的遗物", {"note": "木匣来历"})],
        artifacts=[_select_artifact(), _contract_artifact([{"key": "师父的遗物", "reason": "goal 回收"}])],
        extra_rows=_chapter_rows("project-1", ["师父临终前留下一只木匣。"], chapter_no=1),
    )

    result = await promise_keeper_handler(_context(handler_settings, seeded, provider, "promise_verify"))

    prompt = provider.user_prompts[0]
    assert "终稿正文：" in prompt and "承诺定向取证证据" not in prompt
    assert not [event for event in result.extra_events if str(event["event"]).startswith("chapter.promise_rag_verify")]
    rows = await _read_promises(handler_settings)
    assert rows["师父的遗物"]["status"] == "resolved"


# -- 兑现 evidence 段落锚点补强（apply_fulfillment + locate_anchor）--------------


def test_apply_fulfillment_stores_paragraph_anchor() -> None:
    """带锚点落 evidence：fulfillment 补 paragraph_id/content_hash 两键
    （改段后按 content_hash 对账失效引用）；index 键不落库。"""
    from proseforge.application.agents.promise_handlers import apply_fulfillment

    row = _promise_row("project-1", "师父的遗物", {"note": "木匣来历"})
    apply_fulfillment(
        row, chapter_no=3, quote="木匣在灯下裂开", now=datetime.now(UTC),
        anchor={"paragraph_id": "p0004", "index": 4, "content_hash": "ab12"},
    )
    value = json.loads(row.value_json)
    assert value["fulfillments"] == [
        {"chapter": 3, "quote": "木匣在灯下裂开", "paragraph_id": "p0004", "content_hash": "ab12"}
    ]


def test_apply_fulfillment_without_anchor_keeps_legacy_shape() -> None:
    """找不到锚点（或调用方没传）时不加键：旧格式 {chapter, quote} 完全兼容。"""
    from proseforge.application.agents.promise_handlers import apply_fulfillment

    row = _promise_row("project-1", "师父的遗物", {"note": "木匣来历"})
    apply_fulfillment(row, chapter_no=3, quote="木匣在灯下裂开", now=datetime.now(UTC), anchor=None)
    value = json.loads(row.value_json)
    assert value["fulfillments"] == [{"chapter": 3, "quote": "木匣在灯下裂开"}]


def test_apply_fulfillment_anchor_comes_from_locate_anchor() -> None:
    """调用处组合：终稿分段 → locate_anchor → 锚点与写回时的
    chapter_versions.paragraph_anchors 同套切分/哈希，可确定性复算。"""
    from proseforge.application.agents.promise_handlers import apply_fulfillment
    from proseforge.domain.chapter.paragraphs import (
        content_hash,
        locate_anchor,
        split_paragraphs,
    )

    content = "第一段起势。\n\n第二段木匣在灯下裂开。\n\n第三段收束。"
    paragraphs, _separators = split_paragraphs(content)
    anchor = locate_anchor(paragraphs, "木匣在灯下裂开")
    assert anchor is not None

    row = _promise_row("project-1", "师父的遗物", {"note": "木匣来历"})
    apply_fulfillment(row, chapter_no=3, quote="木匣在灯下裂开", now=datetime.now(UTC), anchor=anchor)
    fulfillment = json.loads(row.value_json)["fulfillments"][0]
    assert fulfillment["paragraph_id"] == "p0001"
    assert fulfillment["content_hash"] == content_hash(paragraphs[1])

    # 引文找不到锚点：不加键，落库不炸。
    missing = locate_anchor(paragraphs, "不存在的引文")
    assert missing is None
    row2 = _promise_row("project-1", "三日之约", {"note": "谷口之约"})
    apply_fulfillment(row2, chapter_no=3, quote="不存在的引文", now=datetime.now(UTC), anchor=missing)
    assert json.loads(row2.value_json)["fulfillments"] == [{"chapter": 3, "quote": "不存在的引文"}]

"""米开朗基罗·定点改写（pinpoint_rewrite.py）宿主可跑测试。

sqlite+aiosqlite 真实落库 + FakeProvider 假模型（无网络、无 PG），种子模式
复制自 tests/agents/test_chief_handler.py。覆盖：
- 段落锚点生成与 append_version 写回挂钩（chapter_versions.paragraph_anchors）；
- issue 定位段落改写后其余段落字节级不变；
- 衔接检查（与邻段雷同 → 回退原文 + 审计事件）；
- evidence 引用失效对账（重锚定成功 / 失败上报奥莉维亚两路）；
- 整章重写兜底开关（rewrite_mode=full）与全局性门禁原因回退；
- 审校 findings 段落引用（paragraph_refs）。
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from proseforge.application.agents.pinpoint_rewrite import (
    annotate_findings_paragraphs,
    try_pinpoint_rewrite,
)
from proseforge.domain.chapter.entity import Chapter
from proseforge.domain.chapter.paragraphs import (
    anchors_json,
    build_anchors,
    content_hash,
    join_paragraphs,
    locate_quote,
    split_paragraphs,
)
from proseforge.domain.common.ids import new_id
from proseforge.domain.ports.model_provider import GenerationEvent
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.agents import (
    AgentArtifactModel,
    AgentRunModel,
)
from proseforge.infrastructure.database.models.chapter import ChapterVersionModel
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.security.credential_cipher import CredentialCipher
from proseforge.settings import Settings, get_settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()

SCENE_CONTENT = "雨夜，主角提着青铜钥匙回城。\n\n城门已闭，守卫盘问姓名。\n\n主角绕到水门，青铜钥匙派上用场。"


@pytest.fixture()
def executor_settings(tmp_path, monkeypatch):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'agents.db').as_posix()}"
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
    provider_id = "fake"

    def __init__(self, payloads: dict[str, object] | None = None, usage: tuple[int, int] = (4, 2)):
        self._payloads = payloads or {}
        self._input, self._output = usage
        self.requests: list[dict[str, str]] = []

    async def stream(self, request):
        self.requests.append(dict(request.metadata))
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


async def _seed(
    settings: Settings,
    *,
    goal: str = "改写第三章",
    review_findings: list[dict[str, object]] | None = None,
    cluster_config: dict[str, object] | None = None,
    promise_fulfillments: list[dict[str, object]] | None = None,
) -> dict[str, str]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"pinpoint-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            credential_id = f"cred-{uuid.uuid4().hex[:8]}"
            associated = f"{user.id}:openai:{credential_id}".encode()
            encrypted = CredentialCipher(base64.b64decode(MASTER_KEY)).encrypt(json.dumps({"api_key": "sk-test"}).encode(), associated_data=associated)
            await uow.credentials.create(user.id, "openai", base64.b64encode(encrypted).decode(), record_id=credential_id)
            project = await uow.projects.add(Project.create(owner_id=user.id, slug=f"p-{uuid.uuid4().hex[:8]}", title="Pinpoint"))
            if cluster_config is not None:
                row = await uow.session.get(ProjectModel, project.id)
                row.cluster_config_json = json.dumps(cluster_config, ensure_ascii=False)
            now = datetime.now(UTC)
            run = AgentRunModel(
                id=new_id(), user_id=user.id, project_id=project.id, goal=goal,
                goal_hash="g" * 64, graph_revision=1, status="RUNNING", budget_limit=10000,
                created_at=now, updated_at=now,
            )
            uow.session.add(run)
            await uow.session.flush()
            review_artifact_id = ""
            if review_findings is not None:
                review_artifact_id = f"rev-{uuid.uuid4().hex[:8]}"
                payload = {"report_type": "ContinuityReport", "summary": "s", "issues": review_findings}
                raw = json.dumps(payload, ensure_ascii=False)
                uow.session.add(AgentArtifactModel(
                    id=review_artifact_id, run_id=run.id, task_id=None, artifact_type="report",
                    sha256=content_hash(raw), provenance="{}", preview=raw[:200], payload=raw,
                ))
            if promise_fulfillments is not None:
                uow.session.add(StoryBibleEntryModel(
                    id=new_id(), project_id=project.id, kind="promise", key="青铜钥匙的来历",
                    value_json=json.dumps({"note": "伏笔", "fulfillments": promise_fulfillments}, ensure_ascii=False),
                    status="open", confidence=0.6, source="auto", pinned=False,
                    version=1, created_at=now, updated_at=now,
                ))
            await uow.commit()
            return {"run_id": run.id, "user_id": user.id, "project_id": project.id, "review_artifact_id": review_artifact_id}
    finally:
        await engine.dispose()


def _context(settings: Settings, seeded: dict[str, str], provider: FakeProvider, **overrides) -> dict[str, object]:
    _engine, factory = create_engine_and_sessionmaker(settings)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(factory)

    artifacts: list[dict[str, object]] = []
    if seeded.get("review_artifact_id"):
        artifacts.append({"id": seeded["review_artifact_id"], "task_key": "review_continuity", "artifact_type": "report", "preview": ""})
    context: dict[str, object] = {
        "run": {"id": seeded["run_id"], "goal": "改写第三章", "project_id": seeded["project_id"]},
        "task": {"id": "task-1", "role": "chief_editor", "task_key": "rewrite"},
        "provider": provider,
        "provider_id": "fake",
        "model": "fake-model",
        "artifacts": artifacts,
        "uow_factory": uow_factory,
    }
    context.update(overrides)
    return context


def _scene(content: str = SCENE_CONTENT) -> dict[str, object]:
    return {"id": "scene-art-1", "title": "回城", "content": content}


# ---------------------------------------------------------------------------
# 段落锚点：切分/重组字节级一致 + append_version 写回挂钩
# ---------------------------------------------------------------------------


def test_split_join_roundtrip_is_byte_exact():
    for content in (
        SCENE_CONTENT,
        "单段无换行",
        "甲\n\n\n乙",  # 多个换行视为一个分隔符
        "甲\n　\n乙",  # 全角空格填充的空行
        "甲\n\n乙\n\n",  # 尾部空行
    ):
        paragraphs, separators = split_paragraphs(content)
        assert join_paragraphs(paragraphs, separators) == content


def test_build_anchors_and_locate_quote():
    anchors = build_anchors(SCENE_CONTENT)
    assert [anchor["paragraph_id"] for anchor in anchors] == ["p0000", "p0001", "p0002"]
    assert anchors[1]["content_hash"] == content_hash("城门已闭，守卫盘问姓名。")
    paragraphs, _ = split_paragraphs(SCENE_CONTENT)
    assert locate_quote(paragraphs, "城门已闭") == [1]
    assert locate_quote(paragraphs, "青铜钥匙") == [0, 2]  # 跨段命中全部返回
    assert locate_quote(paragraphs, "不存在的引文") == []
    # 空白归一兜底：引文里的换行差异不影响定位
    assert locate_quote(["甲\n乙丙"], "甲乙 丙") == [0]


@pytest.mark.asyncio
async def test_append_version_stores_paragraph_anchors(executor_settings):
    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"anchors-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            project = await uow.projects.add(Project.create(owner_id=user.id, slug=f"p-{uuid.uuid4().hex[:8]}", title="Anchors"))
            chapter = await uow.chapters.add(Chapter.create(project_id=project.id, chapter_no=1, title="第一章"))
            version = await uow.chapters.append_version(chapter_id=chapter.id, content=SCENE_CONTENT)
            await uow.commit()
            assert version.paragraph_anchors == anchors_json(SCENE_CONTENT)
            # 去重路径（同 content_hash 复用既有行）：锚点仍是首建行生成的那份
            duplicate = await uow.chapters.append_version(chapter_id=chapter.id, content=SCENE_CONTENT)
            assert duplicate.id == version.id
            assert duplicate.paragraph_anchors == version.paragraph_anchors
        async with SqlAlchemyUnitOfWork(factory) as uow:
            row = await uow.session.get(ChapterVersionModel, version.id)
            anchors = json.loads(row.paragraph_anchors)
            assert [anchor["paragraph_id"] for anchor in anchors] == ["p0000", "p0001", "p0002"]
            assert anchors[0]["start"] == 0 and anchors[0]["end"] == len("雨夜，主角提着青铜钥匙回城。")
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 定点改写：只改标注段，其余段落字节级不变
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pinpoint_rewrite_keeps_other_paragraphs_byte_exact(executor_settings):
    provider = FakeProvider(payloads={
        "rewrite": {"title": "回城（终稿）", "rewrites": [{"index": 1, "content": "城门大开，守卫酣睡未醒。"}]},
    })
    seeded = await _seed(executor_settings, review_findings=[
        {"finding": "城门状态与前章矛盾", "severity": "high", "evidence_spans": [{"artifact_id": "", "start": 0, "end": 4, "quote": "城门已闭"}]},
    ])
    context = _context(executor_settings, seeded, provider)

    result = await try_pinpoint_rewrite(context, _scene())

    assert result is not None
    expected = "雨夜，主角提着青铜钥匙回城。\n\n城门大开，守卫酣睡未醒。\n\n主角绕到水门，青铜钥匙派上用场。"
    assert result.payload["content"] == expected  # p0000/p0002 与分隔符逐字节不变
    assert result.payload["rewrite_of"] == "scene-art-1"
    assert result.payload["title"] == "回城（终稿）"
    pinpoint = result.payload["pinpoint"]
    assert pinpoint["mode"] == "pinpoint"
    assert pinpoint["changed_paragraphs"] == [{
        "paragraph_id": "p0001", "index": 1,
        "old_hash": content_hash("城门已闭，守卫盘问姓名。"),
        "new_hash": content_hash("城门大开，守卫酣睡未醒。"),
    }]
    pinpoint_events = [event for event in result.extra_events if event["event"] == "rewrite.pinpoint"]
    assert pinpoint_events == [{"event": "rewrite.pinpoint", "changed_paragraphs": ["p0001"], "reverted_paragraphs": [], "located_issues": 1, "total_issues": 1}]
    assert provider.requests and provider.requests[0]["task_key"] == "rewrite"


@pytest.mark.asyncio
async def test_pinpoint_rewrite_full_text_output_accepted_as_degradation(executor_settings):
    provider = FakeProvider(payloads={"rewrite": {"title": "回城（终稿）", "content": "整章重写文本。"}})
    seeded = await _seed(executor_settings, review_findings=[
        {"finding": "矛盾", "severity": "high", "evidence_spans": [{"quote": "城门已闭"}]},
    ])
    context = _context(executor_settings, seeded, provider)

    result = await try_pinpoint_rewrite(context, _scene())

    assert result is not None
    assert result.payload["content"] == "整章重写文本。"
    assert result.payload["pinpoint"]["mode"] == "full_text_fallback"
    assert any(event["event"] == "rewrite.pinpoint_fallback" for event in result.extra_events)


@pytest.mark.asyncio
async def test_pinpoint_rewrite_cohesion_reverts_neighbor_duplicate(executor_settings):
    # 模型把 p0000 改成与邻段 p0001 雷同 → 衔接检查回退该段（保留原文），p0002 照常改写
    provider = FakeProvider(payloads={
        "rewrite": {"rewrites": [
            {"index": 0, "content": "城门已闭，守卫盘问姓名。"},
            {"index": 2, "content": "主角亮出青铜钥匙，从水门潜入城中。"},
        ]},
    })
    seeded = await _seed(executor_settings, review_findings=[
        {"finding": "开头矛盾", "severity": "high", "evidence_spans": [{"quote": "雨夜"}]},
        {"finding": "结尾仓促", "severity": "medium", "evidence_spans": [{"quote": "水门"}]},
    ])
    context = _context(executor_settings, seeded, provider)

    result = await try_pinpoint_rewrite(context, _scene())

    assert result is not None
    paragraphs, _separators = split_paragraphs(str(result.payload["content"]))
    assert paragraphs[0] == "雨夜，主角提着青铜钥匙回城。"  # 回退：原文字节级保留
    assert paragraphs[1] == "城门已闭，守卫盘问姓名。"
    assert paragraphs[2] == "主角亮出青铜钥匙，从水门潜入城中。"
    cohesion = {event["paragraph_id"]: event for event in result.extra_events if event["event"] == "rewrite.cohesion"}
    assert cohesion["p0000"]["action"] == "reverted"
    assert cohesion["p0000"]["flags"] == ["duplicates_neighbor"]
    assert cohesion["p0002"]["action"] == "kept"
    assert result.payload["pinpoint"]["reverted_paragraphs"] == ["p0000"]


# ---------------------------------------------------------------------------
# 兜底开关与回退路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_rewrite_switch_falls_back(executor_settings):
    provider = FakeProvider(payloads={"rewrite": {"rewrites": [{"index": 1, "content": "改写段"}]}})
    seeded = await _seed(
        executor_settings,
        review_findings=[{"finding": "矛盾", "severity": "high", "evidence_spans": [{"quote": "城门已闭"}]}],
        cluster_config={"mode": "cluster", "rewrite_mode": "full"},
    )
    context = _context(executor_settings, seeded, provider)

    assert await try_pinpoint_rewrite(context, _scene()) is None
    assert provider.requests == []  # 开关开启：不调模型，走整章重写旧路径


@pytest.mark.asyncio
async def test_global_gate_reason_falls_back(executor_settings):
    provider = FakeProvider()
    seeded = await _seed(executor_settings, review_findings=[
        {"finding": "矛盾", "severity": "high", "evidence_spans": [{"quote": "城门已闭"}]},
    ])
    context = _context(executor_settings, seeded, provider, gate_reasons=["字数不足：100 < 2500"])

    assert await try_pinpoint_rewrite(context, _scene()) is None
    assert provider.requests == []


@pytest.mark.asyncio
async def test_unlocatable_quote_falls_back(executor_settings):
    provider = FakeProvider()
    seeded = await _seed(executor_settings, review_findings=[
        {"finding": "幻觉引文", "severity": "high", "evidence_spans": [{"quote": "根本不在正文里的引文"}]},
    ])
    context = _context(executor_settings, seeded, provider)

    assert await try_pinpoint_rewrite(context, _scene()) is None
    assert provider.requests == []  # 定位不到段落：不调模型，整章重写兜底


# ---------------------------------------------------------------------------
# evidence 引用失效对账：重锚定成功 / 失败上报奥莉维亚
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_reanchors_stale_evidence(executor_settings):
    # p0000 被改写（"青铜钥匙"从该段消失），但 p0002 仍有同一引文 → 重锚定到 p0002
    provider = FakeProvider(payloads={
        "rewrite": {"rewrites": [{"index": 0, "content": "雨夜，主角空着手回城。"}]},
    })
    seeded = await _seed(
        executor_settings,
        review_findings=[{"finding": "开头要改", "severity": "high", "evidence_spans": [{"quote": "雨夜"}]}],
        promise_fulfillments=[{"chapter": 3, "quote": "青铜钥匙"}],
    )
    context = _context(executor_settings, seeded, provider)

    result = await try_pinpoint_rewrite(context, _scene())

    assert result is not None
    reanchored = [event for event in result.extra_events if event["event"] == "promise.evidence_reanchored"]
    assert reanchored == [{"event": "promise.evidence_reanchored", "key": "青铜钥匙的来历", "chapter": 3, "paragraph_id": "p0002", "old_paragraph_id": "p0000"}]
    assert not any(event["event"] == "promise.evidence_stale" for event in result.extra_events)
    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            row = await uow.session.scalar(select(StoryBibleEntryModel).where(StoryBibleEntryModel.project_id == seeded["project_id"]))
            value = json.loads(row.value_json)
            fulfillment = value["fulfillments"][0]
            assert fulfillment["paragraph_id"] == "p0002"
            assert fulfillment["content_hash"] == content_hash("主角绕到水门，青铜钥匙派上用场。")
            assert fulfillment["reanchored_from"] == content_hash("雨夜，主角提着青铜钥匙回城。")
            assert row.version == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_reports_stale_when_unanchorable(executor_settings):
    # 引文只在被改段出现，改写后全章找不到 → 落 promise.evidence_stale 上报奥莉维亚
    provider = FakeProvider(payloads={
        "rewrite": {"rewrites": [{"index": 1, "content": "城门大开，一路畅通。"}]},
    })
    seeded = await _seed(
        executor_settings,
        review_findings=[{"finding": "城门矛盾", "severity": "high", "evidence_spans": [{"quote": "城门已闭"}]}],
        promise_fulfillments=[{"chapter": 3, "quote": "守卫盘问"}],
    )
    context = _context(executor_settings, seeded, provider)

    result = await try_pinpoint_rewrite(context, _scene())

    assert result is not None
    stale = [event for event in result.extra_events if event["event"] == "promise.evidence_stale"]
    assert stale == [{
        "event": "promise.evidence_stale", "key": "青铜钥匙的来历", "chapter": 3,
        "quote": "守卫盘问", "stale_paragraph_id": "p0001",
        "stale_hash": content_hash("城门已闭，守卫盘问姓名。"),
    }]
    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            row = await uow.session.scalar(select(StoryBibleEntryModel).where(StoryBibleEntryModel.project_id == seeded["project_id"]))
            value = json.loads(row.value_json)
            fulfillment = value["fulfillments"][0]
            assert "paragraph_id" not in fulfillment  # 重锚定失败不写新锚点
            assert row.version == 1  # 无落库变更
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_hash_match_ignores_chapter_mismatch(executor_settings):
    # 新结构 fulfillment（带 content_hash）：hash 精确命中即失效，不受章号限制
    provider = FakeProvider(payloads={
        "rewrite": {"rewrites": [{"index": 0, "content": "雨夜，主角空着手回城。"}]},
    })
    seeded = await _seed(
        executor_settings,
        review_findings=[{"finding": "开头要改", "severity": "high", "evidence_spans": [{"quote": "雨夜"}]}],
        promise_fulfillments=[{
            "chapter": 99, "quote": "青铜钥匙",
            "paragraph_id": "p0000", "content_hash": content_hash("雨夜，主角提着青铜钥匙回城。"),
        }],
    )
    context = _context(executor_settings, seeded, provider)

    result = await try_pinpoint_rewrite(context, _scene())

    assert result is not None
    reanchored = [event for event in result.extra_events if event["event"] == "promise.evidence_reanchored"]
    assert len(reanchored) == 1 and reanchored[0]["paragraph_id"] == "p0002"


@pytest.mark.asyncio
async def test_reconcile_skips_fulfillments_of_other_chapters(executor_settings):
    # 旧结构 fulfillment：章号不匹配不动（quote 巧合落在被改段也不算失效）
    provider = FakeProvider(payloads={
        "rewrite": {"rewrites": [{"index": 1, "content": "城门大开，一路畅通。"}]},
    })
    seeded = await _seed(
        executor_settings,
        review_findings=[{"finding": "城门矛盾", "severity": "high", "evidence_spans": [{"quote": "城门已闭"}]}],
        promise_fulfillments=[{"chapter": 7, "quote": "守卫盘问"}],
    )
    context = _context(executor_settings, seeded, provider)

    result = await try_pinpoint_rewrite(context, _scene())

    assert result is not None
    assert not any(event["event"].startswith("promise.evidence") for event in result.extra_events)


# ---------------------------------------------------------------------------
# 审校 findings 段落引用（review_handlers 挂载的 annotate 步骤）
# ---------------------------------------------------------------------------


def test_annotate_findings_paragraphs():
    findings = [
        {"finding": "城门矛盾", "severity": "high", "target_artifact_id": "art-1",
         "evidence_spans": [{"artifact_id": "art-1", "start": 0, "end": 4, "quote": "城门已闭"}]},
        {"finding": "无引文", "severity": "medium", "target_artifact_id": "art-1", "evidence_spans": []},
    ]
    annotate_findings_paragraphs(findings, {"art-1": SCENE_CONTENT})
    assert findings[0]["paragraph_refs"] == [{"paragraph_id": "p0001", "index": 1, "content_hash": content_hash("城门已闭，守卫盘问姓名。")}]
    assert "paragraph_refs" not in findings[1]


def test_migration_0049_revision_chain():
    import importlib

    module = importlib.import_module(
        "proseforge.infrastructure.database.migrations.versions.0049_paragraph_anchors"
    )
    assert module.revision == "0049_paragraph_anchors"
    assert module.down_revision == "0048_pin_vector_1024"
    assert callable(module.upgrade) and callable(module.downgrade)

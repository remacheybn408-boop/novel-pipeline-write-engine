"""M4 回归测试：评审/改写 handler 的 input_budget 裁剪。

旧实现缺口：
- 评审 handler 按 artifact 各自封顶全文（budget/2 字符/个），N 份上游草稿
  合计可达 N x budget/2 字符，场景包不裁剪，超模型窗口 → 重试 FAILED；
- 改写 handler（chief_editor 管线模式）把场景正文与四桶清单全文直拼 prompt，
  完全没有预算保护。

修复后：评审全文预算在所有被审 artifact 间共享，场景包超预算时按 default
handler 同款 trim_scene_pack 裁剪（记 context.trimmed 事件）；改写 handler 对
正文与清单按预算中段裁剪（elide_middle 首 70% / 尾 20%）。
sqlite+aiosqlite 真实落库（评审行持久化需要 DB），假 provider 记录完整 prompt。
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime

import pytest

from proseforge.application.agents.chief_handler import _rewrite_final_draft
from proseforge.application.agents.review_handlers import _run_reviewer
from proseforge.domain.common.ids import new_id
from proseforge.domain.ports.model_provider import GenerationEvent
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.agents import (
    AgentArtifactModel,
    AgentRunModel,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.settings import Settings, get_settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()
INPUT_BUDGET = 4000  # tokens；估计口径与 role_handlers 一致：chars // 2


@pytest.fixture()
def db_settings(tmp_path, monkeypatch):
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


class RecordingProvider:
    """记录每次请求完整 system/user prompt 的假 provider（无网络）。"""

    provider_id = "fake"

    def __init__(self, payload: dict[str, object]):
        self._payload = payload
        self.requests: list[dict[str, str]] = []

    async def stream(self, request):
        self.requests.append({
            "system_prompt": "\n".join(str(block.get("text", "")) for block in request.system_blocks),
            "user_prompt": "\n".join(str(block.get("text", "")) for block in request.input_blocks),
        })
        yield GenerationEvent("response.started")
        yield GenerationEvent("content.delta", text=json.dumps(self._payload, ensure_ascii=False))
        yield GenerationEvent("response.completed", data={"usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}})


async def _seed_artifacts(settings: Settings, artifacts: list[dict[str, object]]):
    """种子 user/project/run + artifact 行；返回 (engine, session_factory, run_id)。"""
    engine, factory = create_engine_and_sessionmaker(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    run_id = new_id()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        user = await uow.users.create(f"budget-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
        uow.session.add(ProjectModel(id="project-1", owner_id=user.id, slug="project-1", title="Budget Test Project"))
        await uow.session.flush()  # FK parent: agent_runs.project_id -> projects.id
        now = datetime.now(UTC)
        uow.session.add(AgentRunModel(
            id=run_id, user_id=user.id, project_id="project-1", goal="写第三章", goal_hash="g" * 64,
            graph_revision=1, status="RUNNING", budget_limit=1000, created_at=now, updated_at=now,
        ))
        await uow.session.flush()  # FK parent: agent_artifacts/agent_reviews -> agent_runs
        for item in artifacts:
            raw = json.dumps(item["payload"], ensure_ascii=False, sort_keys=True)
            uow.session.add(AgentArtifactModel(
                id=str(item["id"]), run_id=run_id, task_id=None, artifact_type="candidate",
                sha256=hashlib.sha256(raw.encode()).hexdigest(), provenance="{}", preview=raw[:80], payload=raw,
            ))
        await uow.commit()
    return engine, factory, run_id


def _review_context(provider: RecordingProvider, factory, run_id: str, artifacts: list[dict[str, object]], **overrides: object) -> dict[str, object]:
    context: dict[str, object] = {
        # task id 不落库：_task_depends_on 读不到行时 depends_on 为空集（不走 select 过滤/章节注入分支）
        "task": {"id": "task-not-seeded", "role": "continuity_reviewer", "task_key": "review"},
        "run": {"id": run_id, "goal": "写第三章", "goal_hash": "g" * 64},
        "provider": provider,
        "provider_id": "fake",
        "model": "fake-model",
        "uow_factory": lambda: SqlAlchemyUnitOfWork(factory),
        "artifacts": artifacts,
        "scene_pack": None,
        "input_budget": INPUT_BUDGET,
    }
    context.update(overrides)
    return context


def _prompt_estimate(request: dict[str, str]) -> int:
    return (len(request["system_prompt"]) + len(request["user_prompt"])) // 2


@pytest.mark.asyncio
async def test_reviewer_shares_text_budget_across_artifacts(db_settings):
    """4 份 2 万字符上游 artifact：全文预算共享，prompt 估计不超 input_budget。"""
    big = "文" * 20000
    seeded = [{"id": f"art-{i}", "payload": {"title": f"稿{i}", "content": big}} for i in range(4)]
    engine, factory, run_id = await _seed_artifacts(db_settings, seeded)
    try:
        provider = RecordingProvider({"summary": "s", "findings": []})
        artifacts = [{"id": item["id"], "task_key": "scene", "artifact_type": "candidate", "preview": "预览"} for item in seeded]

        await _run_reviewer("continuity_reviewer", _review_context(provider, factory, run_id, artifacts))

        request = provider.requests[0]
        # 旧实现：每 artifact 各封顶 2000 字符，4 份合计 8000+ 字符 → 估计超 4000 token
        assert _prompt_estimate(request) <= INPUT_BUDGET
        # 每个被审 artifact 仍出现在 prompt 中（裁剪而非丢弃），全文按中段省略收缩
        assert all(item["id"] in request["user_prompt"] for item in artifacts)
        assert "中段省略" in request["user_prompt"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reviewer_trims_oversized_scene_pack(db_settings):
    """超大场景包：按 default handler 同款 trim_scene_pack 裁剪并记 context.trimmed 事件。"""
    engine, factory, run_id = await _seed_artifacts(db_settings, [{"id": "art-1", "payload": {"title": "稿", "content": "正文" * 100}}])
    try:
        provider = RecordingProvider({"summary": "s", "findings": []})
        artifacts = [{"id": "art-1", "task_key": "scene", "artifact_type": "candidate", "preview": "预览"}]
        pack = {"worldview": "世" * 20000, "evidence": "证" * 20000}

        result = await _run_reviewer("continuity_reviewer", _review_context(provider, factory, run_id, artifacts, scene_pack=pack))

        request = provider.requests[0]
        # trim_scene_pack 与评审 prompt 估计口径一致（chars//2），留 5% 余量防边界抖动
        assert _prompt_estimate(request) <= int(INPUT_BUDGET * 1.05)
        trimmed = [event for event in result.extra_events if event.get("event") == "context.trimmed"]
        assert trimmed and trimmed[0]["kinds"] == ["scene_pack"]
        assert trimmed[0]["input_budget"] == INPUT_BUDGET
        assert "世" * 20000 not in request["user_prompt"]  # 场景包被裁剪而非全文注入
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rewrite_trims_scene_and_merge_list_to_budget():
    """改写 handler：3 万字符正文 + 超大四桶清单按预算中段裁剪，首尾保留。"""
    provider = RecordingProvider({"title": "终稿", "content": "改写后正文"})
    context: dict[str, object] = {
        "task": {"id": "t1", "role": "chief_editor", "task_key": "rewrite"},
        "run": {"id": "r1", "goal": "写第三章", "goal_hash": "h"},
        "provider": provider,
        "provider_id": "fake",
        "model": "fake-model",
        "uow_factory": None,  # _rewrite_final_draft 不触碰数据库
        "artifacts": [],
        "input_budget": INPUT_BUDGET,
    }
    scene = {"id": "s1", "title": "雨夜回城", "content": "头" * 100 + "身" * 29800 + "尾" * 100}
    merge_payload = {
        "summary": "s",
        "agreements": [{"review_id": f"r{i}", "claims": [{"finding": "发现" * 100, "severity": "low"}]} for i in range(30)],
        "conflicts": [],
        "unsupported": [],
        "accepted": [],
    }

    result = await _rewrite_final_draft(context, scene, merge_payload)

    request = provider.requests[0]
    prompt = request["user_prompt"]
    # 旧实现：正文+清单全文直拼，本例约 37000 字符 → 估计超 18000 token
    assert _prompt_estimate(request) <= INPUT_BUDGET
    assert "中段省略" in prompt
    assert "头" * 100 in prompt and "尾" * 100 in prompt  # elide_middle：首 70% / 尾 20% 保留
    assert "身" * 5000 not in prompt
    assert result.payload["content"] == "改写后正文"
    assert result.payload["rewrite_of"] == "s1"

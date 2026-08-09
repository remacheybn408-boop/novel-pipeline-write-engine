"""评审合议（review_council，约翰逊协作化）宿主可跑测试。

sqlite+aiosqlite 真实落库 + 假 provider（无网络、无 PG），种子模式复制自
tests/agents/test_agent_executor.py。覆盖：
- 合议裁定落库：conflict_group 胜方 resolution=accepted / 负方 rejected
  （兼容现有用户审批语义），council.committed 事件 + candidate artifact；
- 定点改写消费合议产出：pinpoint 提示词带合议指令、不带被裁定驳回的原始主张；
- 整章改写消费合议产出：合议指令不可定位时回退整章路径，提示词用
  「评审合议裁定清单」而非四桶 JSON；
- 合议调用失败降级：确定性去重兜底 + council.fallback 事件，冲突保持未裁定。
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from proseforge.domain.common.ids import new_id
from proseforge.domain.ports.model_provider import GenerationEvent
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.agents import (
    AgentArtifactModel,
    AgentEventModel,
    AgentReviewModel,
    AgentRunModel,
    AgentTaskModel,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.security.credential_cipher import CredentialCipher
from proseforge.settings import Settings, get_settings
from proseforge.workflows.agent_executor import execute_run

MASTER_KEY = base64.b64encode(b"k" * 32).decode()

# 全唯一 CJK 字符序列：复读检测（n-gram 重复）不触发，字数稳定达标。
_BASE = "".join(chr(0x4E00 + index) for index in range(4000))
_MARKER = "城门已闭，守卫盘问姓名。"
SCENE_PARAGRAPHS = [
    _BASE[:1500],
    _BASE[1500:2000] + _MARKER + _BASE[2000:2800],
    _BASE[2800:4000],
]
SCENE_CONTENT = "\n\n".join(SCENE_PARAGRAPHS)  # 3023 字，改写后仍 >= 2500
FINAL_CONTENT = "".join(chr(0x4E00 + index) for index in range(3000, 6000))  # 整章改写终稿

_CONFLICT_QUOTE = "城门已闭，守卫盘问姓名"
_REVIEW_CONTINUITY = {
    "summary": "连续性发现两处问题",
    "findings": [
        {
            "finding": "时间线矛盾：雨夜城门已闭",
            "severity": "high",
            "evidence_spans": [{"artifact_id": "", "start": 0, "end": 11, "quote": _CONFLICT_QUOTE}],
        },
        {
            "finding": "道具状态漂移：青铜钥匙前文已遗失",
            "severity": "high",
            "evidence_spans": [{"artifact_id": "", "start": 0, "end": 4, "quote": "青铜钥匙"}],
        },
    ],
}
_REVIEW_ADVERSARIAL = {
    "summary": "对抗评审不认可",
    "findings": [
        {
            "finding": "此处逻辑自洽无需修改",
            "severity": "low",
            "evidence_spans": [{"artifact_id": "", "start": 0, "end": 11, "quote": _CONFLICT_QUOTE}],
        },
    ],
}
_CLEAN_REVIEW = {"summary": "干净", "findings": []}

COUNCIL_INSTRUCTION = "按合议裁定改写城门段落，使其与前文时间线一致"

# 评审合议图：scene -> 三评审 -> review_council -> merge -> rewrite -> recheck
COUNCIL_GRAPH = [
    {"id": "scene", "role": "scene_writer", "token_budget": 5},
    {"id": "review_continuity", "role": "continuity_reviewer", "depends_on": ["scene"], "token_budget": 5},
    {"id": "review_adversarial", "role": "adversarial_reviewer", "depends_on": ["scene"], "token_budget": 5},
    {"id": "review_style", "role": "style_editor", "depends_on": ["scene"], "token_budget": 5},
    {"id": "review_council", "role": "merge_editor", "depends_on": ["review_continuity", "review_adversarial", "review_style"], "token_budget": 5},
    {"id": "merge", "role": "merge_editor", "depends_on": ["review_council"], "token_budget": 5},
    {"id": "rewrite", "role": "chief_editor", "depends_on": ["merge"], "token_budget": 5},
    {"id": "recheck", "role": "continuity_reviewer", "depends_on": ["rewrite"], "token_budget": 5},
]


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


class CouncilProvider:
    """按 task_key 定制输出的假 provider；review_council 的输出从用户提示词里
    提取真实 conflict_group slug（证明冲突组确实送进了合议），无法提取时
    （``council_mode == "broken"``）输出非法 JSON 触发确定性兜底。"""

    provider_id = "fake"

    def __init__(self, payloads: dict[str, object], *, council_mode: str = "resolve", usage: tuple[int, int] = (4, 2)):
        self._payloads = payloads
        self._council_mode = council_mode
        self._input, self._output = usage
        self.requests: list[dict[str, str]] = []

    async def stream(self, request):
        task_key = str(request.metadata.get("task_key", ""))
        user_prompt = "\n".join(str(block.get("text", "")) for block in request.input_blocks)
        self.requests.append({"task_key": task_key, "user_prompt": user_prompt})
        if task_key == "review_council":
            if self._council_mode == "broken":
                text = "not json at all"
            else:
                match = re.search(r"cg-[0-9a-f]{12}", user_prompt)
                assert match is not None, "council prompt must carry the conflict group slug"
                # relocate 模式：findings 与指令的引文都不在正文里，pinpoint 无法定位。
                evidence_quote = _CONFLICT_QUOTE if self._council_mode == "resolve" else "正文里不存在的引文片段"
                text = json.dumps({
                    "summary": "合议：1 个冲突组已裁定",
                    "findings": [
                        {"finding": "时间线矛盾：雨夜城门已闭", "severity": "high", "source": ["continuity_reviewer"], "evidence": [evidence_quote]},
                    ],
                    "rulings": [
                        {"conflict_group": match.group(0), "winner_role": "continuity_reviewer", "resolution": "采纳连续性评审意见", "reason": "引文显示城门已闭与前文矛盾"},
                    ],
                    "rewrite_instructions": [
                        {"finding": "时间线矛盾：雨夜城门已闭", "severity": "high", "instruction": COUNCIL_INSTRUCTION, "evidence": [evidence_quote]},
                    ],
                }, ensure_ascii=False)
        else:
            payload = self._payloads.get(task_key, {"summary": "ok"})
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


def _patch_provider(monkeypatch, provider: CouncilProvider) -> None:
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: provider)


async def _seed_run(settings: Settings) -> dict[str, str]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"council-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            credential_id = f"cred-{uuid.uuid4().hex[:8]}"
            associated = f"{user.id}:openai:{credential_id}".encode()
            encrypted = CredentialCipher(base64.b64decode(MASTER_KEY)).encrypt(json.dumps({"api_key": "sk-test"}).encode(), associated_data=associated)
            await uow.credentials.create(user.id, "openai", base64.b64encode(encrypted).decode(), record_id=credential_id)
            now = datetime.now(UTC)
            uow.session.add(ProjectModel(id="project-1", owner_id=user.id, slug="project-1", title="Council Test Project"))
            await uow.session.flush()
            run = AgentRunModel(
                id=new_id(), user_id=user.id, project_id="project-1", goal="改写第三章",
                goal_hash="g" * 64, graph_revision=1, status="PENDING", budget_limit=100000,
                created_at=now, updated_at=now,
            )
            uow.session.add(run)
            await uow.session.flush()
            for item in COUNCIL_GRAPH:
                uow.session.add(AgentTaskModel(
                    id=new_id(), run_id=run.id, task_key=str(item["id"]), role=str(item["role"]),
                    status="PENDING", token_budget=int(item.get("token_budget", 1)),
                    depends_on=json.dumps(item.get("depends_on", [])),
                ))
            await uow.commit()
            return {"run_id": run.id, "user_id": user.id}
    finally:
        await engine.dispose()


async def _read_state(settings: Settings, run_id: str):
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            tasks = [
                {key: getattr(task, key) for key in ("task_key", "status", "last_error")}
                for task in await uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run_id))
            ]
            events = [
                {"event_type": event.event_type, "payload": json.loads(event.payload)}
                for event in await uow.session.scalars(select(AgentEventModel).where(AgentEventModel.run_id == run_id).order_by(AgentEventModel.sequence))
            ]
            artifacts = []
            for artifact in await uow.session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.run_id == run_id)):
                task_key = ""
                if artifact.task_id:
                    task_row = await uow.session.get(AgentTaskModel, artifact.task_id)
                    task_key = task_row.task_key if task_row is not None else ""
                artifacts.append({"task_key": task_key, "payload": json.loads(artifact.payload)})
            reviews = [
                {
                    "reviewer_role": row.reviewer_role,
                    "status": row.status,
                    "conflict_group": row.conflict_group,
                    "resolution": (json.loads(row.payload or "{}") or {}).get("resolution"),
                }
                for row in await uow.session.scalars(select(AgentReviewModel).where(AgentReviewModel.run_id == run_id))
            ]
            return {"status": run.status, "tasks": tasks, "events": events, "artifacts": artifacts, "reviews": reviews}
    finally:
        await engine.dispose()


def _request_for(provider: CouncilProvider, task_key: str) -> str:
    return next(request["user_prompt"] for request in provider.requests if request["task_key"] == task_key)


@pytest.mark.asyncio
async def test_council_rulings_persisted_and_pinpoint_consumes_council(executor_settings, monkeypatch):
    provider = CouncilProvider(
        {
            "scene": {"title": "回城", "content": SCENE_CONTENT},
            "review_continuity": _REVIEW_CONTINUITY,
            "review_adversarial": _REVIEW_ADVERSARIAL,
            "review_style": _CLEAN_REVIEW,
            "rewrite": {"title": "回城（终稿）", "rewrites": [{"index": 1, "content": "城门大开，守卫列队相迎。"}]},
            "recheck": _CLEAN_REVIEW,
        },
        council_mode="resolve",
    )
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings)

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    state = await _read_state(executor_settings, seeded["run_id"])
    assert state["status"] == "COMPLETED"
    status_by_key = {task["task_key"]: task["status"] for task in state["tasks"]}
    assert all(status == "SUCCEEDED" for status in status_by_key.values()), status_by_key

    # 合议 artifact 落库：去重 findings + 裁定 + 排序改写指令，mode=council
    council = next(artifact["payload"] for artifact in state["artifacts"] if artifact["task_key"] == "review_council")
    assert council["mode"] == "council"
    assert len(council["rulings"]) == 1
    assert council["rulings"][0]["winner_role"] == "continuity_reviewer"
    assert council["rewrite_instructions"][0]["instruction"] == COUNCIL_INSTRUCTION
    assert council["rewrite_instructions"][0]["evidence"] == [_CONFLICT_QUOTE]
    assert council["sources"]  # 评审行 id 清单（可审计）
    committed = next(event["payload"] for event in state["events"] if event["event_type"] == "council.committed")
    assert committed["mode"] == "council" and committed["rulings"] == 1
    assert committed["ruled_groups"] == [council["rulings"][0]["conflict_group"]]

    # 裁定写回评审行：胜方 accepted / 负方 rejected（兼容用户审批语义），
    # 不再计未裁定冲突（chief proposal guard 语义据此放行）。
    # 只取冲突组内的行（recheck 也是 continuity_reviewer 角色，避免键覆盖）。
    conflicted = {row["reviewer_role"]: row for row in state["reviews"] if row["conflict_group"]}
    assert conflicted["continuity_reviewer"]["resolution"] == "accepted"
    assert conflicted["adversarial_reviewer"]["resolution"] == "rejected"
    assert conflicted["continuity_reviewer"]["conflict_group"] == council["rulings"][0]["conflict_group"]

    # 定点改写消费合议产出：prompt 带合议指令，不带被裁定驳回的原始主张
    rewrite_prompt = _request_for(provider, "rewrite")
    assert "本段审校标注" in rewrite_prompt  # 走的是 pinpoint 定点路径
    assert COUNCIL_INSTRUCTION in rewrite_prompt
    assert "此处逻辑自洽无需修改" not in rewrite_prompt
    rewritten = next(artifact["payload"] for artifact in state["artifacts"] if artifact["task_key"] == "rewrite")
    assert rewritten["pinpoint"]["mode"] == "pinpoint"
    paragraphs = str(rewritten["content"]).split("\n\n")
    assert paragraphs[0] == SCENE_PARAGRAPHS[0]  # 未标注段字节级不变
    assert paragraphs[1] == "城门大开，守卫列队相迎。"
    assert paragraphs[2] == SCENE_PARAGRAPHS[2]


@pytest.mark.asyncio
async def test_full_rewrite_consumes_council_directives(executor_settings, monkeypatch):
    """合议指令引文不可定位 -> 回退整章改写，prompt 用合议裁定清单而非四桶。"""
    provider = CouncilProvider(
        {
            "scene": {"title": "回城", "content": SCENE_CONTENT},
            "review_continuity": _REVIEW_CONTINUITY,
            "review_adversarial": _REVIEW_ADVERSARIAL,
            "review_style": _CLEAN_REVIEW,
            "rewrite": {"title": "回城（终稿）", "content": FINAL_CONTENT},
            "recheck": _CLEAN_REVIEW,
        },
        council_mode="relocate",  # 指令引文不在正文里 -> pinpoint 无法定位
    )
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings)

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    state = await _read_state(executor_settings, seeded["run_id"])
    rewrite_prompt = _request_for(provider, "rewrite")
    assert "评审合议裁定清单" in rewrite_prompt
    assert "rewrite_instructions" in rewrite_prompt
    assert COUNCIL_INSTRUCTION in rewrite_prompt
    assert "四桶分类" not in rewrite_prompt
    rewritten = next(artifact["payload"] for artifact in state["artifacts"] if artifact["task_key"] == "rewrite")
    assert rewritten["content"] == FINAL_CONTENT
    assert rewritten.get("rewrite_of")


@pytest.mark.asyncio
async def test_council_fallback_keeps_pipeline_alive(executor_settings, monkeypatch):
    """合议模型输出不可用 -> 确定性去重兜底：council.fallback 事件 + 冲突保持未裁定。"""
    provider = CouncilProvider(
        {
            "scene": {"title": "回城", "content": SCENE_CONTENT},
            "review_continuity": _REVIEW_CONTINUITY,
            "review_adversarial": _REVIEW_ADVERSARIAL,
            "review_style": _CLEAN_REVIEW,
            "rewrite": {"title": "回城（终稿）", "content": FINAL_CONTENT},
            "recheck": _CLEAN_REVIEW,
        },
        council_mode="broken",
    )
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings)

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    state = await _read_state(executor_settings, seeded["run_id"])
    council = next(artifact["payload"] for artifact in state["artifacts"] if artifact["task_key"] == "review_council")
    assert council["mode"] == "fallback"
    assert council["rulings"] == []
    # 兜底去重：同一引文的两份评审合并为一条，source 记全双方
    merged = next(finding for finding in council["findings"] if _CONFLICT_QUOTE in finding["evidence"])
    assert sorted(merged["source"]) == ["adversarial_reviewer", "continuity_reviewer"]
    assert any(event["event_type"] == "council.fallback" for event in state["events"])
    # 冲突保持未裁定（resolution 仍是 None），管线不因兜底伪造裁定
    assert all(row["resolution"] is None for row in state["reviews"] if row["conflict_group"])

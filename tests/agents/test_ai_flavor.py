"""AI 味治理两件套测试（ai_flavor.py + 写作/审校两侧接入）。

单元测试覆盖三条确定性规则的正反用例；集成测试用 sqlite+aiosqlite
真实落库 + FakeProvider 假模型（种子模式复制自 test_review_handlers.py），
验证 continuity_reviewer 报告 issues 合并 ai_flavor 条目且结构符合
quality_gate 消费格式（severity=high + 非空 evidence_spans）。
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime

import pytest

from proseforge.application.agents.ai_flavor import (
    AI_CLICHE_TERMS,
    WRITING_STYLE_RULES,
    detect_ai_flavor,
)
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


# --- 规则 1：套话命中密度 ---


def _cliche_paragraph(terms: list[str]) -> str:
    """把套话均匀嵌进正常叙述（每条独立成段，密度远超 3 处/千字阈值）。"""
    filler = "他沿着河堤走了很久，路灯把影子拉得很长，脚步声在空街上回响。"
    return "\n".join(f"{filler}{term}，{filler}" for term in terms)


def test_cliche_density_flags_issue_with_locations():
    terms = ["他突然意识到", "空气仿佛凝固", "嘴角勾起一抹", "眼中闪过一丝", "心跳漏了一拍"]
    text = _cliche_paragraph(terms)  # 5 处套话，绝对数与密度均超阈值
    issues = detect_ai_flavor(text)
    cliche = next(issue for issue in issues if issue["rule"] == "cliche_terms")
    assert cliche["type"] == "ai_flavor"
    evidence = cliche["evidence"]
    assert len(evidence) == 5
    for span in evidence:
        # 定位正确：quote 与起止偏移必须对应原文该位置的子串
        assert text[span["start"]:span["end"]] == span["quote"]
    assert {span["quote"] for span in evidence} == set(terms)


def test_clean_text_has_no_issues():
    paragraphs = [
        "码头的灯坏了一半，老周摸着黑把缆绳绕上桩子。",
        "水里漂着碎冰，撞在船壳上，一声一声。",
        "他没回头，知道是谁站在堤上。",
        "烟抽完了，他把烟头摁灭在鞋底。",
        "货单压在工具箱底下，边角已经卷了。",
        "远处有狗叫了两声，又没了动静。",
    ]
    assert detect_ai_flavor("\n".join(paragraphs)) == []


def test_single_cliche_hit_below_threshold_not_flagged():
    # 绝对命中数不足 3 时短文本不做密度放大（避免单次命中误报）
    text = "他深吸一口气，推开了门。" + "屋里很暗，只有桌上的台灯亮着。" * 10
    assert all(issue["rule"] != "cliche_terms" for issue in detect_ai_flavor(text))


# --- 规则 2：段首重复率 ---


def test_paragraph_start_repeat_flagged():
    # 6 段中 4 段首 4 字同为「他走到窗」→ 重复率 (6-3)/6 = 50% > 40%
    paragraphs = [
        "他走到窗前，掀开帘子看了一眼。",
        "他走到窗台，把信封压平。",
        "他走到窗边，又停住了。",
        "他走到窗口，灯刚好灭了。",
        "雨下大了，砸在铁皮棚上。",
        "屋里只剩下钟摆的声音。",
    ]
    issues = detect_ai_flavor("\n".join(paragraphs))
    repeat = next(issue for issue in issues if issue["rule"] == "paragraph_start_repeat")
    assert repeat["type"] == "ai_flavor"
    assert len(repeat["evidence"]) == 4
    assert all(span["quote"] == "他走到窗" for span in repeat["evidence"])


def test_paragraph_start_varied_not_flagged():
    paragraphs = [
        "码头的灯坏了一半。",
        "水里漂着碎冰。",
        "他没回头。",
        "烟抽完了。",
        "货单压在工具箱底下。",
        "远处有狗叫了两声。",
    ]
    assert all(issue["rule"] != "paragraph_start_repeat" for issue in detect_ai_flavor("\n".join(paragraphs)))


def test_paragraph_start_repeat_needs_min_paragraphs():
    # 同开头（重复率 75%）但不足 5 段 → 样本太小不判定
    paragraphs = ["他走到窗前。", "他走到窗台。", "他走到窗边。", "他走到窗口。"]
    assert all(issue["rule"] != "paragraph_start_repeat" for issue in detect_ai_flavor("\n".join(paragraphs)))


# --- 规则 3：段尾模式化 ---


def test_paragraph_ending_pattern_flagged():
    paragraphs = [
        "他把信烧了，火苗舔着纸角，一切都仿佛回到了起点了。",
        "门外的脚步声远了，屋子里静得似乎能听见灰尘落下了。",
        "他坐回椅子上，盯着天花板，原来答案一直在那里了。",
        "雨停了，瓦檐还在滴水。",
    ]
    issues = detect_ai_flavor("\n".join(paragraphs))
    ending = next(issue for issue in issues if issue["rule"] == "paragraph_ending_pattern")
    assert ending["type"] == "ai_flavor"
    assert len(ending["evidence"]) == 3  # 连续 3 段总结式收尾


def test_paragraph_ending_scattered_not_flagged():
    # 有总结式段尾但不连续（中间被正常段打断）→ 不报
    paragraphs = [
        "一切都仿佛回到了起点了。",
        "雨停了，瓦檐还在滴水。",
        "原来答案一直在那里了。",
        "他把烟头摁灭在鞋底。",
        "这一刻他终于松了口气了。",
        "远处有狗叫了两声，又没了动静。",
    ]
    assert all(issue["rule"] != "paragraph_ending_pattern" for issue in detect_ai_flavor("\n".join(paragraphs)))


def test_empty_text_returns_no_issues():
    assert detect_ai_flavor("") == []
    assert detect_ai_flavor("   \n  ") == []


# --- 词表与禁令文本的静态约束 ---


def test_cliche_terms_catalog_size_and_uniqueness():
    assert len(AI_CLICHE_TERMS) >= 20  # 蓝图要求至少 20 条
    assert len(set(AI_CLICHE_TERMS)) == len(AI_CLICHE_TERMS)
    assert all(len(term) >= 4 for term in AI_CLICHE_TERMS)  # 短语级，减少误伤


def test_writing_style_rules_within_token_budget():
    assert len(WRITING_STYLE_RULES) <= 500  # 注入 goal_hint 的体积约束


def test_human_flavor_guide_static_contract():
    """scene_d 人味专令：体积受限 + 覆盖指南核心纪律（蒸馏自
    packs/personas/references/human-flavor-guide.md）。"""
    from proseforge.application.agents.ai_flavor import HUMAN_FLAVOR_GUIDE

    assert len(HUMAN_FLAVOR_GUIDE) <= 800  # 注入 goal_hint 的体积约束
    for keyword in ("情绪不出场", "潜台词", "句长交错", "结尾落在实物", "留白"):
        assert keyword in HUMAN_FLAVOR_GUIDE


# --- 集成：continuity_reviewer 报告合并 ai_flavor 条目 ---


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
    """按 task_key 定制输出的假 provider（复制自 test_review_handlers.py）。"""

    provider_id = "fake"

    def __init__(self, payloads: dict[str, object] | None = None):
        self._payloads = payloads or {}
        self.requests: list[dict[str, str]] = []

    async def stream(self, request):
        self.requests.append(dict(request.metadata))
        payload = self._payloads.get(request.metadata.get("task_key", ""), {"summary": "ok"})
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


async def _seed_run(settings: Settings, tasks: list[dict[str, object]]) -> dict[str, str]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"agents-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            credential_id = f"cred-{uuid.uuid4().hex[:8]}"
            associated = f"{user.id}:openai:{credential_id}".encode()
            encrypted = CredentialCipher(base64.b64decode(MASTER_KEY)).encrypt(json.dumps({"api_key": "sk-test"}).encode(), associated_data=associated)
            await uow.credentials.create(user.id, "openai", base64.b64encode(encrypted).decode(), record_id=credential_id)
            now = datetime.now(UTC)
            uow.session.add(ProjectModel(id="project-1", owner_id=user.id, slug="project-1", title="AI Flavor Test Project"))
            await uow.session.flush()
            run = AgentRunModel(
                id=new_id(), user_id=user.id, project_id="project-1", goal_hash="g" * 64,
                graph_revision=1, status="PENDING", budget_limit=1000,
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
            await uow.commit()
            return {"run_id": run.id, "user_id": user.id}
    finally:
        await engine.dispose()


async def _read_state(settings: Settings, run_id: str):
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            from sqlalchemy import select

            artifacts = [
                {key: getattr(artifact, key) for key in ("id", "artifact_type", "payload")}
                for artifact in await uow.session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.run_id == run_id))
            ]
            reviews = [
                {key: getattr(review, key) for key in ("id", "reviewer_role", "status", "evidence", "payload")}
                for review in await uow.session.scalars(select(AgentReviewModel).where(AgentReviewModel.run_id == run_id))
            ]
            events = [
                {key: getattr(event, key) for key in ("event_type", "payload")}
                for event in await uow.session.scalars(select(AgentEventModel).where(AgentEventModel.run_id == run_id))
            ]
            return artifacts, reviews, events
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_continuity_report_merges_ai_flavor_issues(executor_settings, monkeypatch):
    cliche_content = _cliche_paragraph(["他突然意识到", "空气仿佛凝固", "嘴角勾起一抹", "眼中闪过一丝", "心跳漏了一拍"])
    provider = FakeProvider(payloads={
        "scene": {"title": "回城", "content": cliche_content},
        "review": {"summary": "连续性无异常", "findings": []},
    })
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: provider)
    seeded = await _seed_run(executor_settings, [
        {"id": "scene", "role": "scene_writer", "token_budget": 10},
        {"id": "review", "role": "continuity_reviewer", "depends_on": ["scene"], "token_budget": 10},
    ])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    artifacts, reviews, _events = await _read_state(executor_settings, seeded["run_id"])
    report = next(artifact for artifact in artifacts if json.loads(artifact["payload"]).get("report_type") == "ContinuityReport")
    payload = json.loads(report["payload"])
    ai_issues = [issue for issue in payload["issues"] if issue.get("type") == "ai_flavor"]
    assert ai_issues, "continuity report must merge deterministic ai_flavor issues"
    # quality_gate 消费格式：severity=high + 非空 evidence_spans（quote 对应原文片段）
    from proseforge.application.agents.quality_gate import _findings_with_evidence

    assert _findings_with_evidence([payload]) >= 1
    for issue in ai_issues:
        assert issue["severity"] == "high"
        assert issue["evidence_spans"], issue["finding"]
        for span in issue["evidence_spans"]:
            assert span["quote"]
            assert span["quote"] in cliche_content
    # ai_flavor finding 有证据 → 评审行 WARNING（现有 verdict 逻辑自然生效）
    review = next(row for row in reviews if row["reviewer_role"] == "continuity_reviewer")
    assert review["status"] == "WARNING"
    assert payload["verdict"] == "WARNING"
    claims = json.loads(review["payload"])["claims"]
    assert any("套话" in claim["finding"] for claim in claims)


@pytest.mark.asyncio
async def test_clean_scene_report_has_no_ai_flavor_issues(executor_settings, monkeypatch):
    clean_content = (
        "码头的灯坏了一半，老周摸着黑把缆绳绕上桩子。\n"
        "水里漂着碎冰，撞在船壳上，一声一声。\n"
        "他没回头，知道是谁站在堤上。\n"
        "烟抽完了，他把烟头摁灭在鞋底。\n"
        "货单压在工具箱底下，边角已经卷了。\n"
        "远处有狗叫了两声，又没了动静。"
    )
    provider = FakeProvider(payloads={
        "scene": {"title": "回城", "content": clean_content},
        "review": {"summary": "连续性无异常", "findings": []},
    })
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: provider)
    seeded = await _seed_run(executor_settings, [
        {"id": "scene", "role": "scene_writer", "token_budget": 10},
        {"id": "review", "role": "continuity_reviewer", "depends_on": ["scene"], "token_budget": 10},
    ])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    artifacts, reviews, _events = await _read_state(executor_settings, seeded["run_id"])
    report = next(artifact for artifact in artifacts if json.loads(artifact["payload"]).get("report_type") == "ContinuityReport")
    payload = json.loads(report["payload"])
    assert not [issue for issue in payload["issues"] if issue.get("type") == "ai_flavor"]
    assert payload["verdict"] == "PASS"
    review = next(row for row in reviews if row["reviewer_role"] == "continuity_reviewer")
    assert review["status"] == "PASS"

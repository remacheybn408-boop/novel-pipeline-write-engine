"""ROLE_HANDLERS 注册表契约与 Artifact schema 校验的纯内存测试（无 db、无网络）。"""

from __future__ import annotations

import pytest

from proseforge.application.agents.role_handlers import (
    ARTIFACT_SCHEMAS,
    ARTIFACT_TYPES,
    ROLE_HANDLERS,
    RoleResult,
    allowed_artifact_types,
    default_artifact_type,
    default_role_handler,
    handler_for,
    register_role,
    validate_artifact_payload,
)
from proseforge.domain.ports.model_provider import GenerationEvent


def test_artifact_contract_covers_ten_types():
    assert len(ARTIFACT_TYPES) == 10
    assert set(ARTIFACT_TYPES) == set(ARTIFACT_SCHEMAS)
    assert validate_artifact_payload("SceneDraft", {"title": "t"}) is not None  # 缺 content
    assert validate_artifact_payload("SceneDraft", {"title": "t", "content": "c"}) is None
    assert validate_artifact_payload("candidate", {"anything": 1}) is None  # legacy 类型只要求非空对象
    assert validate_artifact_payload("candidate", {}) is not None
    assert validate_artifact_payload("NoSuchType", {"a": 1}) is not None


def test_role_allowlist_comes_from_domain_policy():
    # roles.py 未改动：普通角色只允许 report/candidate，world_builder 只允许 story_fact
    assert allowed_artifact_types("chief_planner") == frozenset({"report", "candidate"})
    assert allowed_artifact_types("world_builder") == frozenset({"story_fact"})
    assert allowed_artifact_types("no_such_role") == frozenset()
    assert default_artifact_type("chief_planner") == "candidate"
    assert default_artifact_type("world_builder") == "story_fact"


def test_register_role_overrides_default_and_restores():
    # scene_writer 无专家注册（merge_editor 等已由 WS-D 专家模块接管），用于验证默认解析路径
    assert handler_for("scene_writer") is default_role_handler

    async def specialist(_context):
        return RoleResult(artifact_type="candidate", payload={"ok": True})

    saved = ROLE_HANDLERS.get("scene_writer")
    try:
        register_role("scene_writer")(specialist)
        assert handler_for("scene_writer") is specialist
    finally:
        if saved is None:
            ROLE_HANDLERS.pop("scene_writer", None)
        else:
            ROLE_HANDLERS["scene_writer"] = saved
    assert handler_for("scene_writer") is default_role_handler


class _RecordingProvider:
    provider_id = "fake"

    def __init__(self):
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        yield GenerationEvent("content.delta", text='{"summary": "ok"}')
        yield GenerationEvent("response.completed", data={"usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}})

    async def list_models(self):
        return []

    async def validate_credentials(self):
        return {"valid": True}

    async def count_tokens(self, _request):
        return 1


@pytest.mark.asyncio
async def test_default_handler_parses_json_and_reports_usage():
    provider = _RecordingProvider()
    context = {
        "run": {"id": "run-1", "goal_hash": "g" * 64},
        "task": {"id": "task-1", "role": "scene_writer", "task_key": "scene-a"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": [{"artifact_type": "candidate", "task_key": "planner", "preview": "chief_planner candidate"}],
    }

    result = await default_role_handler(context)

    assert result.artifact_type == "candidate"
    assert result.payload == {"summary": "ok"}
    assert (result.input_tokens, result.output_tokens, result.used_tokens) == (7, 3, 10)
    request = provider.requests[0]
    # metadata 带 role/task_key，mock provider 后续可按角色分支
    assert request.metadata["role"] == "scene_writer"
    assert request.metadata["task_key"] == "scene-a"
    assert request.response_schema is not None  # 触发结构化 JSON 输出


@pytest.mark.asyncio
async def test_default_handler_raises_on_non_json_output():
    class GarbageProvider(_RecordingProvider):
        async def stream(self, request):
            yield GenerationEvent("content.delta", text="not json at all")

    import json as _json

    context = {
        "run": {"id": "run-1", "goal_hash": "g" * 64},
        "task": {"id": "task-1", "role": "chief_planner", "task_key": "planner"},
        "provider": GarbageProvider(),
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": [],
    }
    with pytest.raises(_json.JSONDecodeError):
        await default_role_handler(context)


# ---------------------------------------------------------------------------
# analyst handler: full-goal injection with budget-based middle elision
# ---------------------------------------------------------------------------

from proseforge.application.agents.role_handlers import analyst_role_handler


def _analyst_context(provider, goal: str, input_budget: int | None = None):
    context: dict[str, object] = {
        "run": {"id": "run-1", "goal": goal},
        "task": {"id": "task-1", "role": "analyst", "task_key": "analyze"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": [],
    }
    if input_budget is not None:
        context["input_budget"] = input_budget
    return context


def test_analyst_handler_registered():
    assert handler_for("analyst") is analyst_role_handler


@pytest.mark.asyncio
async def test_analyst_injects_short_goal_verbatim():
    provider = _RecordingProvider()
    goal = "第一章 相遇\n第二章 冲突\n第三章 决战"

    result = await analyst_role_handler(_analyst_context(provider, goal, input_budget=100000))

    assert result.artifact_type == "candidate"
    request = provider.requests[0]
    assert goal in request.input_blocks[0]["text"]
    # Analyst speaks its own role prompt, not the default writer one.
    assert "分析 Agent" in request.system_blocks[0]["text"]


@pytest.mark.asyncio
async def test_analyst_elides_long_goal_head70_tail20():
    provider = _RecordingProvider()
    goal = "头" * 100 + "中" * 9800 + "尾" * 100  # 10000 chars
    # input_budget 8000 -> cap = max(2000, 8000 // 2) = 4000 chars
    # (head 2800 + tail 800 + omission marker).
    await analyst_role_handler(_analyst_context(provider, goal, input_budget=8000))

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert goal not in user_text  # the full outline is NOT dumped
    assert "头" * 100 in user_text  # head kept
    assert "尾" * 100 in user_text  # tail kept
    assert "…（中段省略" in user_text
    assert "中" * 4000 not in user_text  # middle dropped


@pytest.mark.asyncio
async def test_default_handler_still_truncates_goal_at_4000():
    # Control: the analyst's full-goal injection must not leak into the
    # default path (GOAL_HINT_MAX_CHARS head-truncation stays).
    provider = _RecordingProvider()
    goal = "第一章 " + "字" * 10000
    context = {
        "run": {"id": "run-1", "goal": goal},
        "task": {"id": "task-1", "role": "scene_writer", "task_key": "scene"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": [],
    }

    await default_role_handler(context)

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert goal not in user_text
    assert goal[:4000] in user_text
    assert "字" * 8000 not in user_text


# ---------------------------------------------------------------------------
# scene_writer 篇幅硬要求：与质量门禁同一阈值，注入 user prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scene_writer_prompt_carries_hard_word_requirement():
    provider = _RecordingProvider()
    context = {
        "run": {"id": "run-1", "goal": "写第3章《风起》\n本章大纲：雨夜接头\n目标字数：不少于 3000 字"},
        "task": {"id": "task-1", "role": "scene_writer", "task_key": "scene"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": [],
    }

    await default_role_handler(context)

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "篇幅硬要求：正文 content 不得少于 3000 字" in user_text


@pytest.mark.asyncio
async def test_scene_writer_word_requirement_defaults_to_gate_floor():
    # goal 无字数信息时与门禁默认阈值一致（2500），属有意为之
    provider = _RecordingProvider()
    context = {
        "run": {"id": "run-1", "goal": "写第3章《风起》"},
        "task": {"id": "task-1", "role": "scene_writer", "task_key": "scene"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": [],
    }

    await default_role_handler(context)

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "篇幅硬要求：正文 content 不得少于 2500 字" in user_text


@pytest.mark.asyncio
async def test_non_writer_roles_have_no_word_requirement():
    provider = _RecordingProvider()
    context = {
        "run": {"id": "run-1", "goal": "写第3章《风起》\n目标字数：不少于 3000 字"},
        "task": {"id": "task-1", "role": "continuity_reviewer", "task_key": "continuity"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": [],
    }

    await default_role_handler(context)

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "篇幅硬要求" not in user_text


# ---------------------------------------------------------------------------
# scene_writer 自我打磨：初稿 -> 第二轮打磨终稿；打磨失败回退初稿
# ---------------------------------------------------------------------------


class _SequentialProvider(_RecordingProvider):
    """按调用顺序弹出输出的假 provider（模拟 初稿 -> 打磨稿）。"""

    def __init__(self, outputs: list[str]):
        super().__init__()
        self._outputs = list(outputs)
        self.calls = 0

    async def stream(self, request):
        self.requests.append(request)
        text = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        yield GenerationEvent("content.delta", text=text)
        yield GenerationEvent("response.completed", data={"usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}})


def _scene_writer_context(provider) -> dict[str, object]:
    return {
        "run": {"id": "run-1", "goal": "写第2章《云涌》\n目标字数：不少于 10 字"},
        "task": {"id": "task-1", "role": "scene_writer", "task_key": "scene"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": [],
    }


@pytest.mark.asyncio
async def test_scene_writer_self_polish_replaces_draft():
    import json as _json

    provider = _SequentialProvider([
        _json.dumps({"title": "初稿", "content": "初稿全文"}, ensure_ascii=False),
        _json.dumps({"title": "终稿", "content": "打磨终稿"}, ensure_ascii=False),
    ])

    result = await default_role_handler(_scene_writer_context(provider))

    assert provider.calls == 2  # 初稿一轮 + 打磨一轮
    assert result.payload["content"] == "打磨终稿"
    assert result.payload["title"] == "终稿"
    polish_prompt = provider.requests[1].input_blocks[0]["text"]
    assert "【你的初稿全文】" in polish_prompt
    assert "初稿全文" in polish_prompt
    assert any(event["event"] == "scene.polished" for event in result.extra_events)
    # 两轮 token 都计入 usage
    assert (result.input_tokens, result.used_tokens) == (14, 20)


@pytest.mark.asyncio
async def test_scene_writer_polish_failure_keeps_draft():
    import json as _json

    provider = _SequentialProvider([
        _json.dumps({"title": "初稿", "content": "初稿全文"}, ensure_ascii=False),
        "not json at all",
    ])

    result = await default_role_handler(_scene_writer_context(provider))

    assert result.payload["content"] == "初稿全文"  # 打磨输出不可用：保留初稿
    assert not any(event["event"] == "scene.polished" for event in result.extra_events)


# ---------------------------------------------------------------------------
# scene_writer：场景衔接卡 / 题材写作指引注入；OutlineCandidate scene_bridge 可选
# ---------------------------------------------------------------------------


class _FakeArtifactRow:
    """AgentArtifactModel 的最小替身：_load_scene_bridge 只读 id 与 payload。"""

    def __init__(self, artifact_id: str, payload: str):
        self.id = artifact_id
        self.payload = payload


class _FakeUow:
    """纯内存 uow：session.get 按 id 返回预置行（无 DB、无网络）。"""

    def __init__(self, row: _FakeArtifactRow | None):
        self.session = self
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, _model, ident):
        return self._row if self._row is not None and self._row.id == ident else None


def _scene_bridge_context(provider, planner_payload: dict | None) -> dict[str, object]:
    import json as _json

    row = _FakeArtifactRow("art-planner", _json.dumps(planner_payload, ensure_ascii=False)) if planner_payload is not None else None
    artifacts = [{"id": "art-planner", "artifact_type": "candidate", "task_key": "planner", "preview": "大纲候选"}] if row else []
    return {
        "run": {"id": "run-1", "goal": "写第2章《云涌》\n目标字数：不少于 10 字"},
        "task": {"id": "task-1", "role": "scene_writer", "task_key": "scene_a"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": artifacts,
        "uow_factory": lambda: _FakeUow(row),
    }


@pytest.mark.asyncio
async def test_scene_writer_injects_scene_bridge_card():
    provider = _RecordingProvider()
    payload = {
        "title": "书",
        "chapters": [{"title": "第二章", "summary": "雨夜接头"}],
        "scene_bridge": [
            {"time_anchor": "当夜子时", "space_anchor": "自城东客栈冒雨而至", "emotion_anchor": "惊疑未定", "pov_anchor": "沈青", "purpose": "接头", "ending_hook": "门环响了"},
            {"time_anchor": "半个时辰后", "pov_anchor": "沈青", "purpose": "脱身"},
        ],
    }

    await default_role_handler(_scene_bridge_context(provider, payload))

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "【场景衔接卡】" in user_text
    assert "当夜子时" in user_text
    assert "视角锚=沈青" in user_text
    assert "场景2" in user_text  # 缺字段的场景只渲染已有锚点


@pytest.mark.asyncio
async def test_scene_writer_without_scene_bridge_stays_quiet():
    # 旧格式大纲候选（无 scene_bridge）：不注入衔接卡，不报错
    provider = _RecordingProvider()
    payload = {"title": "书", "chapters": [{"title": "第二章", "summary": "雨夜接头"}]}

    result = await default_role_handler(_scene_bridge_context(provider, payload))

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "【场景衔接卡】" not in user_text
    assert result.payload == {"summary": "ok"}


@pytest.mark.asyncio
async def test_scene_writer_injects_genre_guidance():
    # goal 的「题材：言情」行映射到 packs/skills/romance-fiction-writing
    provider = _RecordingProvider()
    context = {
        "run": {"id": "run-1", "goal": "写第2章《云涌》\n目标字数：不少于 10 字\n题材：言情"},
        "task": {"id": "task-1", "role": "scene_writer", "task_key": "scene_a"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": [],
    }

    await default_role_handler(context)

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "【题材写作指引】" in user_text


@pytest.mark.asyncio
async def test_scene_writer_unmapped_genre_injects_nothing():
    provider = _RecordingProvider()
    context = {
        "run": {"id": "run-1", "goal": "写第2章《云涌》\n目标字数：不少于 10 字\n题材：菜谱"},
        "task": {"id": "task-1", "role": "scene_writer", "task_key": "scene_a"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": [],
    }

    result = await default_role_handler(context)

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "【题材写作指引】" not in user_text
    assert result.payload == {"summary": "ok"}


def test_outline_candidate_scene_bridge_is_optional():
    # scene_bridge 是可选字段：旧大纲候选（无此字段）校验照过，新字段也不破坏 schema
    assert validate_artifact_payload("OutlineCandidate", {"title": "t", "chapters": []}) is None
    assert validate_artifact_payload("OutlineCandidate", {"title": "t", "chapters": [], "scene_bridge": []}) is None


# ---------------------------------------------------------------------------
# scene_d（人味写作席位）：HUMAN_FLAVOR_GUIDE 只注入 scene_d，其余草稿不注入
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scene_d_injects_human_flavor_guide():
    provider = _RecordingProvider()
    context = {
        "run": {"id": "run-1", "goal": "写第2章《云涌》\n目标字数：不少于 10 字"},
        "task": {"id": "task-1", "role": "scene_writer", "task_key": "scene_d"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": [],
    }

    await default_role_handler(context)

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "【人味写作专令】" in user_text
    assert "情绪不出场" in user_text
    # 通用反 AI 腔禁令对所有草稿照常在场。
    assert "【反 AI 腔写作规范】" in user_text


@pytest.mark.asyncio
async def test_other_scene_drafts_skip_human_flavor_guide():
    provider = _RecordingProvider()
    context = {
        "run": {"id": "run-1", "goal": "写第2章《云涌》\n目标字数：不少于 10 字"},
        "task": {"id": "task-1", "role": "scene_writer", "task_key": "scene_a"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": [],
    }

    await default_role_handler(context)

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "【人味写作专令】" not in user_text
    assert "【反 AI 腔写作规范】" in user_text


def test_render_scene_bridge_caps_at_600_chars():
    from proseforge.application.agents.role_handlers import (
        SCENE_BRIDGE_MAX_CHARS,
        render_scene_bridge,
    )

    long_bridge = [{"time_anchor": "时" * 500, "purpose": "事" * 500}]
    text = render_scene_bridge(long_bridge)
    assert len(text) == SCENE_BRIDGE_MAX_CHARS
    assert text.endswith("…")
    assert render_scene_bridge(None) == ""
    assert render_scene_bridge({"time_anchor": ""}) == ""  # 全空字段不渲染


# ---------------------------------------------------------------------------
# scene_writer 硬事实卡注入：全书大纲的数字+量词/专名确定性提取，进 goal_hint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scene_writer_goal_hint_carries_hard_fact_card():
    provider = _RecordingProvider()
    context = {
        "run": {
            "id": "run-1",
            "goal": (
                "写第1章 回城\n"
                "全书大纲（仅作全局设定与伏笔参照，本章只写「写第1章」指定的内容）：\n"
                "主角解开七道封印，1997年的雨夜回城。"
            ),
        },
        "task": {"id": "task-1", "role": "scene_writer", "task_key": "scene"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": [],
    }

    await default_role_handler(context)

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "本书硬事实（禁止擅改）" in user_text
    assert "七道封印" in user_text
    assert "1997年" in user_text


@pytest.mark.asyncio
async def test_scene_writer_without_outline_facts_injects_no_card():
    # goal 无全书大纲、无数字事实与专名：不注入硬事实卡，其余契约不变
    provider = _RecordingProvider()
    context = {
        "run": {"id": "run-1", "goal": "写第1章 回城\n本章大纲：雨夜接头"},
        "task": {"id": "task-1", "role": "scene_writer", "task_key": "scene"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": [],
    }

    result = await default_role_handler(context)

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "本书硬事实（禁止擅改）" not in user_text
    assert result.payload == {"summary": "ok"}


# ---------------------------------------------------------------------------
# scene_writer 线索显性化注入：goal 带「伏笔/钩子」行时要求正文用线索原词
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scene_writer_goal_hint_carries_clue_explicit_instruction():
    provider = _RecordingProvider()
    context = {
        "run": {
            "id": "run-1",
            "goal": "写第4章 下山\n伏笔/钩子：埋入大师姐的暗中监视\n目标字数：3000",
        },
        "task": {"id": "task-1", "role": "scene_writer", "task_key": "scene"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": [],
    }

    await default_role_handler(context)

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "【线索显性化】" in user_text
    assert "大师姐的暗中监视" in user_text


@pytest.mark.asyncio
async def test_scene_writer_without_hooks_injects_no_clue_instruction():
    provider = _RecordingProvider()
    context = {
        "run": {"id": "run-1", "goal": "写第1章 回城\n本章大纲：雨夜接头"},
        "task": {"id": "task-1", "role": "scene_writer", "task_key": "scene"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": [],
    }

    await default_role_handler(context)

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "【线索显性化】" not in user_text


# ---------------------------------------------------------------------------
# scene_writer 文风技法卡注入：goal_hint 第 9 块，预算让位链最先被裁
# ---------------------------------------------------------------------------


def _style_context(provider, goal: str, **extra) -> dict[str, object]:
    context: dict[str, object] = {
        "run": {"id": "run-1", "goal": goal},
        "task": {"id": "task-1", "role": "scene_writer", "task_key": "scene_a"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": [],
    }
    context.update(extra)
    return context


@pytest.mark.asyncio
async def test_scene_writer_injects_style_cards():
    # 题材：武侠 → 汪曾祺/阿城技法卡合并摘要进入 goal_hint 末尾
    provider = _RecordingProvider()

    await default_role_handler(_style_context(provider, "写第2章《云涌》\n目标字数：不少于 10 字\n题材：武侠"))

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "【文风技法卡】" in user_text
    assert "汪曾祺" in user_text
    # 注入位置排在题材写作指引之后（第 9 块）
    assert user_text.index("【文风技法卡】") > user_text.index("【题材写作指引】")


@pytest.mark.asyncio
async def test_scene_writer_unmapped_genre_falls_back_to_default_style_cards():
    # 映射不上的题材回退契诃夫/汪曾祺，而非不注入
    provider = _RecordingProvider()

    await default_role_handler(_style_context(provider, "写第2章《云涌》\n目标字数：不少于 10 字\n题材：菜谱"))

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "【文风技法卡】" in user_text
    assert "契诃夫" in user_text


@pytest.mark.asyncio
async def test_scene_writer_style_card_trimmed_first_over_budget():
    # 预算让位链：文风技法卡最先被裁，低于记忆/接缝卡；裁剪审计留有记录
    provider = _RecordingProvider()
    context = _style_context(
        provider,
        "写第2章《云涌》\n目标字数：不少于 10 字\n题材：武侠",
        input_budget=1,  # 必然超预算
    )

    result = await default_role_handler(context)

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "【文风技法卡】" not in user_text
    trimmed = next(event for event in result.extra_events if event["event"] == "context.trimmed")
    assert trimmed["kinds"][0] == "style_card"


@pytest.mark.asyncio
async def test_scene_writer_memoir_genre_end_to_end():
    # memoir 题材端到端：题材包指引 + 史铁生/门罗/马尔克斯技法卡同时注入
    provider = _RecordingProvider()

    await default_role_handler(_style_context(provider, "写第1章 回望\n目标字数：不少于 10 字\n题材：自传"))

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "【题材写作指引】" in user_text
    assert "记忆锚点" in user_text  # memoir-fiction-writing 题材包正文
    assert "【文风技法卡】" in user_text
    assert "史铁生" in user_text

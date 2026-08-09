"""ROLE_HANDLERS 注册表契约（蓝图 V3-004/005，后续 workstream 的挂载点）。

executor（proseforge/workflows/agent_executor.py）只 import 本模块的注册表与
校验函数。专家 handler（评审簇、主编等后续 workstream）在自己的模块里用
``@register_role("continuity_reviewer")`` 注册，**通过模块 import 副作用生效**：
把模块路径加入 ``SPECIALIST_MODULES``，``handler_for`` 首次解析时由
``load_specialists()`` 惰性 import 一次（避免循环 import 与启动成本）。

TaskContext 约定键：
- ``run``: dict——id/goal（原文，可空）/goal_hash/graph_revision/project_id/chapter_id/base_version_id 快照
- ``task``: dict——id/task_key/role/token_budget 快照
- ``provider``: ModelProvider 实例（已按 run owner 凭据构建）
- ``provider_id`` / ``model``: str
- ``uow_factory``: 无参 callable，返回新的 SqlAlchemyUnitOfWork（handler 自持短事务用）
- ``artifacts``: list[dict]——截至认领时已提交 Artifact 的摘要
  （id/task_key/artifact_type/preview；preview 已脱敏限长，不含正文全文）
- ``reasoning``: 弹性解析后的 provider reasoning 载荷（executor 按席位档位
  + 任务类型经 reasoning_policy.resolve_task_reasoning 得出；None = auto）
- ``run`` 快照可选键 ``memory_slice``：用户已批准记忆事实切片
  （[{"fact_key", "value"}]）；缺省时默认 handler 自行经
  ``memory_service.load_memory_slice(uow_factory, run)`` 加载（仅 ACCEPTED）

默认 handler（``default_role_handler``）：按角色提示词调模型 → 解析 JSON →
产出 artifact_type/payload/usage；模型调用发生在任何数据库事务之外。
Artifact 的服务端校验（allowlist + schema）由 executor 在提交时统一执行。
analyst 专用 handler 注册在本模块（``analyst_role_handler``）：管线相同，
但用户提示词注入 run.goal 全文（大纲原文，按 input_budget 中段裁剪）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from proseforge.application.agents.model_json import parse_model_json
from proseforge.domain.agents.roles import CATALOG, AgentRole

TaskContext = dict[str, object]


@dataclass
class RoleResult:
    """角色 handler 的执行结果；executor 据此提交 Artifact 与结算预算。"""

    artifact_type: str
    payload: dict[str, object]
    used_tokens: int = 0
    extra_events: list[dict[str, object]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


RoleHandler = Callable[[TaskContext], Awaitable[RoleResult]]

ROLE_HANDLERS: dict[str, RoleHandler] = {}

# 专家模块路径（后续 workstream 在此登记自己的模块，import 副作用完成注册）
SPECIALIST_MODULES: tuple[str, ...] = (
    "proseforge.application.agents.review_handlers",  # WS-D：评审簇 + merge_editor
    "proseforge.application.agents.chief_handler",  # WS-D：chief_editor（MergeCandidate → V2 proposal）
    "proseforge.application.agents.promise_handlers",  # 奥莉维亚：承诺契约/核对/登记
)

_specialists_loaded = False


def register_role(role: str) -> Callable[[RoleHandler], RoleHandler]:
    """注册某角色的专家 handler（后注册覆盖先注册）。"""

    def decorator(handler: RoleHandler) -> RoleHandler:
        ROLE_HANDLERS[role] = handler
        return handler

    return decorator


def load_specialists() -> None:
    """惰性 import SPECIALIST_MODULES 一次；幂等。"""
    global _specialists_loaded
    if _specialists_loaded:
        return
    _specialists_loaded = True
    import importlib

    for module_path in SPECIALIST_MODULES:
        importlib.import_module(module_path)


def handler_for(role: str) -> RoleHandler:
    """解析角色 handler：专家注册优先，否则用默认通用 handler。"""
    load_specialists()
    return ROLE_HANDLERS.get(role, default_role_handler)


# --- Artifact 类型契约（蓝图 V3-005：10 种类型 + 最小 required-keys 校验） ---

ARTIFACT_TYPES: tuple[str, ...] = (
    "OutlineCandidate",
    "CharacterCard",
    "WorldRuleCandidate",
    "TimelineReport",
    "SceneDraft",
    "StyleReview",
    "ContinuityReport",
    "AdversarialReport",
    "MergeCandidate",
    "RevisionProposal",
)

ARTIFACT_SCHEMAS: dict[str, tuple[str, ...]] = {
    # OutlineCandidate 的 scene_bridge（场景衔接卡）是可选字段：旧大纲候选
    # 无此字段照常通过校验（向后兼容），下游注入处缺省即不注入。
    "OutlineCandidate": ("title", "chapters"),
    "CharacterCard": ("name", "role", "traits"),
    "WorldRuleCandidate": ("rule", "scope"),
    "TimelineReport": ("events", "issues"),
    "SceneDraft": ("title", "content"),
    "StyleReview": ("summary", "issues"),
    "ContinuityReport": ("summary", "issues"),
    "AdversarialReport": ("summary", "risks"),
    "MergeCandidate": ("summary", "sources"),
    "RevisionProposal": ("summary", "changes"),
}

# 现行 RolePolicy 允许的非类型化 legacy 类型（roles.py 由 policy workstream 演进）
GENERIC_ARTIFACT_TYPES: frozenset[str] = frozenset({"report", "candidate", "story_fact"})


def allowed_artifact_types(role: str) -> frozenset[str]:
    """角色 Artifact 类型 allowlist（domain/agents/roles.py RolePolicy，只读）。"""
    try:
        return CATALOG[AgentRole(role)].artifact_types
    except (KeyError, ValueError):
        return frozenset()


def default_artifact_type(role: str) -> str:
    """默认 handler 的产出类型：优先 candidate，其次 report，再次 allowlist 首项。"""
    allowed = allowed_artifact_types(role)
    for preferred in ("candidate", "report"):
        if preferred in allowed:
            return preferred
    return min(allowed) if allowed else "candidate"


DEFAULT_MAX_OUTPUT_TOKENS = 12000  # 与 RolePolicy.max_tokens 缺省一致

# scene_writer 上一章全文注入：无 input_budget（纯测试 context）时的兜底上限。
PREV_CHAPTER_FALLBACK_MAX_CHARS = 8000


async def _load_previous_chapter_text(context: TaskContext, goal_text: str) -> str:
    """scene_writer 的连贯性基准：当前章之前最近一章的 active 版本全文。

    返回空串（不注入）的情形：无 uow_factory（纯内存测试 context）、无
    project_id、首章（写第 1 章，无更前章节可承接）、无前章、或前章无
    active 版本。章节号取自 goal 的「写第N章」。
    """
    from sqlalchemy import select

    from proseforge.application.agents.review_target import parse_chapter_no
    from proseforge.infrastructure.database.models.chapter import (
        ChapterModel,
        ChapterVersionModel,
    )

    uow_factory = context.get("uow_factory")
    run = context.get("run")
    if uow_factory is None or not isinstance(run, dict):
        return ""
    project_id = str(run.get("project_id", "") or "")
    if not project_id:
        return ""
    current_no = parse_chapter_no(goal_text)
    async with uow_factory() as uow:  # type: ignore[operator]
        rows = await uow.session.scalars(
            select(ChapterModel)
            .where(ChapterModel.project_id == project_id, ChapterModel.active_version_id.isnot(None))
            .order_by(ChapterModel.chapter_no.desc())
        )
        chapter = next((row for row in rows if current_no is None or row.chapter_no < current_no), None)
        if chapter is None:
            return ""
        version = await uow.session.get(ChapterVersionModel, chapter.active_version_id)
        if version is None or not str(version.content or "").strip():
            return ""
        chapter_no = chapter.chapter_no
        title = str(chapter.title or "")
        content = str(version.content).strip()
    return f"第{chapter_no}章《{title}》：\n{content}"


SCENE_BRIDGE_MAX_CHARS = 600  # 场景衔接卡注入上限（超出截断）

# scene_bridge 字段 → 衔接卡渲染标签（契约见 prompts.ROLE_OUTPUT_HINTS["chief_planner"]）
_SCENE_BRIDGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("time_anchor", "时间锚"),
    ("space_anchor", "空间锚"),
    ("emotion_anchor", "情绪锚"),
    ("pov_anchor", "视角锚"),
    ("purpose", "任务"),
    ("ending_hook", "接力句"),
)


def render_scene_bridge(scene_bridge: object, *, max_chars: int = SCENE_BRIDGE_MAX_CHARS) -> str:
    """planner 产出的 scene_bridge（场景衔接规划）→ 注入用紧凑文本；空/非法返回 ""。

    兼容单场景 dict 与场景 list；缺字段的条目只渲染已有的锚点。
    """
    if isinstance(scene_bridge, dict):
        scenes: list[object] = [scene_bridge]
    elif isinstance(scene_bridge, list):
        scenes = scene_bridge
    else:
        return ""
    lines: list[str] = []
    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            continue
        parts = [f"{label}={value}" for key, label in _SCENE_BRIDGE_FIELDS if (value := str(scene.get(key) or "").strip())]
        if parts:
            lines.append(f"场景{index}：" + "｜".join(parts))
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


async def _load_scene_bridge(context: TaskContext) -> str:
    """读 planner Artifact 的 scene_bridge 字段（可选，缺失不报错），渲染为衔接卡文本。

    返回空串（不注入）的情形：无 uow_factory（纯内存测试 context）、上游无
    planner Artifact、payload 非 JSON 或无 scene_bridge 字段（旧大纲候选向后兼容）。
    """
    import json as _json

    from proseforge.infrastructure.database.models.agents import AgentArtifactModel

    uow_factory = context.get("uow_factory")
    if uow_factory is None:
        return ""
    planner_ids = [
        str(item.get("id", ""))
        for item in context.get("artifacts", [])
        if isinstance(item, dict) and str(item.get("task_key", "")) == "planner" and item.get("id")
    ]
    if not planner_ids:
        return ""
    async with uow_factory() as uow:  # type: ignore[operator]
        row = await uow.session.get(AgentArtifactModel, planner_ids[0])
        raw_payload = row.payload if row is not None else None  # 会话内快照，退出后 ORM 实例过期
    if raw_payload is None:
        return ""
    try:
        payload = _json.loads(raw_payload)
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return render_scene_bridge(payload.get("scene_bridge"))

# Over-budget reversible compaction: the popped artifact/memory blocks are
# folded into one structured summary block sized to the remaining budget.
COMPACT_MIN_CHARS = 60  # 剩余预算低于此不尝试压缩，保持硬裁剪
COMPACT_SLACK_CHARS = 8  # chars//2 估算的取整余量


def _compact_dropped_blocks(blocks: list[dict[str, object]], *, max_chars: int) -> tuple[str, dict[str, object]] | None:
    """Fold over-budget dropped blocks into one validated summary block.

    Uses context_engine reversible compaction (dedup + validate_summary);
    returns None — the caller keeps the hard trim — when there is not
    enough room or the summary fails validation.
    """
    from proseforge.context_engine.compaction import compact_reversibly

    if max_chars < COMPACT_MIN_CHARS:
        return None
    summary = {
        "facts": [str(block.get("text") or "") for block in blocks if block.get("text")],
        "decisions": [], "constraints": [], "characters": [], "timeline": [],
        "open_questions": [], "unresolved_plot_threads": [], "style_requirements": [],
        "source_message_ids": sorted({str(block.get("id", "")) for block in blocks if block.get("id")}),
    }
    result = compact_reversibly(blocks, summary)
    if result.validation.status != "PASS":
        return None
    text = "[上下文压缩摘要（超预算裁剪块的可逆压缩，原文仍可恢复）]\n" + "\n".join(f"- {fact}" for fact in result.summary["facts"])
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text, {
        "kinds": sorted({str(block.get("kind", "")) for block in blocks}),
        "blocks": len(blocks),
        "summary_chars": len(text),
        "validation": result.validation.status,
    }


def resolve_max_output_tokens(task: dict, role: str) -> int:
    """GenerationRequest.max_output_tokens：任务 token_budget > 1 时按预算封顶；
    token_budget=1 是 GraphTaskRequest 的“未设置”默认值，回退角色 RolePolicy.max_tokens。
    executor 可能附加 max_output_boost（正文任务的思考预算预留，见
    reasoning_policy.resolve_task_reasoning），在基础预算之上叠加。"""
    try:
        budget = int(task.get("token_budget") or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget > 1:
        base = budget
    else:
        try:
            base = CATALOG[AgentRole(role)].max_tokens
        except (KeyError, ValueError):
            base = DEFAULT_MAX_OUTPUT_TOKENS
    try:
        boost = int(task.get("max_output_boost") or 0)
    except (TypeError, ValueError):
        boost = 0
    return base + max(0, boost)


def validate_artifact_payload(artifact_type: str, payload: object) -> str | None:
    """最小 schema 校验：返回 None 通过，否则返回错误原因（不抛异常）。

    10 种类型化 Artifact 校验 required keys；legacy 类型只要求非空 JSON 对象。
    """
    if artifact_type not in ARTIFACT_TYPES and artifact_type not in GENERIC_ARTIFACT_TYPES:
        return f"unknown artifact type: {artifact_type}"
    if not isinstance(payload, dict) or not payload:
        return "artifact payload must be a non-empty JSON object"
    missing = [key for key in ARTIFACT_SCHEMAS.get(artifact_type, ()) if key not in payload]
    if missing:
        return f"artifact payload missing required keys: {','.join(missing)}"
    return None


async def _run_default_handler(context: TaskContext, *, goal_hint: str) -> RoleResult:
    """Shared prompt -> model -> JSON -> RoleResult pipeline.

    ``goal_hint`` is the goal text injected into the user prompt: the
    default handler passes the GOAL_HINT_MAX_CHARS-truncated goal, the
    analyst handler passes the full outline elided against the input budget.
    """
    from proseforge.application.agents.memory_service import load_memory_slice
    from proseforge.application.agents.prompts import build_task_prompt, prompt_for_task
    from proseforge.domain.ports.model_provider import GenerationRequest
    from proseforge.providers.usage import normalize_provider_usage

    task = context["task"]
    run = context["run"]
    assert isinstance(task, dict) and isinstance(run, dict)
    role, task_key = str(task["role"]), str(task["task_key"])
    seam_lagging = False  # 跨章接缝卡：前章 L0 摘要滞后标记（仅 scene_writer 路径置位）
    style_card_injection = ""  # 文风技法卡注入段：预算让位链最先裁（仅 scene_writer 路径赋值）
    if role == "scene_writer":
        # 篇幅硬要求：与质量门禁同一阈值（parse_min_words），写进 goal_hint
        # 末尾，随 build_task_prompt 的每次重组（含预算裁剪重拼）始终在场。
        from proseforge.application.agents.quality_gate import parse_min_words

        goal_hint = (
            f"{goal_hint}\n\n篇幅硬要求：正文 content 不得少于 "
            f"{parse_min_words(goal_hint)} 字，不足即不合格，请充分铺陈后再收尾。"
        )
        # 反 AI 腔禁令清单：与篇幅硬要求同款注入方式——拼进 goal_hint 末尾，
        # 随 build_task_prompt 的每次重组（含预算裁剪重拼）始终在场。
        from proseforge.application.agents.ai_flavor import WRITING_STYLE_RULES

        goal_hint += "\n\n" + WRITING_STYLE_RULES
        # scene_d（人味写作席位）专享：第四路草稿的去 AI 味纪律蒸馏版，
        # 完整版见 packs/personas/references/human-flavor-guide.md。
        if task_key == "scene_d":
            from proseforge.application.agents.ai_flavor import HUMAN_FLAVOR_GUIDE

            goal_hint += "\n\n" + HUMAN_FLAVOR_GUIDE
        # 题材写作指引 + 场景衔接卡：与篇幅硬要求同款注入方式——拼进 goal_hint
        # 末尾，随 build_task_prompt 的每次重组（含预算裁剪重拼）始终在场，
        # 不在预算裁剪链的让位名单里。三块衔接卡（scene_a/b/c）注入同一张卡。
        from proseforge.application.agents.genre_skills import (
            genre_from_goal,
            genre_skill_excerpt,
        )

        genre_excerpt = genre_skill_excerpt(genre_from_goal(str(run.get("goal") or goal_hint)))
        if genre_excerpt:
            goal_hint += "\n\n【题材写作指引】本章题材对应的写作规范，正文以此为准：\n" + genre_excerpt
        bridge_card = await _load_scene_bridge(context)
        if bridge_card:
            goal_hint += (
                "\n\n【场景衔接卡】本章三场景共享的全局衔接规划：开写两段内落地时间锚与空间锚，"
                "场景结尾按接力句留余韵，视角锁定不漂移：\n" + bridge_card
            )
        # 跨章接缝卡：上一章结尾锚点（时间/伏笔/位置/结尾原文），与衔接卡同款
        # 注入方式——拼进 goal_hint 末尾，始终在场。本章开头必须承接接缝。
        from proseforge.application.agents.seam_card import load_seam_card

        seam_card, seam_lagging = await load_seam_card(context, str(run.get("goal") or goal_hint))
        if seam_card:
            goal_hint += "\n\n" + seam_card
        # 硬事实卡：全书大纲里的数量/年代/专名确定性提取，与篇幅硬要求同款
        # 注入方式——拼进 goal_hint 末尾，随 build_task_prompt 重组始终在场。
        # 语料取 run.goal 全文（goal_hint 已按 4000 字截断，全书大纲段在尾部）。
        from proseforge.application.agents.hard_facts import render_hard_fact_card

        fact_card = render_hard_fact_card(str(run.get("goal") or goal_hint))
        if fact_card:
            goal_hint += "\n\n" + fact_card
        # 本章承诺契约卡：promise_contract 节点（奥莉维亚）产出，与硬事实卡
        # 同款注入方式；artifact 缺失/降级时 load 返回空串，不注入不退化。
        from proseforge.application.agents.promise_handlers import (
            load_promise_contract_card,
        )

        contract_card = await load_promise_contract_card(context)
        if contract_card:
            goal_hint += (
                "\n\n【本章承诺契约】以下承诺约束与篇幅硬要求同级：due 条目必须在正文中正面兑现"
                "（写出具体情节），plant 条目必须落实为可感细节：\n" + contract_card
            )
        # 线索显性化：门禁按线索原词做子串/片段覆盖核对，写作若只用同义改写
        # 表达（「监视」写成「盯着」）会被误判未命中。要求显性落笔并用原词。
        from proseforge.application.agents.quality_gate import parse_required_clues

        clues = parse_required_clues(str(run.get("goal") or goal_hint))
        if clues:
            goal_hint += (
                "\n\n【线索显性化】以下必含线索必须在正文中显性落笔（写出具体情节），"
                "并尽量使用线索原词，便于门禁核对：" + "、".join(clues[:8])
            )
        # 文风技法卡：排在上述各块之后，按题材取 2-3 张作家技法卡的合并摘要
        # （≤800 字）作为行文笔法的审美基准；无映射题材回退契诃夫/汪曾祺。
        # 与篇幅硬要求同款注入方式——拼进 goal_hint 末尾；优先级全场最低，
        # 预算让位链里最先被裁（见下方 input_budget 分支），低于记忆/接缝卡。
        from proseforge.application.agents.genre_skills import genre_style_excerpt

        style_excerpt = genre_style_excerpt(genre_from_goal(str(run.get("goal") or goal_hint)))
        if style_excerpt:
            style_card_injection = (
                "\n\n【文风技法卡】本章题材匹配的作家技法摘要，行文笔法以此为审美基准"
                "（迁移手法，不模仿个人句式）：\n" + style_excerpt
            )
            goal_hint += style_card_injection
    provider_id = str(context.get("provider_id", "unknown"))
    provider = context["provider"]
    memory_slice = run.get("memory_slice")
    if memory_slice is None:
        memory_slice = await load_memory_slice(context.get("uow_factory"), run)

    # Scene pack (narrative RAG) + per-role input budget. The executor puts
    # the pack SECTIONS and input_budget into the context; trimming order
    # when over budget: style card injection -> prev chapter text -> scene_pack
    # (retriever trim_scene_pack) -> artifact previews -> memory slice entries,
    # each recorded in a context.trimmed audit event. Popped blocks are not lost: they are folded into one
    # validated reversible-compaction summary block (context.compacted
    # event); a validation failure keeps the hard trim.
    from proseforge.application.work.retriever import render_pack_text, trim_scene_pack

    system_prompt = prompt_for_task(role, task_key)
    artifacts_list = list(context.get("artifacts", []))
    memory_list = list(memory_slice)
    pack_sections = context.get("scene_pack")
    pack_text = render_pack_text(pack_sections) if pack_sections else None
    input_budget = context.get("input_budget")
    prev_chapter_block = ""
    if role == "scene_writer":
        # 连贯性基准：注入上一章 active 版本全文，开写前必须先读完。
        # 预算裁剪时最先让位（trim 顺序在下方 input_budget 分支）。
        from proseforge.application.agents.artifact_texts import elide_middle

        prev_text = await _load_previous_chapter_text(context, str(run.get("goal") or goal_hint))
        if prev_text:
            cap = max(2000, int(input_budget) // 3) if input_budget else PREV_CHAPTER_FALLBACK_MAX_CHARS
            prev_chapter_block = (
                "\n\n【上一章全文·连贯性基准】开写前必须先读完它：承接其情节走向、"
                "人物状态与未回收伏笔，本章开头要与上一章结尾自然衔接。\n"
                + elide_middle(prev_text, cap)
            )
    trimmed_kinds: list[str] = []
    dropped_blocks: list[dict[str, object]] = []
    compact_text = ""
    compact_event: dict[str, object] | None = None

    def _user_prompt() -> str:
        return build_task_prompt(role=role, task_key=task_key, goal_hint=goal_hint, artifacts=artifacts_list, memory_slice=memory_list, scene_pack=pack_text) + prev_chapter_block + compact_text

    user_prompt = _user_prompt()
    if input_budget:
        # chars//2 token estimate (CJK ~1 token/char, same as retriever).
        def _estimate() -> int:
            return (len(system_prompt) + len(user_prompt)) // 2

        if _estimate() > input_budget and style_card_injection:
            # 文风技法卡最先让位：笔法审美基准可丢，连贯性基准/门禁阈值与
            # 上游 Artifact 必须保住。裁掉后重组 user_prompt 再继续让位链。
            dropped_blocks.append({"id": "style_card", "kind": "style_card", "text": style_card_injection[:500]})
            goal_hint = goal_hint[: -len(style_card_injection)]
            style_card_injection = ""
            user_prompt = _user_prompt()
            trimmed_kinds.append("style_card")
        if _estimate() > input_budget and prev_chapter_block:
            # 上一章全文最先让位：连贯性可退化为 scene pack 里的前章摘要，
            # 但 goal、门禁阈值与上游 Artifact 必须保住。
            dropped_blocks.append({"id": "prev_chapter", "kind": "prev_chapter", "text": prev_chapter_block[:500]})
            prev_chapter_block = ""
            user_prompt = _user_prompt()
            trimmed_kinds.append("prev_chapter")
        if _estimate() > input_budget and pack_sections:
            pack_budget = max(200, input_budget - (len(system_prompt) + len(build_task_prompt(role=role, task_key=task_key, goal_hint=goal_hint, artifacts=artifacts_list, memory_slice=memory_list))) // 2)
            pack_text = trim_scene_pack(pack_sections, pack_budget)
            user_prompt = _user_prompt()
            trimmed_kinds.append("scene_pack")
        while _estimate() > input_budget and artifacts_list:
            dropped = artifacts_list.pop()
            dropped_blocks.append({"id": str(dropped.get("id", "")), "kind": "artifacts", "text": f"[{dropped.get('artifact_type', '')}] {dropped.get('task_key', '')}: {dropped.get('preview', '')}"})
            user_prompt = _user_prompt()
            if "artifacts" not in trimmed_kinds:
                trimmed_kinds.append("artifacts")
        while _estimate() > input_budget and memory_list:
            dropped = memory_list.pop()
            dropped_blocks.append({"id": str(dropped.get("fact_key", "")), "kind": "memory_slice", "text": f"{dropped.get('fact_key', '')}: {dropped.get('value', '')}"})
            user_prompt = _user_prompt()
            if "memory_slice" not in trimmed_kinds:
                trimmed_kinds.append("memory_slice")
        if dropped_blocks:
            # Compaction only replaces the DROP step: the trim priority
            # above is unchanged, and the summary block is sized to the
            # budget left after trimming.
            remaining_chars = max(0, (int(input_budget) - _estimate()) * 2 - COMPACT_SLACK_CHARS)
            compacted = _compact_dropped_blocks(dropped_blocks, max_chars=remaining_chars)
            if compacted is not None:
                compact_text, compact_event = compacted
                compact_text = "\n\n" + compact_text
                user_prompt = _user_prompt()
                if _estimate() > input_budget:
                    # Summary overflowed the estimate: keep the hard trim.
                    compact_text, compact_event = "", None
                    user_prompt = _user_prompt()

    async def _call_model(user_text: str) -> tuple[dict, tuple[int, int, int]]:
        """One model round: request -> stream -> parse JSON -> (payload, usage)."""
        request = GenerationRequest(
            model=str(context["model"]),
            system_blocks=({"role": "system", "text": system_prompt},),
            input_blocks=({"role": "user", "text": user_text},),
            response_schema={"type": "object"},
            max_output_tokens=resolve_max_output_tokens(task, role),
            reasoning=context.get("reasoning"),
            metadata={"workflow": "agent-run", "run_id": str(run.get("id", "")), "role": role, "task_key": task_key},
        )
        parts: list[str] = []
        usage = None
        async for event in provider.stream(request):
            if event.event == "content.delta":
                parts.append(event.text)
            elif event.event == "usage.updated":
                usage = normalize_provider_usage(provider_id, event.data)
            elif event.event == "response.completed" and event.data.get("usage"):
                usage = normalize_provider_usage(provider_id, event.data, final=True)
        parsed = parse_model_json("".join(parts).strip())
        if not isinstance(parsed, dict):
            raise ValueError("role output must be a JSON object")  # noqa: TRY004 -- ValueError is the contract: callers catch it to mark the run failed
        return parsed, (
            usage.input_tokens if usage else 0,
            usage.output_tokens if usage else 0,
            usage.total_tokens if usage else 0,
        )

    payload, (input_tokens, output_tokens, used_tokens) = await _call_model(user_prompt)
    extra_events: list[dict[str, object]] = []
    if role == "scene_writer" and isinstance(payload.get("content"), str) and payload["content"].strip():
        # 自我打磨（慢工出细活）：初稿全文 + 自审清单回喂，取打磨终稿；
        # 打磨输出不可用（异常/非 JSON/空 content）保留初稿，不拖垮任务。
        polish_budget = max(2000, int(input_budget) // 2) if input_budget else PREV_CHAPTER_FALLBACK_MAX_CHARS
        polish_prompt = (
            user_prompt
            + "\n\n【你的初稿全文】\n"
            + elide_middle(str(payload["content"]), polish_budget)
            + "\n\n以总编眼光逐条自审：伏笔/钩子是否落实、与上一章是否自然衔接、"
            "是否有讲述式偷懒（直接宣判情绪、总结式跳情节）、字数是否达到硬要求；"
            "然后输出打磨后的终稿 JSON（title/content），只交终稿，不要输出任何解释。"
        )
        try:
            polished, polish_usage = await _call_model(polish_prompt)
        except Exception:
            polished, polish_usage = None, None  # 打磨失败保留初稿，不拖垮任务
        if polished is not None and polish_usage is not None:
            polished_content = polished.get("content")
            if isinstance(polished_content, str) and polished_content.strip():
                polished_title = polished.get("title")
                payload = {
                    **payload,
                    "content": polished_content,
                    "title": str(polished_title) if isinstance(polished_title, str) and polished_title.strip() else payload.get("title"),
                }
                p_in, p_out, p_total = polish_usage
                input_tokens += p_in
                output_tokens += p_out
                used_tokens += p_total
                extra_events.append({"event": "scene.polished", "draft_chars": len(str(payload["content"]))})
    declared = payload.get("artifact_type")
    artifact_type = str(declared) if isinstance(declared, str) and declared else default_artifact_type(role)
    if memory_list:
        # 记忆优先审计：本任务实际看到的已批准记忆条数（裁剪后）。
        extra_events.append({"event": "memory.seen", "count": len(memory_list)})
    if seam_lagging:
        # 前章 L0 摘要尚未落库（异步摘要链路延迟）：不阻塞，只留可见性。
        extra_events.append({"event": "context.summary_lagging", "role": role})
    if trimmed_kinds:
        extra_events.append({"event": "context.trimmed", "kinds": trimmed_kinds, "input_budget": input_budget})
    if compact_event is not None:
        extra_events.append({"event": "context.compacted", **compact_event, "input_budget": input_budget})
    return RoleResult(
        artifact_type=artifact_type,
        payload=payload,
        used_tokens=used_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        extra_events=extra_events,
    )


async def default_role_handler(context: TaskContext) -> RoleResult:
    """默认通用 handler：角色提示词 → 模型 → 解析 JSON → RoleResult。

    模型输出非合法 JSON 时抛 JSONDecodeError，由 executor 按 malformed_json
    语义重试（max_attempts 内重置 PENDING，否则任务 FAILED）。
    """
    from proseforge.application.agents.prompts import goal_hint_for

    run = context["run"]
    assert isinstance(run, dict)
    return await _run_default_handler(context, goal_hint=goal_hint_for(run))


# Fallback goal cap when the context carries no input_budget (direct handler
# use in tests; the executor always sets one). Same chars-per-token
# conversion as the reviewers (review_handlers._run_reviewer).
ANALYST_GOAL_FALLBACK_MAX_CHARS = 20000


@register_role("analyst")
async def analyst_role_handler(context: TaskContext) -> RoleResult:
    """analyst (the orchestrator's secretary): same pipeline as the default
    handler, except the user prompt injects the FULL run goal (the dumped
    outline) instead of the GOAL_HINT_MAX_CHARS-truncated head. Oversized
    outlines are elided head 70% / tail 20% (artifact_texts.elide_middle)
    against max(2000, input_budget // 2) chars.
    """
    from proseforge.application.agents.artifact_texts import elide_middle
    from proseforge.application.agents.prompts import goal_hint_for

    run = context["run"]
    assert isinstance(run, dict)
    goal = str(run.get("goal") or "").strip()
    if not goal:
        # Legacy run without a stored goal: keep the goal_hash fallback.
        return await _run_default_handler(context, goal_hint=goal_hint_for(run))
    input_budget = context.get("input_budget")
    max_chars = max(2000, int(input_budget) // 2) if input_budget else ANALYST_GOAL_FALLBACK_MAX_CHARS
    return await _run_default_handler(context, goal_hint=elide_middle(goal, max_chars))

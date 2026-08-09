"""评审簇角色 handler（蓝图 V3-006）：continuity/adversarial/style 评审 + merge_editor。

职责：
- 评审 handler 调模型产出带证据区间的 findings（JSON），并把评审结论持久化为
  AgentReviewModel 行（此前 review_swarm 只有内存语义，本模块负责接线落库）；
- 两条评审在同一证据上给出不同结论时，按 review_swarm.detect_conflicts 语义共享
  确定性 conflict_group（cg-<sha256(run_id|证据) 前 12 位>），双方状态置 CONFLICT；
- merge_editor 四态：select 任务做并行草稿协作融合（调模型，失败回退确定性
  择优）；review_council 任务做评审合议（调模型裁定冲突 + 排序改写指令，
  失败回退确定性去重）；analyze_merge 任务做分析三席位融合（调模型，失败
  回退确定性合并）；merge 任务只对本 run 已落库评审做四桶分类，绝不改写
  作者正文、不调模型。

评审对象选取：
- write 管线评审电池（depends_on 含 select）只审 select 任务的择优产出，
  落选的 scene_a/b/c 草稿不送审（findings 不进门禁计数）；
- 独立 review 意图（depends_on=[]，无上游 artifact）按 run.chapter_id 或
  goal 章节号注入章节正文（review_target.resolve_chapter_target），持久化为
  确定性 id 的 chapter-target artifact（三个并行评审共享一行）；无法确定
  评审对象时任务直接 FAILED（中文原因），不空转产空报告。

Artifact 类型说明：现行 RolePolicy（domain/agents/roles.py）对上述角色的 allowlist
只有 report/candidate，类型化 Artifact（ContinuityReport 等）会被 executor 的
allowlist 校验拒绝。因此评审报告以 artifact_type="report" 提交，payload 携带
report_type 与类型化 schema 的 required keys（ContinuityReport→summary+issues，
AdversarialReport→summary+risks，StyleReview→summary+issues）；policy 放开
allowlist 后，该 payload 可直接通过 validate_artifact_payload 的类型化校验。

存储评审状态 → 合并桶映射（merge_editor 与 chief_editor 共用，见 categorize_reviews）：
- payload.resolution == "accepted"（任意状态，含已裁定的冲突）→ accepted
- status == "CONFLICT" 且带 conflict_group 且未裁定 → conflicts（按组聚合，resolution=null）
- status == "UNSUPPORTED" → unsupported
- 其余（PASS/WARNING：有证据支持且无对抗结论）→ agreements
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from proseforge.application.agents.model_json import parse_model_json
from proseforge.application.agents.review_swarm import Finding, detect_conflicts
from proseforge.application.agents.review_target import (
    ChapterTarget,
    resolve_chapter_target,
)
from proseforge.application.agents.role_handlers import (
    RoleResult,
    TaskContext,
    register_role,
    resolve_max_output_tokens,
)
from proseforge.domain.agents import policy
from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.agents import (
    AgentArtifactModel,
    AgentReviewModel,
    AgentTaskModel,
)

# 评审角色 → (类型化报告名, findings 在 payload 中的键名)；键名对齐 ARTIFACT_SCHEMAS required keys
REVIEWER_REPORT_TYPES: dict[str, tuple[str, str]] = {
    "continuity_reviewer": ("ContinuityReport", "issues"),
    "adversarial_reviewer": ("AdversarialReport", "risks"),
    "style_editor": ("StyleReview", "issues"),
}

MAX_EVIDENCE_SPANS = 32  # 与 api/routes/agent_runs.py ReviewRequest 的 evidence 上限一致
_STATUS_RANK = {"PASS": 0, "UNSUPPORTED": 1, "WARNING": 2, "CONFLICT": 3}
_MERGE_BUCKETS: tuple[str, ...] = ("agreements", "conflicts", "unsupported", "accepted")


# --- 模型调用与输出归一（chief_handler 复用 stream_model_json） ---


async def stream_model_json(context: TaskContext, *, system_prompt: str, user_prompt: str) -> tuple[dict[str, Any], tuple[int, int, int]]:
    """流式调模型并解析单个 JSON 对象；返回 (payload, (input, output, total) tokens)。

    输出非合法 JSON 时抛 JSONDecodeError，由 executor 按 malformed_json 语义重试；
    模型调用不持有任何数据库事务。
    """
    from proseforge.domain.ports.model_provider import GenerationRequest
    from proseforge.providers.usage import normalize_provider_usage

    task = context["task"]
    run = context["run"]
    assert isinstance(task, dict) and isinstance(run, dict)
    provider = context["provider"]
    provider_id = str(context.get("provider_id", "unknown"))
    role = str(task["role"])
    request = GenerationRequest(
        model=str(context["model"]),
        system_blocks=({"role": "system", "text": system_prompt},),
        input_blocks=({"role": "user", "text": user_prompt},),
        response_schema={"type": "object"},
        max_output_tokens=resolve_max_output_tokens(task, role),
        reasoning=context.get("reasoning"),
        metadata={"workflow": "agent-run", "run_id": str(run.get("id", "")), "role": role, "task_key": str(task["task_key"])},
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
    payload = parse_model_json("".join(parts))
    if not isinstance(payload, dict):
        raise ValueError("role output must be a JSON object")  # noqa: TRY004 -- ValueError is the contract: callers catch it to mark the run failed
    tokens = (usage.input_tokens, usage.output_tokens, usage.total_tokens) if usage else (0, 0, 0)
    return payload, tokens


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把模型输出归一为 findings 列表；兼容 issues/risks 纯字符串清单（无证据区间）。"""
    items: list[Any] = []
    raw = payload.get("findings")
    if isinstance(raw, list):
        items.extend(raw)
    if not items:
        for key in ("issues", "risks"):
            legacy = payload.get(key)
            if isinstance(legacy, list):
                items.extend(legacy)
    findings: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            findings.append({"finding": item[:500], "severity": "medium", "target_artifact_id": None, "evidence_spans": []})
        elif isinstance(item, dict):
            raw_spans = item.get("evidence_spans")
            spans = [span for span in raw_spans if isinstance(span, dict)] if isinstance(raw_spans, list) else []
            findings.append({
                "finding": str(item.get("finding", ""))[:500],
                "severity": str(item.get("severity", "medium")),
                "target_artifact_id": str(item["target_artifact_id"]) if item.get("target_artifact_id") else None,
                "evidence_spans": [
                    {"artifact_id": str(span.get("artifact_id", "")), "start": _safe_int(span.get("start", 0)), "end": _safe_int(span.get("end", 0)), "quote": str(span.get("quote", ""))[:500]}
                    for span in spans
                ][:MAX_EVIDENCE_SPANS],
            })
    return [finding for finding in findings if finding["finding"]]


def _span_key(span: dict[str, Any]) -> str:
    """证据标识：优先 quote（detect_conflicts 按 evidence 相等判同靶），否则 artifact:start:end。"""
    quote = str(span.get("quote", "")).strip()
    if quote:
        return quote
    artifact_id = str(span.get("artifact_id", "")).strip()
    return f"{artifact_id}:{span.get('start', 0)}:{span.get('end', 0)}" if artifact_id else ""


# --- 评审快照与四桶分类（merge_editor / chief_editor / chief-proposal 端点共用） ---


def snapshot_review(row: AgentReviewModel) -> dict[str, Any]:
    """事务内快照评审行（会话关闭后 ORM 实例过期，分类/合并只读快照 dict）。"""
    try:
        payload = json.loads(row.payload or "{}")
    except ValueError:
        payload = {}
    try:
        evidence = json.loads(row.evidence or "[]")
    except ValueError:
        evidence = []
    claims = payload.get("claims") if isinstance(payload, dict) else None
    return {
        "id": row.id,
        "artifact_id": row.artifact_id,
        "reviewer_role": row.reviewer_role,
        "status": row.status,
        "conflict_group": row.conflict_group,
        "evidence": evidence if isinstance(evidence, list) else [],
        "claims": claims if isinstance(claims, list) else [],
        "resolution": payload.get("resolution") if isinstance(payload, dict) else None,
    }


def categorize_reviews(reviews: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """存储评审快照 → agreement/conflict/unsupported/accepted 四桶（映射见模块 docstring）；纯函数。"""
    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in _MERGE_BUCKETS}
    conflict_groups: dict[str, list[dict[str, Any]]] = {}
    for review in reviews:
        entry = {
            "review_id": review["id"],
            "artifact_id": review["artifact_id"],
            "reviewer_role": review["reviewer_role"],
            "status": review["status"],
            "claims": list(review["claims"]),
        }
        if review["resolution"] == "accepted":
            buckets["accepted"].append(entry)
        elif review["status"] == "CONFLICT" and review["conflict_group"]:
            conflict_groups.setdefault(str(review["conflict_group"]), []).append(entry)
        elif review["status"] == "UNSUPPORTED":
            buckets["unsupported"].append(entry)
        else:
            buckets["agreements"].append(entry)
    buckets["conflicts"] = [
        {
            "conflict_group": group,
            "parties": sorted(str(item["review_id"]) for item in items),
            "claims": [claim for item in items for claim in item["claims"]],
            "resolution": None,  # 冲突记录保留双方主张；裁定只能来自用户审批
        }
        for group, items in sorted(conflict_groups.items())
    ]
    return buckets


def build_merge_payload(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """MergeCandidate payload（summary+sources 对齐 validate_artifact_payload 的 required keys）。"""
    buckets = categorize_reviews(reviews)
    summary = (
        f"Merge of {len(reviews)} reviews: "
        f"{len(buckets['agreements'])} agreement, {len(buckets['conflicts'])} conflict group, "
        f"{len(buckets['unsupported'])} unsupported, {len(buckets['accepted'])} accepted."
    )
    return {
        "summary": summary,
        "sources": sorted(str(review["id"]) for review in reviews),
        "artifact_ids": sorted({str(review["artifact_id"]) for review in reviews}),
        **buckets,
    }


# --- 冲突接线：review_swarm.detect_conflicts 语义的持久化 ---


def _conflict_slug(run_id: str, evidence_key: str) -> str:
    """确定性冲突组 slug：同一 run 同一证据的矛盾评审复用同组（重跑幂等）。"""
    return f"cg-{hashlib.sha256(f'{run_id}|{evidence_key}'.encode()).hexdigest()[:12]}"


def _row_findings(row: AgentReviewModel) -> list[Finding]:
    """评审行 → detect_conflicts 输入（无主张或无证据的行不参与，PASS 行天然不冲突）。"""
    snapshot = snapshot_review(row)
    findings: list[Finding] = []
    for claim in snapshot["claims"]:
        message = str(claim.get("finding", "")).strip() if isinstance(claim, dict) else ""
        if not message:
            continue
        for span in snapshot["evidence"]:
            if isinstance(span, dict):
                key = _span_key(span)
                if key:
                    findings.append(Finding(reviewer=row.reviewer_role, message=message, evidence=key))
    return findings


def wire_conflicts(run_id: str, rows: list[AgentReviewModel]) -> None:
    """对一批评审行（既有 + 新增）执行冲突接线：同证据不同结论 → 双方 CONFLICT + 共享 slug。"""
    tagged = [(finding, row) for row in rows for finding in _row_findings(row)]
    row_by_finding = {id(finding): row for finding, row in tagged}
    for left, right in detect_conflicts([finding for finding, _ in tagged]):
        pair = {row_by_finding[id(left)], row_by_finding[id(right)]}
        if len(pair) < 2:
            continue  # 同一评审内部矛盾不成组（避免单方冲突组）
        slug = _conflict_slug(run_id, str(left.evidence))
        for row in pair:
            row.conflict_group = slug
            row.status = "CONFLICT"


# --- 评审 handler ---


def _review_user_prompt(role: str, task_key: str, run: dict[str, Any], artifacts: list[dict[str, Any]], scene_pack: str | None = None, artifact_texts: dict[str, str] | None = None, genre_block: str = "", memory_slice: list[dict[str, object]] | None = None, seam_block: str = "", style_block: str = "") -> str:
    from proseforge.application.agents.prompts import (
        JSON_OUTPUT_INSTRUCTION,
        goal_hint_for,
        render_memory_lines,
    )

    lines = [
        f"任务：{task_key}（角色 {role}）",
        f"写作目标摘要：{goal_hint_for(run)}",
    ]
    if genre_block:
        # Genre writing standard (from the goal's 题材 line): reviewers audit
        # against the genre pack, same injection point family as the goal.
        lines.append(genre_block)
    if style_block:
        # 文风技法卡（style_editor 专属）：与写作侧 scene_writer 同款摘要，
        # 作为审校的审美基准。
        lines.append(style_block)
    if seam_block:
        # 跨章接缝卡 + 场景衔接卡（continuity_reviewer 专属）：接缝审计基准。
        lines.append(seam_block)
        lines.append(
            "接缝审计：本章开头必须承接接缝卡的时间/空间/人物状态锚点，并守住衔接卡接力句；"
            "断裂、跳跃或接力句丢失以 findings 上报（type=seam_break，severity=high，附引文）。"
        )
    # 记忆优先（先查再想）：已批准记忆在场景包之前注入，评审据其核对
    # 人物状态/道具/时间线等跨章事实。
    lines.extend(render_memory_lines(memory_slice))
    if scene_pack:
        # Same injection point as build_task_prompt: before the artifacts.
        lines.append("叙事检索场景包：")
        lines.append(scene_pack)
    lines.append("评审对象（上游 Artifact 全文；evidence_spans 的 artifact_id 必须取自下列 id）：")
    texts = artifact_texts or {}
    for item in artifacts:
        artifact_id = str(item.get("id", ""))
        lines.append(f"=== artifact_id={artifact_id} [{item.get('artifact_type', '')}] {item.get('task_key', '')} ===")
        lines.append(texts.get(artifact_id) or str(item.get("preview", "")))
    lines.append("逐条输出 findings：每条给出 verdict；有证据时填 evidence_spans（quote 对应原文片段），无证据时 verdict=UNSUPPORTED 且 evidence_spans 为空。")
    lines.append(JSON_OUTPUT_INSTRUCTION)
    return "\n".join(lines)


async def _task_depends_on(context: TaskContext) -> set[str]:
    """The claimed task's declared dependencies (task_keys), read in a short transaction."""
    task = context["task"]
    assert isinstance(task, dict)
    uow_factory = context["uow_factory"]
    assert callable(uow_factory)
    async with uow_factory() as uow:
        row = await uow.session.get(AgentTaskModel, str(task.get("id", "")))
        if row is None:
            return set()
        try:
            raw = json.loads(row.depends_on or "[]")
        except ValueError:
            return set()
        return {str(item) for item in raw} if isinstance(raw, list) else set()


async def _ensure_chapter_target_artifact(context: TaskContext, target: ChapterTarget) -> dict[str, Any]:
    """Persist the chapter full text as a run artifact (review rows need a real
    artifact id, and the target becomes visible in the run manifest).

    Deterministic id: the three parallel reviewers share one row — the loser
    of an insert race rolls back and reuses the committed row.
    """
    run = context["run"]
    assert isinstance(run, dict)
    run_id = str(run["id"])
    artifact_id = f"chapter-target-{run_id}"
    payload = {"title": target.title, "content": target.content, "chapter_no": target.chapter_no, "source_chapter_id": target.chapter_id}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    uow_factory = context["uow_factory"]
    assert callable(uow_factory)
    async with uow_factory() as uow:
        if await uow.session.get(AgentArtifactModel, artifact_id) is None:
            uow.session.add(AgentArtifactModel(
                id=artifact_id,
                run_id=run_id,
                task_id=None,
                artifact_type="candidate",
                sha256=hashlib.sha256(raw.encode()).hexdigest(),
                provenance=json.dumps({"task_key": "chapter_target", "chapter_id": target.chapter_id}, sort_keys=True),
                preview=raw[:200],
                payload=raw,
            ))
            try:
                await uow.commit()
            except IntegrityError:
                await uow.rollback()  # a parallel reviewer committed the shared row first
    return {"id": artifact_id, "task_key": "chapter_target", "artifact_type": "candidate", "preview": raw[:200]}


async def _run_reviewer(role: str, context: TaskContext) -> RoleResult:
    policy.authorize(role, "create_artifact")  # fail-closed；PolicyDenied 由 executor 置任务 FAILED 并留 policy.denied
    task = context["task"]
    run = context["run"]
    assert isinstance(task, dict) and isinstance(run, dict)
    run_id, task_key = str(run["id"]), str(task["task_key"])
    artifacts = [item for item in context.get("artifacts", []) if isinstance(item, dict)]
    depends_on = await _task_depends_on(context)
    if SELECT_TASK_KEY in depends_on:
        # M5: the review battery audits the selected winner only — losing
        # parallel drafts (scene_a/b/c/d) must not feed findings into the gate.
        artifacts = [item for item in artifacts if str(item.get("task_key", "")) == SELECT_TASK_KEY]
    elif task_key == "recheck":
        # The final gate audits the rewrite only: recheck reviews the LATEST
        # rewrite artifact (bounded revise rounds leave several; ids are
        # time-ordered), not the whole upstream pile.
        rewrite_artifacts = [item for item in artifacts if str(item.get("task_key", "")) == "rewrite"]
        if rewrite_artifacts:
            artifacts = [max(rewrite_artifacts, key=lambda item: str(item.get("id", "")))]
    if not artifacts:
        if depends_on:
            raise ValueError("上游任务没有产出可评审的内容")
        # Standalone review intent (depends_on=[]): inject the chapter full
        # text resolved from run.chapter_id / the goal's chapter number.
        target, reason = await resolve_chapter_target(context, object_label="评审")
        if target is None:
            raise ValueError(reason or "无法确定评审对象")
        artifacts = [await _ensure_chapter_target_artifact(context, target)]
    report_type, findings_key = REVIEWER_REPORT_TYPES[role]

    # 模型调用在任何数据库事务之外
    from proseforge.application.agents.artifact_texts import load_artifact_texts
    from proseforge.application.agents.prompts import prompt_for_role
    from proseforge.application.work.retriever import render_pack_text, trim_scene_pack

    # Reviewers read FULL texts, not the 200-char executor preview (stress
    # tests: previews made reviewers report "content missing"). The text cap
    # is derived from the per-role input budget (tokens are ~chars/2 for
    # CJK) and is SHARED across all reviewed artifacts — a per-artifact cap
    # let N upstream drafts total N x budget/2 chars and overflow the model
    # window. budget/2 chars overall leaves headroom for the system prompt,
    # the scene pack and the findings output.
    input_budget = context.get("input_budget")
    text_budget = max(2000, int(input_budget) // 2) if input_budget else 8000
    max_chars = max(1000, text_budget // max(1, len(artifacts)))
    # Load full texts for the FILTERED target set (not every run artifact in
    # the context snapshot — losing drafts are excluded, and the injected
    # chapter-target artifact is not part of the executor snapshot at all).
    artifact_texts = await load_artifact_texts({**context, "artifacts": artifacts}, max_chars_per_artifact=max_chars)

    system_prompt = prompt_for_role(role)
    genre_block = ""
    style_block = ""
    if role in ("continuity_reviewer", "style_editor"):
        # 审校按题材标准审：goal 的「题材：X」行映射到题材包 SKILL.md 摘录；
        # 无映射/无包时空串不注入（genre_skills 内部已做静默保护）。
        from proseforge.application.agents.genre_skills import (
            genre_from_goal,
            genre_skill_excerpt,
            genre_style_excerpt,
        )

        genre = genre_from_goal(str(run.get("goal") or ""))
        genre_excerpt = genre_skill_excerpt(genre)
        if genre_excerpt:
            genre_block = "【题材写作指引】本章题材对应的写作规范，审校以此为准：\n" + genre_excerpt
        if role == "style_editor":
            # 文风技法卡：与写作侧 scene_writer 注入的同款合并摘要（≤800 字），
            # 作为 style_editor 审校的审美基准；无映射题材回退契诃夫/汪曾祺。
            style_excerpt = genre_style_excerpt(genre)
            if style_excerpt:
                style_block = "【文风技法卡】本章题材匹配的作家技法摘要，审校以此为审美基准（迁移手法，不模仿个人句式）：\n" + style_excerpt
    memory_slice = [item for item in run.get("memory_slice") or [] if isinstance(item, dict)]
    # 跨章接缝卡 + 场景衔接卡：continuity_reviewer 专属的接缝审计基准
    # （评审此前拿不到上一章任何信息，章间接缝无人把守）。
    seam_block, seam_lagging = "", False
    if role == "continuity_reviewer":
        from proseforge.application.agents.role_handlers import _load_scene_bridge
        from proseforge.application.agents.seam_card import load_seam_card

        seam_card, seam_lagging = await load_seam_card(context, str(run.get("goal") or ""))
        seam_parts = [seam_card] if seam_card else []
        bridge_card = await _load_scene_bridge(context)
        if bridge_card:
            seam_parts.append("【场景衔接卡】本章写作侧衔接规划（评审核对接力句是否守住）：\n" + bridge_card)
        seam_block = "\n\n".join(seam_parts)
    pack_sections = context.get("scene_pack")
    pack_text = render_pack_text(pack_sections) if pack_sections else None
    user_prompt = _review_user_prompt(role, task_key, run, artifacts, scene_pack=pack_text, artifact_texts=artifact_texts, genre_block=genre_block, memory_slice=memory_slice, seam_block=seam_block, style_block=style_block)
    trimmed_kinds: list[str] = []
    if input_budget and pack_sections and (len(system_prompt) + len(user_prompt)) // 2 > int(input_budget):
        # Still over budget (huge scene pack): trim the pack to whatever the
        # rest of the prompt leaves, same as the default handler does.
        pack_budget = max(200, int(input_budget) - (len(system_prompt) + len(_review_user_prompt(role, task_key, run, artifacts, artifact_texts=artifact_texts, genre_block=genre_block, memory_slice=memory_slice, seam_block=seam_block, style_block=style_block))) // 2)
        pack_text = trim_scene_pack(pack_sections, pack_budget)
        user_prompt = _review_user_prompt(role, task_key, run, artifacts, scene_pack=pack_text, artifact_texts=artifact_texts, genre_block=genre_block, memory_slice=memory_slice, seam_block=seam_block, style_block=style_block)
        trimmed_kinds.append("scene_pack")
    output, (input_tokens, output_tokens, used_tokens) = await stream_model_json(
        context,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    findings = _normalize_findings(output)
    summary = str(output.get("summary", ""))[:2000]

    if role == "continuity_reviewer":
        # 约翰逊 AI 腔检测（确定性阈值规则，不调模型）：对被审正文跑
        # detect_ai_flavor，命中合并进报告 issues（type="ai_flavor"）。
        # severity=high + evidence_spans 齐备，走 quality_gate 现有
        # evidenced-high 计数逻辑（阈值/降级约定不变），不新造打分体系。
        from proseforge.application.agents.ai_flavor import detect_ai_flavor

        for item in artifacts:
            artifact_id = str(item.get("id", ""))
            target_text = artifact_texts.get(artifact_id) or str(item.get("preview", ""))
            for issue in detect_ai_flavor(target_text):
                findings.append({
                    "finding": str(issue["finding"])[:500],
                    "severity": "high",
                    "type": "ai_flavor",
                    "rule": str(issue.get("rule", "")),
                    "target_artifact_id": artifact_id or None,
                    "evidence_spans": [
                        {"artifact_id": artifact_id, "start": _safe_int(span.get("start", 0)), "end": _safe_int(span.get("end", 0)), "quote": str(span.get("quote", ""))[:500]}
                        for span in issue.get("evidence", [])
                        if isinstance(span, dict)
                    ][:MAX_EVIDENCE_SPANS],
                })

    # 段落锚点引用（第 11 项前置）：给带引文的 findings 补 paragraph_refs，
    # 定点改写按引文重定位，refs 仅供审计/展示，best-effort 不改评审语义。
    from proseforge.application.agents.pinpoint_rewrite import (
        annotate_findings_paragraphs,
    )

    annotate_findings_paragraphs(findings, artifact_texts)

    # findings 按目标 artifact 归组；幻觉 id / 无目标的归入第一个上游 artifact（确定性，通常即唯一被审稿）
    by_artifact: dict[str, list[dict[str, Any]]] = {str(item.get("id", "")): [] for item in artifacts}
    fallback_id = str(artifacts[0].get("id", "")) if artifacts else ""
    for finding in findings:
        target = finding["target_artifact_id"]
        if not target:
            target = next((str(span["artifact_id"]) for span in finding["evidence_spans"] if span.get("artifact_id") in by_artifact), None)
        if target not in by_artifact:
            target = fallback_id
        if target:
            by_artifact[target].append(finding)

    per_artifact: list[dict[str, Any]] = []
    for item in artifacts:
        artifact_id = str(item.get("id", ""))
        related = by_artifact.get(artifact_id, [])
        spans = [span for finding in related for span in finding["evidence_spans"]][:MAX_EVIDENCE_SPANS]
        if not related:
            # PASS 也必须带证据（服务端规则：仅 UNSUPPORTED 允许空证据）——引用被审 artifact 本身
            status, evidence = "PASS", [{"artifact_id": artifact_id, "start": 0, "end": 0, "quote": str(item.get("preview", ""))[:120], "note": "no findings"}]
        elif spans:
            status, evidence = "WARNING", spans
        else:
            status, evidence = "UNSUPPORTED", []
        per_artifact.append({
            "artifact_id": artifact_id,
            "status": status,
            "evidence": evidence,
            "claims": [{"finding": finding["finding"], "severity": finding["severity"]} for finding in related],
        })

    uow_factory = context["uow_factory"]
    assert callable(uow_factory)
    async with uow_factory() as uow:
        existing = list(await uow.session.scalars(select(AgentReviewModel).where(AgentReviewModel.run_id == run_id)))
        by_key = {(row.artifact_id, row.reviewer_role): row for row in existing}
        new_rows: list[AgentReviewModel] = []
        for entry in per_artifact:
            if (entry["artifact_id"], role) in by_key:
                continue  # 幂等：同 (artifact, reviewer) 复用已落库行，任务重试/重投不重复建行
            row = AgentReviewModel(
                id=new_id(),
                run_id=run_id,
                artifact_id=entry["artifact_id"],
                reviewer_role=role,
                status=entry["status"],
                evidence=json.dumps(entry["evidence"], ensure_ascii=False),
                payload=json.dumps({"claims": entry["claims"], "resolution": None}, ensure_ascii=False),
            )
            uow.session.add(row)
            by_key[(entry["artifact_id"], role)] = row
            new_rows.append(row)
        wire_conflicts(run_id, [*existing, *new_rows])
        await uow.commit()

    review_rows = [by_key[(entry["artifact_id"], role)] for entry in per_artifact]
    verdict = max((row.status for row in review_rows), key=lambda item: _STATUS_RANK.get(item, 0), default="UNSUPPORTED")
    extra_events: list[dict[str, object]] = [
        {"event": "review.committed", "review_id": row.id, "artifact_id": row.artifact_id, "reviewer_role": role, "status": row.status, "conflict_group": row.conflict_group}
        for row in new_rows
    ]
    if trimmed_kinds:
        # Same audit event as the default handler's budget trimming.
        extra_events.append({"event": "context.trimmed", "kinds": trimmed_kinds, "input_budget": input_budget})
    if memory_slice:
        # 记忆优先审计：本任务实际看到的已批准记忆条数。
        extra_events.append({"event": "memory.seen", "count": len(memory_slice)})
    if seam_lagging:
        # 前章 L0 摘要尚未落库（异步摘要链路延迟）：不阻塞，只留可见性。
        extra_events.append({"event": "context.summary_lagging", "role": role})
    return RoleResult(
        artifact_type="report",  # RolePolicy allowlist 仅 report/candidate；report_type 承载类型化报告名
        payload={
            "report_type": report_type,
            "summary": summary or f"{role} reviewed {len(artifacts)} artifacts.",
            findings_key: findings,
            "review_ids": [row.id for row in review_rows],
            "verdict": verdict,
        },
        used_tokens=used_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        extra_events=extra_events,
    )


def _make_reviewer_handler(role: str) -> Callable[[TaskContext], Awaitable[RoleResult]]:
    async def handler(context: TaskContext) -> RoleResult:
        return await _run_reviewer(role, context)

    handler.__name__ = f"{role}_handler"
    return handler


for _role in REVIEWER_REPORT_TYPES:
    register_role(_role)(_make_reviewer_handler(_role))


# --- 并行草稿融合（write 管线 select 任务，merge_editor 角色） ---

SELECT_TASK_KEY = "select"

# 融合输入的草稿正文总预算：无 input_budget（纯测试 context）时的兜底。
# 多份草稿共享一份预算（与评审同一约定），防止 N 份草稿各自顶格撑爆窗口。
SELECT_DRAFTS_FALLBACK_MAX_CHARS = 16000


def _deterministic_pick(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """确定性择优：正文最长者胜出，并列按 artifact id 升序取先（融合失败/单稿时的兜底）。"""
    # max 取首个最大项：先按 artifact id 升序排序，长度并列时确定性取 id 最小者
    return max(sorted(candidates, key=lambda item: item["id"]), key=lambda item: len(item["content"]))


def _select_payload(winner: dict[str, Any], *, candidates: list[dict[str, Any]], rationale: str, sources: list[str]) -> dict[str, Any]:
    title = winner.get("title")
    return {
        "title": str(title) if isinstance(title, str) and str(title).strip() else "选定草稿",
        "content": winner["content"],
        "selected_from": winner["id"],
        "selection_rationale": rationale,
        "sources": sources,
        "candidates": [{"artifact_id": item["id"], "chars": len(item["content"])} for item in sorted(candidates, key=lambda item: item["id"])],
    }


async def _fuse_drafts(context: TaskContext, candidates: list[dict[str, Any]], run: dict[str, Any]) -> tuple[dict[str, Any], str, list[str], tuple[int, int, int]]:
    """协作融合：调模型把多份并行草稿博采众长合成一章定稿。

    返回 (winner, rationale, sources, usage)。任何模型侧失败（异常/非法 JSON/
    空 content）都抛给调用方走确定性兜底——融合是增益，不是新的单点故障。
    """
    from proseforge.application.agents.ai_flavor import (
        HUMAN_FLAVOR_GUIDE,
        WRITING_STYLE_RULES,
    )
    from proseforge.application.agents.artifact_texts import elide_middle
    from proseforge.application.agents.prompts import (
        goal_hint_for,
        prompt_for_task,
        render_memory_lines,
    )
    from proseforge.application.agents.quality_gate import parse_min_words

    input_budget = context.get("input_budget")
    text_budget = max(2000, int(input_budget) // 2) if input_budget else SELECT_DRAFTS_FALLBACK_MAX_CHARS
    per_draft = max(1000, text_budget // len(candidates))
    goal_hint = goal_hint_for(run)

    lines = [
        "任务：select（角色 merge_editor）",
        f"写作目标摘要：{goal_hint}",
        f"篇幅硬要求：融合定稿正文 content 不得少于 {parse_min_words(goal_hint)} 字，融合只允许加厚不允许抽瘦。",
        "",
        WRITING_STYLE_RULES,
        "",
        HUMAN_FLAVOR_GUIDE,
        "",
    ]
    # 记忆优先（先查再想）：融合不得与已批准记忆（人物状态/道具/时间线）冲突。
    lines.extend(render_memory_lines([item for item in run.get("memory_slice") or [] if isinstance(item, dict)]))
    lines.append(f"以下是同一章的 {len(candidates)} 份并行草稿（各有长短，正文过长时中段已省略）：")
    for item in sorted(candidates, key=lambda entry: entry["id"]):
        lines.append(f"=== artifact_id={item['id']} 草稿《{item.get('title') or '无题'}》 ===")
        lines.append(elide_middle(item["content"], per_draft))
    lines.append(
        "融合指令：以整体最强的一版为骨架，吸收其余草稿的亮点（更具体的动作与感官细节、"
        "更生动的对白、更准的伏笔落点、更自然的衔接），统一文风抹平接缝，产出融合定稿；"
        "rationale 说明骨架选择与吸收来源，backbone 填骨架稿 artifact_id，sources 填实际采用的 artifact_id。"
    )
    from proseforge.application.agents.prompts import JSON_OUTPUT_INSTRUCTION

    lines.append(JSON_OUTPUT_INSTRUCTION)

    output, usage = await stream_model_json(
        context,
        system_prompt=prompt_for_task("merge_editor", SELECT_TASK_KEY),
        user_prompt="\n".join(lines),
    )
    content = output.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("fusion output has empty content")
    known_ids = {item["id"] for item in candidates}
    backbone = str(output.get("backbone") or "")
    if backbone not in known_ids:
        backbone = _deterministic_pick(candidates)["id"]  # 模型没报可靠骨架：按兜底胜者记账
    sources = [str(item) for item in output.get("sources") or [] if str(item) in known_ids] or sorted(known_ids)
    rationale = str(output.get("rationale") or "").strip()[:1000] or "collaborative fusion"
    title = output.get("title")
    winner = {
        "id": backbone,
        "title": str(title) if isinstance(title, str) and title.strip() else "融合定稿",
        "content": content,
    }
    return winner, rationale, sources, usage


async def _select_draft(context: TaskContext) -> RoleResult:
    """select 任务：多份并行场景草稿（scene_a/b/c/d）的协作融合——调模型博采众长
    合成一章定稿（以最强稿为骨架、吸收他稿亮点），而不是简单挑一份。

    降级纪律：只有一份草稿时直接采用（不调模型）；融合调用失败/产出不可用时，
    回退确定性择优（正文最长者，并列按 artifact id 升序），并记 select.fallback
    事件——融合是增益，管线不能死在这一次额外调用上。理由随 artifact payload
    落库（selection_rationale + sources + candidates 清单）。
    """
    policy.authorize("merge_editor", "create_artifact")
    run = context["run"]
    assert isinstance(run, dict)
    run_id = str(run["id"])
    artifacts = [item for item in context.get("artifacts", []) if isinstance(item, dict)]
    scene_ids = [str(item["id"]) for item in artifacts if str(item.get("task_key", "")).startswith("scene") and item.get("id")]
    artifact_ids = scene_ids or [str(item["id"]) for item in artifacts if item.get("id")]
    uow_factory = context["uow_factory"]
    assert callable(uow_factory)
    candidates: list[dict[str, Any]] = []
    if artifact_ids:
        # 短事务内快照草稿 payload（会话关闭后 ORM 实例过期，只留 dict）
        async with uow_factory() as uow:
            rows = [
                (row.id, row.payload)
                for row in await uow.session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.id.in_(artifact_ids)))
            ]
        for artifact_id, raw_payload in rows:
            try:
                payload = json.loads(raw_payload)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            content = payload.get("content")
            if isinstance(content, str) and content.strip():
                candidates.append({"id": artifact_id, "title": payload.get("title"), "content": content})
    if not candidates:
        raise ValueError("select task found no scene drafts upstream")

    mode, usage = "fusion", (0, 0, 0)
    if len(candidates) == 1:
        # 单稿无需融合：直接采用，省一次模型调用
        mode = "single"
        winner = candidates[0]
        rationale = "Single upstream draft; adopted without a fusion call."
        sources = [winner["id"]]
    else:
        try:
            winner, model_rationale, sources, usage = await _fuse_drafts(context, candidates, run)
            rationale = f"Collaborative fusion of {len(candidates)} parallel drafts: {model_rationale}"
        except Exception as exc:  # 融合失败不拖垮管线：确定性择优兜底
            mode = "fallback"
            winner = _deterministic_pick(candidates)
            rationale = (
                f"Fusion call failed ({type(exc).__name__}: {str(exc)[:200]}); deterministic pick: longest of "
                f"{len(candidates)} parallel drafts; ties broken by artifact id."
            )
            sources = [winner["id"]]

    extra_events: list[dict[str, object]] = []
    memory_count = len([item for item in run.get("memory_slice") or [] if isinstance(item, dict)])
    if memory_count:
        # 记忆优先审计：本任务实际看到的已批准记忆条数。
        extra_events.append({"event": "memory.seen", "count": memory_count})
    if mode == "fallback":
        extra_events.append({"event": "select.fallback", "run_id": run_id, "candidate_count": len(candidates)})
    extra_events.append({"event": "select.committed", "run_id": run_id, "selected_from": winner["id"], "candidate_count": len(candidates), "mode": mode})
    return RoleResult(
        artifact_type="candidate",
        payload=_select_payload(winner, candidates=candidates, rationale=rationale, sources=sources),
        used_tokens=usage[2],
        input_tokens=usage[0],
        output_tokens=usage[1],
        extra_events=extra_events,
    )


# --- 评审合议（write 管线 review_council 任务，merge_editor 角色） ---

COUNCIL_TASK_KEY = "review_council"
ANALYZE_MERGE_TASK_KEY = "analyze_merge"

# 评审 task_key → reviewer 角色（合议输入标注来源用）
_REVIEW_TASK_ROLES: dict[str, str] = {
    "review_continuity": "continuity_reviewer",
    "review_adversarial": "adversarial_reviewer",
    "review_style": "style_editor",
}
_COUNCIL_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
# 合议/融合 prompt 的正文预算：无 input_budget（纯测试 context）时的兜底。
_COUNCIL_FALLBACK_MAX_CHARS = 12000


def _council_severity_rank(value: Any) -> int:
    return _COUNCIL_SEVERITY_RANK.get(str(value or "medium").lower(), 1)


async def _load_task_payloads(context: TaskContext, task_keys: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """context artifacts 里指定 task_key 的 Artifact → {task_key: payload dict}（同 key 多份取 id 最新）。"""
    artifacts = [item for item in context.get("artifacts", []) if isinstance(item, dict)]
    wanted = {
        str(item["id"]): str(item.get("task_key", ""))
        for item in artifacts
        if item.get("id") and str(item.get("task_key", "")) in task_keys
    }
    uow_factory = context.get("uow_factory")
    if not wanted or uow_factory is None:
        return {}
    payloads: dict[str, dict[str, Any]] = {}
    async with uow_factory() as uow:  # type: ignore[operator]
        rows = await uow.session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.id.in_(wanted)).order_by(AgentArtifactModel.id))
        for row in rows:
            try:
                payload = json.loads(row.payload)
            except ValueError:
                continue
            if isinstance(payload, dict):
                payloads[wanted[row.id]] = payload
    return payloads


async def load_council_payload(context: TaskContext) -> dict[str, Any] | None:
    """本 run 的 review_council 合议 Artifact payload（最新一份）；无合议/无 uow 返回 None。

    rewrite 整章路径与 pinpoint 定点改写共用：合议产出存在时一律消费合议
    裁定清单，不再退回四桶/原始 findings（冲突语义不被绕过）。
    """
    payloads = await _load_task_payloads(context, (COUNCIL_TASK_KEY,))
    payload = payloads.get(COUNCIL_TASK_KEY)
    if payload is None or ("rewrite_instructions" not in payload and "findings" not in payload):
        return None
    return payload


def _extract_review_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """评审 Artifact payload → findings 条目（issues/risks/findings 三键兼容）。"""
    for key in ("issues", "risks", "findings"):
        raw = payload.get(key)
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
    return []


def _quotes_of(item: dict[str, Any]) -> list[str]:
    """finding 条目的引文清单：evidence/evidence_spans 两形态兼容。"""
    quotes: list[str] = []
    raw_evidence = item.get("evidence")
    if isinstance(raw_evidence, list):
        for entry in raw_evidence:
            quote = str(entry or "").strip()
            if quote and quote not in quotes:
                quotes.append(quote[:500])
    raw_spans = item.get("evidence_spans")
    if isinstance(raw_spans, list):
        for span in raw_spans:
            if isinstance(span, dict):
                quote = str(span.get("quote") or "").strip()
                if quote and quote not in quotes:
                    quotes.append(quote[:500])
    return quotes


def _normalize_council_findings(raw: Any) -> list[dict[str, Any]]:
    """合议模型输出 findings → [{finding, severity, source, evidence, evidence_spans}]。

    evidence_spans（quote-only）随条目落库：pinpoint 的通用 findings 提取器
    认这个形态，作为合议专用消费路径之外的兜底。
    """
    items = raw if isinstance(raw, list) else []
    findings: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            item = {"finding": item}
        if not isinstance(item, dict):
            continue
        finding = str(item.get("finding") or "").strip()[:500]
        if not finding:
            continue
        source = item.get("source")
        sources = [str(entry) for entry in source if str(entry).strip()] if isinstance(source, list) else ([str(source)] if source else [])
        quotes = _quotes_of(item)
        findings.append({
            "finding": finding,
            "severity": str(item.get("severity") or "medium"),
            "source": sources,
            "evidence": quotes,
            "evidence_spans": [{"quote": quote} for quote in quotes],
        })
    return findings


def _normalize_council_rulings(raw: Any) -> list[dict[str, Any]]:
    """合议模型输出 rulings → [{conflict_group, winner_role, resolution, reason}]（缺组/缺胜方的丢弃）。"""
    items = raw if isinstance(raw, list) else []
    rulings: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        group = str(item.get("conflict_group") or "").strip()
        winner = str(item.get("winner_role") or "").strip()
        if not group or not winner:
            continue
        rulings.append({
            "conflict_group": group,
            "winner_role": winner,
            "resolution": str(item.get("resolution") or "").strip()[:500],
            "reason": str(item.get("reason") or "").strip()[:1000],
        })
    return rulings


def _normalize_council_instructions(raw: Any) -> list[dict[str, Any]]:
    """合议模型输出 rewrite_instructions → 按严重度排序的 [{finding, severity, instruction, evidence}]。"""
    items = raw if isinstance(raw, list) else []
    instructions: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            item = {"finding": item}
        if not isinstance(item, dict):
            continue
        finding = str(item.get("finding") or "").strip()[:500]
        instruction = str(item.get("instruction") or "").strip()[:500]
        if not finding and not instruction:
            continue
        instructions.append({
            "finding": finding or instruction,
            "severity": str(item.get("severity") or "medium"),
            "instruction": instruction or finding,
            "evidence": _quotes_of(item),
        })
    instructions.sort(key=lambda entry: (_council_severity_rank(entry["severity"]), entry["finding"]))
    return instructions


def _council_fallback(reports: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """合议模型调用失败的确定性兜底：按引文/文本去重三份 findings（source 并集），
    指令清单 = 去重 findings 按严重度排序；冲突不裁定（保持未裁定语义）。"""
    merged: dict[str, dict[str, Any]] = {}
    for report in reports:
        for item in report["findings"]:
            finding = str(item.get("finding") or "").strip()[:500]
            quotes = _quotes_of(item)
            if not finding and not quotes:
                continue
            key = quotes[0] if quotes else finding
            entry = merged.setdefault(key, {
                "finding": finding or "（无文字描述，按引文定位）",
                "severity": str(item.get("severity") or "medium"),
                "source": [],
                "evidence": [],
                "evidence_spans": [],
            })
            if report["reviewer_role"] not in entry["source"]:
                entry["source"].append(report["reviewer_role"])
            for quote in quotes:
                if quote not in entry["evidence"]:
                    entry["evidence"].append(quote)
                    entry["evidence_spans"].append({"quote": quote})
    findings = sorted(merged.values(), key=lambda entry: (_council_severity_rank(entry["severity"]), entry["finding"]))
    instructions = [
        {"finding": entry["finding"], "severity": entry["severity"], "instruction": entry["finding"], "evidence": list(entry["evidence"])}
        for entry in findings
    ]
    return findings, instructions


async def _apply_council_rulings(context: TaskContext, run_id: str, rulings: list[dict[str, Any]]) -> list[str]:
    """合议裁定落库：被裁定冲突组的评审行 resolution 置 accepted（胜方）/ rejected（负方）。

    resolution 语义与用户审批一致（categorize_reviews：accepted → accepted 桶；
    非 None → 不再计未裁定冲突，chief proposal guard 放行）；裁定原文随
    payload.council_resolution 保留，可审计。返回实际裁定的 conflict_group。
    """
    by_group = {ruling["conflict_group"]: ruling for ruling in rulings}
    if not by_group:
        return []
    uow_factory = context["uow_factory"]
    assert callable(uow_factory)
    applied: set[str] = set()
    async with uow_factory() as uow:
        rows = await uow.session.scalars(
            select(AgentReviewModel).where(AgentReviewModel.run_id == run_id, AgentReviewModel.conflict_group.in_(by_group))
        )
        for row in rows:
            ruling = by_group.get(str(row.conflict_group or ""))
            if ruling is None:
                continue
            try:
                payload = json.loads(row.payload or "{}")
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload["resolution"] = "accepted" if row.reviewer_role == ruling["winner_role"] else "rejected"
            payload["council_resolution"] = ruling["resolution"] or ruling["reason"]
            row.payload = json.dumps(payload, ensure_ascii=False)
            applied.add(str(row.conflict_group))
        await uow.commit()
    return sorted(applied)


async def _review_council(context: TaskContext) -> RoleResult:
    """review_council 任务：三份评审全文 + wire_conflicts 冲突组 → 合议裁定清单（调模型）。

    产出（candidate artifact，落库可审计）：去重 findings（source 记全上报者）、
    每个冲突组的裁定（winner_role + resolution + reason，同步写回评审行
    resolution 字段）、按严重度排序的 rewrite_instructions（每条带引文）。
    降级纪律与 select 同款：模型调用失败回退确定性去重（冲突不裁定），
    记 council.fallback 事件——合议是增益，管线不能死在这一次调用上。
    """
    from proseforge.application.agents.artifact_texts import elide_middle
    from proseforge.application.agents.prompts import (
        JSON_OUTPUT_INSTRUCTION,
        goal_hint_for,
        prompt_for_task,
    )

    policy.authorize("merge_editor", "create_artifact")
    run = context["run"]
    assert isinstance(run, dict)
    run_id = str(run["id"])
    uow_factory = context["uow_factory"]
    assert callable(uow_factory)
    async with uow_factory() as uow:
        reviews = [snapshot_review(row) for row in await uow.session.scalars(select(AgentReviewModel).where(AgentReviewModel.run_id == run_id))]
    conflicts = categorize_reviews(reviews)["conflicts"]
    review_payloads = await _load_task_payloads(context, tuple(_REVIEW_TASK_ROLES))
    reports = [
        {
            "task_key": task_key,
            "reviewer_role": role,
            "summary": str(review_payloads.get(task_key, {}).get("summary") or ""),
            "findings": _extract_review_findings(review_payloads.get(task_key, {})),
        }
        for task_key, role in _REVIEW_TASK_ROLES.items()
    ]

    input_budget = context.get("input_budget")
    text_budget = max(2000, int(input_budget) // 2) if input_budget else _COUNCIL_FALLBACK_MAX_CHARS
    per_report = max(1000, text_budget // max(1, len(reports) + 1))
    lines = [
        f"任务：{COUNCIL_TASK_KEY}（角色 merge_editor）",
        f"写作目标摘要：{goal_hint_for(run)}",
        "三位评审的 findings 全文（各自独立评审、互不可见，由你合议）：",
    ]
    for report in reports:
        report_text = json.dumps({"summary": report["summary"], "findings": report["findings"]}, ensure_ascii=False)
        lines.append(f"=== {report['reviewer_role']}（{report['task_key']}） ===")
        lines.append(elide_middle(report_text, per_report))
    if conflicts:
        lines.append("冲突组（同一证据、结论相反；逐组裁定 winner_role，reason 必须引用证据本身）：")
        lines.append(elide_middle(json.dumps(conflicts, ensure_ascii=False), per_report))
    else:
        lines.append("本次评审无冲突组（rulings 输出空列表）。")
    lines.append(
        "合议指令：去重 findings（同一问题多人上报合并为一条，source 记全上报者，引文保留最准的一份）；"
        "逐组裁定冲突；产出按严重度排序（high 在前）的 rewrite_instructions——每条对应一个真实问题，"
        "带 evidence 原文引文，instruction 只说清改哪里、为什么改，不替主编写正文。"
    )
    lines.append(JSON_OUTPUT_INSTRUCTION)

    mode, usage = "council", (0, 0, 0)
    try:
        output, usage = await stream_model_json(
            context,
            system_prompt=prompt_for_task("merge_editor", COUNCIL_TASK_KEY),
            user_prompt="\n".join(lines),
        )
        findings = _normalize_council_findings(output.get("findings"))
        rulings = _normalize_council_rulings(output.get("rulings"))
        instructions = _normalize_council_instructions(output.get("rewrite_instructions"))
        summary = str(output.get("summary") or "").strip()[:2000]
    except Exception as exc:  # 合议失败不拖垮管线：确定性去重兜底
        mode = "fallback"
        findings, instructions = _council_fallback(reports)
        rulings = []
        summary = (
            f"Council call failed ({type(exc).__name__}: {str(exc)[:200]}); "
            f"deterministic dedup of {len(findings)} findings; conflicts left unresolved."
        )
    applied = await _apply_council_rulings(context, run_id, rulings) if rulings else []
    if not summary:
        summary = f"Council of {len(reports)} reviews: {len(findings)} findings, {len(rulings)} rulings, {len(instructions)} rewrite instructions."

    extra_events: list[dict[str, object]] = []
    if mode == "fallback":
        extra_events.append({"event": "council.fallback", "run_id": run_id, "finding_count": len(findings)})
    extra_events.append({
        "event": "council.committed",
        "run_id": run_id,
        "mode": mode,
        "findings": len(findings),
        "rulings": len(rulings),
        "ruled_groups": applied,
        "rewrite_instructions": len(instructions),
    })
    return RoleResult(
        artifact_type="candidate",
        payload={
            "summary": summary,
            "sources": sorted(str(review["id"]) for review in reviews),
            "findings": findings,
            "rulings": rulings,
            "rewrite_instructions": instructions,
            "mode": mode,
        },
        used_tokens=usage[2],
        input_tokens=usage[0],
        output_tokens=usage[1],
        extra_events=extra_events,
    )


# --- 分析三席位融合（analyze 管线 analyze_merge 任务，merge_editor 角色） ---

_ANALYZE_SEAT_TASK_KEYS: tuple[str, ...] = ("analyze_structure", "analyze_cast", "analyze_hooks")


def _hooks_by_chapter(hooks_payload: dict[str, Any]) -> dict[int, str]:
    """伏笔席位 chapter_hooks → {chapter_no: hooks}（chapter_no 不可解析的条目丢弃）。"""
    raw = hooks_payload.get("chapter_hooks")
    entries = raw if isinstance(raw, list) else []
    hooks: dict[int, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            chapter_no = int(entry.get("chapter_no"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        text = str(entry.get("hooks") or "").strip()
        if text:
            hooks[chapter_no] = text
    return hooks


def _analyze_merge_payload(chapters: list[dict[str, Any]], *, seats: dict[str, dict[str, Any]], title: Any, volumes: Any) -> dict[str, Any]:
    """融合产出契约组装：normalize_chapters 兼容字段原样携带，volumes/cast 可选汇合。"""
    structure = seats.get("analyze_structure") or {}
    final_volumes = volumes if isinstance(volumes, list) and volumes else structure.get("volumes")
    payload: dict[str, Any] = {
        "title": str(title or structure.get("title") or "未命名"),
        "total_chapters": len(chapters),
        "chapters": chapters,
    }
    if isinstance(final_volumes, list) and final_volumes:
        payload["volumes"] = final_volumes
    cast = (seats.get("analyze_cast") or {}).get("characters")
    if isinstance(cast, list) and cast:
        payload["cast"] = cast
    return payload


def _analyze_merge_fallback(seats: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """融合模型调用失败的确定性兜底：结构席位 chapters 为骨架，伏笔席位
    chapter_hooks 按章补齐空 hooks；无结构 chapters 返回 None（任务失败重试）。"""
    structure = seats.get("analyze_structure") or {}
    chapters = [dict(chapter) for chapter in structure.get("chapters") or [] if isinstance(chapter, dict)]
    if not chapters:
        return None
    hooks_by_no = _hooks_by_chapter(seats.get("analyze_hooks") or {})
    for chapter in chapters:
        if str(chapter.get("hooks") or "").strip():
            continue
        try:
            chapter_no = int(chapter.get("chapter_no"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if chapter_no in hooks_by_no:
            chapter["hooks"] = hooks_by_no[chapter_no]
    return _analyze_merge_payload(chapters, seats=seats, title=structure.get("title"), volumes=structure.get("volumes"))


async def _analyze_merge(context: TaskContext) -> RoleResult:
    """analyze_merge 任务：三专项席位（结构/人物/伏笔）产出 → 最终逐章工作流（调模型）。

    产出契约保持 normalize_chapters 兼容（chapter_no/title/summary/hooks/
    target_words），volumes 可选字段在此汇合（结构席位给出即携带）。降级纪律
    与 select 同款：融合失败回退确定性合并（结构骨架 + 伏笔补齐 hooks），
    记 analyze_merge.fallback；结构席位本身无 chapters 时任务失败走重试。
    """
    from proseforge.application.agents.artifact_texts import elide_middle
    from proseforge.application.agents.prompts import (
        JSON_OUTPUT_INSTRUCTION,
        goal_hint_for,
        prompt_for_task,
    )

    policy.authorize("merge_editor", "create_artifact")
    run = context["run"]
    assert isinstance(run, dict)
    run_id = str(run["id"])
    seats = await _load_task_payloads(context, _ANALYZE_SEAT_TASK_KEYS)

    input_budget = context.get("input_budget")
    text_budget = max(2000, int(input_budget) // 2) if input_budget else _COUNCIL_FALLBACK_MAX_CHARS
    per_seat = max(1000, text_budget // max(1, len(_ANALYZE_SEAT_TASK_KEYS)))
    lines = [
        f"任务：{ANALYZE_MERGE_TASK_KEY}（角色 merge_editor）",
        f"写作目标摘要：{goal_hint_for(run)}",
        "三个专项席位的分析产出（各自独立、互不可见，由你融合成最终逐章工作流）：",
    ]
    for task_key in _ANALYZE_SEAT_TASK_KEYS:
        seat_text = json.dumps(seats.get(task_key) or {"note": "本席位无产出"}, ensure_ascii=False)
        lines.append(f"=== {task_key} ===")
        lines.append(elide_middle(seat_text, per_seat))
    lines.append(
        "融合指令：以结构席位的 chapters 为骨架（一章不漏、顺序不变、target_words 沿用），"
        "hooks 取自伏笔席位的 chapter_hooks（写清埋入/回收），人物席位的关键弧光变化融入 summary；"
        "volumes 沿用结构席位（大纲有分卷时必填）。chapters 必须覆盖大纲中的每一章。"
    )
    lines.append(JSON_OUTPUT_INSTRUCTION)

    mode, usage = "fusion", (0, 0, 0)
    payload: dict[str, Any] | None = None
    try:
        output, usage = await stream_model_json(
            context,
            system_prompt=prompt_for_task("merge_editor", ANALYZE_MERGE_TASK_KEY),
            user_prompt="\n".join(lines),
        )
        # 模型合法返回空 chapters（大纲不可拆解）照原样产出：下游
        # normalize_chapters 得到空清单，留在人工逐章路径，不算失败。
        raw_chapters = output.get("chapters")
        chapters = [dict(chapter) for chapter in raw_chapters if isinstance(chapter, dict)] if isinstance(raw_chapters, list) else []
        payload = _analyze_merge_payload(chapters, seats=seats, title=output.get("title"), volumes=output.get("volumes"))
    except Exception:
        payload = None  # 融合调用失败走确定性兜底
    if payload is None:
        payload = _analyze_merge_fallback(seats)
        mode = "fallback"
        usage = (0, 0, 0)
    if payload is None:
        raise ValueError("analyze seats produced no usable chapters")

    extra_events: list[dict[str, object]] = []
    if mode == "fallback":
        extra_events.append({"event": "analyze_merge.fallback", "run_id": run_id})
    extra_events.append({
        "event": "analyze_merge.committed",
        "run_id": run_id,
        "mode": mode,
        "chapters": len(payload["chapters"]),
        "volumes": len(payload.get("volumes") or []),
    })
    return RoleResult(
        artifact_type="candidate",
        payload=payload,
        used_tokens=usage[2],
        input_tokens=usage[0],
        output_tokens=usage[1],
        extra_events=extra_events,
    )


@register_role("merge_editor")
async def merge_editor_handler(context: TaskContext) -> RoleResult:
    """merge_editor 四态：select=并行草稿协作融合；review_council=评审合议裁定（调模型，
    失败回退确定性去重）；analyze_merge=分析三席位融合（调模型，失败回退确定性合并）；
    其余（merge 任务）对本 run 已落库评审做四桶分类、不调模型。"""
    task = context["task"]
    assert isinstance(task, dict)
    task_key = str(task.get("task_key", ""))
    if task_key == SELECT_TASK_KEY:
        return await _select_draft(context)
    if task_key == COUNCIL_TASK_KEY:
        return await _review_council(context)
    if task_key == ANALYZE_MERGE_TASK_KEY:
        return await _analyze_merge(context)
    policy.authorize("merge_editor", "create_artifact")
    run = context["run"]
    assert isinstance(run, dict)
    run_id = str(run["id"])
    uow_factory = context["uow_factory"]
    assert callable(uow_factory)
    async with uow_factory() as uow:
        reviews = [snapshot_review(row) for row in await uow.session.scalars(select(AgentReviewModel).where(AgentReviewModel.run_id == run_id))]
    payload = build_merge_payload(reviews)
    return RoleResult(
        artifact_type="candidate",
        payload=payload,
        extra_events=[{"event": "merge.committed", "run_id": run_id, "review_count": len(reviews), **{key: len(payload[key]) for key in _MERGE_BUCKETS}}],
    )

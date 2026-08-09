"""Deterministic agent-run summary renderer (no model call).

Turns a finished run's tasks/artifacts into the markdown written back to
the swarm conversation's assistant message, and feeds the export.zip
layout (summary.md / outline.md / scenes/ / reviews/). Everything here is
a pure function of plain dicts — unit-testable without a database.

Write-pipeline runs render the auto-advance narrative (no more "reply to
continue" prompts — the pipeline already ran review + rewrite):
「总调度：第 N 章流水线完成。写作 ✓（X 字）→ 审校 ✓/发现 N 个问题 →
已自动改写 ✓/无需改写 → 定稿《标题》第 N 章」, followed by the top-5
high/medium findings and a "⚠ 带警告交付" line when the post-rewrite
gate re-check still fails.

Analyze runs (outline -> per-chapter workflow) render the chapter list:
「总调度：大纲解析完成，共 N 章工作流。第1章《标题》……」, then an
auto-batch note (2+ chapters, batch_dispatch starts the chain) or the
per-chapter reply hint (single chapter); unparseable analyst output falls
back to the generic completion line.
"""

from __future__ import annotations

import json

_DISPATCHER_LINES = {
    "write": "总调度：写作批次已完成",
    "review": "总调度：审校批次已完成",
    "revise": "总调度：改写批次已完成",
}


def infer_intent(tasks: list[dict[str, object]]) -> str:
    """Graph template type from the task keys (write/review/revise/analyze).

    task_key wins (the pipeline reuses roles across stages); legacy custom
    graphs fall back to role-set matching, and unknown graphs render as a
    generic write batch — the summary must never fail.
    """
    keys = {str(task.get("task_key", "")) for task in tasks}
    keys.discard("")
    from proseforge.application.agents.intent import is_analyze_task_keys

    if is_analyze_task_keys(keys):
        return "analyze"
    if keys and all(key.startswith("review_") for key in keys):
        return "review"
    if keys & {"planner", "character", "scene", "select"}:
        return "write"
    if keys & {"merge", "rewrite", "recheck"}:
        return "revise"
    roles = {str(task.get("role", "")) for task in tasks}
    if roles == {"continuity_reviewer", "adversarial_reviewer"}:
        return "review"
    if roles == {"style_editor", "merge_editor"}:
        return "revise"
    return "write"


def _payload_dict(artifact: dict[str, object]) -> dict[str, object]:
    raw = artifact.get("payload")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _outline_section(artifacts: list[dict[str, object]]) -> list[str]:
    for artifact in artifacts:
        payload = _payload_dict(artifact)
        chapters = payload.get("chapters")
        if isinstance(chapters, list) and chapters:
            lines = [f"# 大纲：{payload.get('title') or '未命名'}"]
            for chapter in chapters:
                if isinstance(chapter, dict):
                    lines.append(f"- {chapter.get('title', '')}：{chapter.get('summary', '')}")
            return lines
    return []


def _scene_sections(artifacts: list[dict[str, object]]) -> list[str]:
    lines: list[str] = []
    for artifact in artifacts:
        payload = _payload_dict(artifact)
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        title = str(payload.get("title") or artifact.get("task_key") or "场景")
        # Render real prose (capped — the full text ships in export.zip);
        # never the artifact preview, which is a raw JSON dump by design.
        lines.append(f"### {title}\n{content.strip()[:500]}")
    return ["## 场景草稿", *lines] if lines else []


def _review_sections(artifacts: list[dict[str, object]]) -> list[str]:
    lines: list[str] = []
    for artifact in artifacts:
        payload = _payload_dict(artifact)
        findings = payload.get("findings")
        if not isinstance(findings, list):
            continue
        task_key = str(artifact.get("task_key") or "评审")
        lines.append(f"### {task_key}（verdict: {payload.get('verdict', '—')}）")
        if payload.get("summary"):
            lines.append(str(payload["summary"]))
        for finding in findings:
            if isinstance(finding, dict):
                lines.append(f"- [{finding.get('severity', '?')}] {finding.get('finding', '')}")
    return ["## 评审发现", *lines] if lines else []


def _revise_sections(artifacts: list[dict[str, object]]) -> list[str]:
    lines: list[str] = []
    for artifact in artifacts:
        payload = _payload_dict(artifact)
        summary = payload.get("summary")
        if summary:
            lines.append(f"### {artifact.get('task_key', '编辑')}\n{summary}")
    return ["## 修改摘要", *lines] if lines else []


# ---------------------------------------------------------------------------
# Analyze-run narrative (analyst artifact -> per-chapter workflow list)
# ---------------------------------------------------------------------------


def _analyze_lines(artifacts: list[dict[str, object]]) -> list[str] | None:
    """Analyst artifact -> per-chapter workflow lines; None when unparsable
    (artifact missing, broken JSON, or no chapters list) so the caller can
    fall back to the generic completion line.

    The analyze_merge fusion artifact is the final workflow — seat artifacts
    (analyze_structure carries raw chapters without the hooks seat's
    enrichment) are only a fallback, same preference as batch_dispatch.
    """
    ordered = sorted(artifacts, key=lambda artifact: 0 if artifact.get("task_key") == "analyze_merge" else 1)
    for artifact in ordered:
        payload = _payload_dict(artifact)
        chapters = payload.get("chapters")
        if not isinstance(chapters, list) or not chapters:
            continue
        entries = [chapter for chapter in chapters if isinstance(chapter, dict)]
        if not entries:
            continue
        lines = [f"总调度：大纲解析完成，共 {len(entries)} 章工作流。"]
        for chapter in entries:
            chapter_no = chapter.get("chapter_no")
            title = str(chapter.get("title") or "未命名")
            lines.append(f"第{chapter_no}章《{title}》" if chapter_no is not None else f"《{title}》")
        if len(entries) >= 2:
            # batch_dispatch picks the run up on COMPLETED and starts the
            # serial per-chapter write-run chain automatically.
            lines.append("已自动开始逐章批量写作（单章失败自动跳过，进度见 run 列表）。")
        else:
            lines.append('逐章写作请回复"写第 1 章"这样的指令。')
        return lines
    return None


# ---------------------------------------------------------------------------
# Write-pipeline narrative (M1: auto review -> auto rewrite -> chapter)
# ---------------------------------------------------------------------------


def _final_draft(artifacts: list[dict[str, object]]) -> dict[str, object] | None:
    """Final SceneDraft-like payload: the rewrite wins over the select
    winner, which wins over the legacy single-scene draft."""
    draft: dict[str, object] | None = None
    for artifact in artifacts:
        payload = _payload_dict(artifact)
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        task_key = str(artifact.get("task_key") or "")
        if task_key == "rewrite":
            return payload
        if task_key == "select":
            draft = payload  # selected winner outranks a raw scene draft
        elif task_key == "scene" and draft is None:
            draft = payload
    return draft


def _top_findings(artifacts: list[dict[str, object]], *, limit: int = 5) -> tuple[int, list[str]]:
    """(total high/medium count, first `limit` rendered lines) across reports."""
    total = 0
    lines: list[str] = []
    for artifact in artifacts:
        payload = _payload_dict(artifact)
        findings = payload.get("findings")
        if not isinstance(findings, list):
            findings = payload.get("issues") or payload.get("risks")
        if not isinstance(findings, list):
            continue
        for finding in findings:
            if isinstance(finding, dict):
                severity = str(finding.get("severity", "")).lower()
                text = str(finding.get("finding", "")).strip()
            else:
                severity, text = "medium", str(finding).strip()
            if severity not in {"high", "medium"} or not text:
                continue
            total += 1
            if len(lines) < limit:
                lines.append(f"- [{severity}] {text}")
    return total, lines


def _write_pipeline_lines(
    tasks: list[dict[str, object]],
    artifacts: list[dict[str, object]],
    gate: dict[str, object] | None,
    chapter: dict[str, object] | None,
) -> list[str]:
    status_by_key = {str(task.get("task_key", "")): str(task.get("status", "")) for task in tasks}
    chapter_no = chapter.get("chapter_no") if chapter else None
    headline = f"总调度：第 {chapter_no} 章流水线完成。" if chapter_no else "总调度：流水线完成。"

    segments: list[str] = []
    draft = _final_draft(artifacts)
    if draft is not None:
        segments.append(f"写作 ✓（{len(str(draft['content']).strip())} 字）")
    else:
        segments.append("写作 ✓")

    finding_total, finding_lines = _top_findings(artifacts)
    show_findings = False
    if gate is None:
        # Legacy/custom runs have no gate event: surface findings when any.
        segments.append(f"发现 {finding_total} 个问题" if finding_total else "审校 ✓")
        show_findings = finding_total > 0
    elif gate.get("passed"):
        segments.append("审校 ✓")
    else:
        segments.append(f"发现 {finding_total} 个问题")
        show_findings = True

    rewrite_status = status_by_key.get("rewrite")
    if rewrite_status == "SKIPPED":
        segments.append("无需改写")
    elif rewrite_status == "SUCCEEDED":
        segments.append("已自动改写 ✓")

    if chapter:
        title = str(chapter.get("title") or (draft or {}).get("title") or "未命名")
        segments.append(f"定稿《{title}》第 {chapter_no} 章")

    lines = [headline, " → ".join(segments)]
    if show_findings and finding_lines:
        lines.extend(finding_lines)
    if gate is not None and gate.get("post_passed") is False:
        reasons = [str(reason) for reason in gate.get("post_reasons") or []]
        lines.append(f"⚠ 带警告交付：{'；'.join(reasons) if reasons else '改写后仍未通过质量门禁'}")
    return lines


def artifact_markdown(artifact: dict[str, object]) -> str:
    """Single-artifact markdown for the zip layout: full content when the
    payload carries it, otherwise the summary/preview fallback."""
    payload = _payload_dict(artifact)
    title = str(payload.get("title") or artifact.get("task_key") or "artifact")
    body = str(payload.get("content") or payload.get("summary") or artifact.get("preview") or "")
    return f"# {title}\n\n{body}".strip() + "\n"


def render_run_summary(
    *,
    intent: str,
    status: str,
    terminal_reason: str | None,
    tasks: list[dict[str, object]],
    artifacts: list[dict[str, object]],
    gate: dict[str, object] | None = None,
    chapter: dict[str, object] | None = None,
) -> str:
    """Markdown written back to the swarm assistant message on run end."""
    if status == "CANCELLED":
        # User-cancelled run: no retry hint (retry from CANCELLED 409s).
        lines = ["总调度：批次已取消。"]
        if terminal_reason:
            lines.append(f"原因：{terminal_reason}")
        return "\n\n".join(lines)

    if status != "COMPLETED":
        failed = [task for task in tasks if task.get("status") == "FAILED"]
        lines = [f"总调度：批次未完成（{status}）。"]
        if terminal_reason:
            lines.append(f"原因：{terminal_reason}")
        for task in failed:
            error = task.get("last_error") or ""
            lines.append(f"- 任务 {task.get('task_key', '')} 失败{('：' + str(error)) if error else ''}")
        lines.append("可在 run 详情页重试（retry）脱困。")
        return "\n\n".join(lines)

    if intent == "write":
        return "\n\n".join(_write_pipeline_lines(tasks, artifacts, gate, chapter))

    if intent == "analyze":
        # Chapter-list writeback from the analyst artifact; broken/missing
        # output falls back to the generic completion line (never mentions
        # review/rewrite — the analyze graph has neither).
        analyze_lines = _analyze_lines(artifacts)
        if analyze_lines is not None:
            return "\n\n".join(analyze_lines)
        return "总调度：批次已完成"

    lines = [_DISPATCHER_LINES.get(intent, "总调度：批次已完成")]
    if intent == "review":
        lines.extend(_review_sections(artifacts))
    elif intent == "revise":
        lines.extend(_revise_sections(artifacts))
    else:
        lines.extend(_outline_section(artifacts))
        lines.extend(_scene_sections(artifacts))
        lines.extend(_review_sections(artifacts))
    if len(lines) == 1:
        lines.append("（无产出内容）")
    return "\n\n".join(line for line in lines if line)

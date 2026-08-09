"""run_summary: deterministic markdown rendering per intent + failure.

Write-pipeline runs render the auto-advance narrative (写作 -> 审校 ->
改写 -> 定稿); review/revise keep their section layouts. No "reply to
continue" next-step prompts anywhere (NEXT_STEP_LINES removed).
"""

from __future__ import annotations

import json

from proseforge.application.agents.run_summary import (
    artifact_markdown,
    infer_intent,
    render_run_summary,
)

WRITE_TASKS = [
    {"task_key": "planner", "role": "chief_planner", "status": "SUCCEEDED", "last_error": None},
    {"task_key": "scene", "role": "scene_writer", "status": "SUCCEEDED", "last_error": None},
    {"task_key": "review", "role": "continuity_reviewer", "status": "SUCCEEDED", "last_error": None},
]
REVIEW_TASKS = [
    {"task_key": "continuity", "role": "continuity_reviewer", "status": "SUCCEEDED", "last_error": None},
    {"task_key": "adversarial", "role": "adversarial_reviewer", "status": "SUCCEEDED", "last_error": None},
]
REVISE_TASKS = [
    {"task_key": "style", "role": "style_editor", "status": "SUCCEEDED", "last_error": None},
    {"task_key": "merge", "role": "merge_editor", "status": "SUCCEEDED", "last_error": None},
]
PIPELINE_TASKS_PASS = [
    {"task_key": "planner", "role": "chief_planner", "status": "SUCCEEDED", "last_error": None},
    {"task_key": "character", "role": "character_designer", "status": "SUCCEEDED", "last_error": None},
    {"task_key": "scene", "role": "scene_writer", "status": "SUCCEEDED", "last_error": None},
    {"task_key": "review_continuity", "role": "continuity_reviewer", "status": "SUCCEEDED", "last_error": None},
    {"task_key": "review_adversarial", "role": "adversarial_reviewer", "status": "SUCCEEDED", "last_error": None},
    {"task_key": "review_style", "role": "style_editor", "status": "SUCCEEDED", "last_error": None},
    {"task_key": "merge", "role": "merge_editor", "status": "SKIPPED", "last_error": None},
    {"task_key": "rewrite", "role": "chief_editor", "status": "SKIPPED", "last_error": None},
    {"task_key": "recheck", "role": "continuity_reviewer", "status": "SKIPPED", "last_error": None},
]
PIPELINE_TASKS_REWRITE = [dict(task, status="SUCCEEDED") for task in PIPELINE_TASKS_PASS]

OUTLINE_ARTIFACT = {"task_key": "planner", "artifact_type": "candidate", "preview": "大纲", "payload": json.dumps({"title": "烛龙传", "chapters": [{"title": "第一章", "summary": "相遇"}, {"title": "第二章", "summary": "冲突"}]}, ensure_ascii=False)}
SCENE_ARTIFACT = {"task_key": "scene", "artifact_type": "candidate", "preview": "雨夜相遇的正文…", "payload": json.dumps({"title": "雨夜", "content": "长正文"}, ensure_ascii=False)}
REWRITE_ARTIFACT = {"task_key": "rewrite", "artifact_type": "candidate", "preview": "终稿…", "payload": json.dumps({"title": "雨夜（终稿）", "content": "改写后的终稿正文"}, ensure_ascii=False)}
REVIEW_ARTIFACT = {"task_key": "review_continuity", "artifact_type": "report", "preview": "评审", "payload": json.dumps({"summary": "总体可", "verdict": "WARNING", "findings": [{"finding": "时间线矛盾", "severity": "high"}, {"finding": "称谓不一致", "severity": "medium"}, {"finding": "口头禅略多", "severity": "low"}]}, ensure_ascii=False)}
STYLE_ARTIFACT = {"task_key": "review_style", "artifact_type": "report", "preview": "", "payload": json.dumps({"summary": "句式偏长，建议拆分"}, ensure_ascii=False)}
CHAPTER_INFO = {"chapter_id": "ch-1", "chapter_no": 3, "version_id": "v-1", "title": "雨夜"}


def test_infer_intent():
    assert infer_intent(WRITE_TASKS) == "write"
    assert infer_intent(REVIEW_TASKS) == "review"
    assert infer_intent(REVISE_TASKS) == "revise"
    assert infer_intent([{"role": "custom_role"}]) == "write"  # unknown graphs render as write


def test_infer_intent_pipeline_task_keys():
    assert infer_intent(PIPELINE_TASKS_PASS) == "write"
    review_graph = [{"task_key": key} for key in ("review_continuity", "review_adversarial", "review_style")]
    assert infer_intent(review_graph) == "review"
    revise_graph = [{"task_key": key} for key in ("merge", "rewrite", "recheck")]
    assert infer_intent(revise_graph) == "revise"


def test_infer_intent_production_write_graph():
    # Production intent.py graph: scene_a/b/c parallel drafts + select winner.
    production_graph = [
        {"task_key": key}
        for key in ("planner", "character", "scene_a", "scene_b", "scene_c", "select", "review_continuity", "review_adversarial", "review_style", "merge", "rewrite", "recheck")
    ]
    assert infer_intent(production_graph) == "write"
    # select alone also marks a write graph (no legacy "scene" key exists).
    assert infer_intent([{"task_key": "scene_a"}, {"task_key": "select"}]) == "write"


def test_write_pipeline_gate_pass_summary():
    text = render_run_summary(
        intent="write", status="COMPLETED", terminal_reason=None,
        tasks=PIPELINE_TASKS_PASS, artifacts=[OUTLINE_ARTIFACT, SCENE_ARTIFACT],
        gate={"passed": True, "reasons": []}, chapter=CHAPTER_INFO,
    )
    assert text.startswith("总调度：第 3 章流水线完成。")
    assert "写作 ✓（3 字）" in text
    assert "审校 ✓" in text
    assert "无需改写" in text
    assert "定稿《雨夜》第 3 章" in text
    # 询问式文案已移除
    assert "进入审校" not in text and "进入改写" not in text and "下一步" not in text


SELECT_ARTIFACT = {"task_key": "select", "artifact_type": "candidate", "preview": "择优终稿…", "payload": json.dumps({"title": "雨夜", "content": "择优后的Winner正文"}, ensure_ascii=False)}


def test_write_pipeline_final_draft_prefers_select_winner():
    # Production graph: no "scene" task — the select artifact is the final
    # draft when the revise stage was SKIPPED.
    text = render_run_summary(
        intent="write", status="COMPLETED", terminal_reason=None,
        tasks=PIPELINE_TASKS_PASS, artifacts=[SELECT_ARTIFACT],
        gate={"passed": True, "reasons": []}, chapter=CHAPTER_INFO,
    )
    assert f"写作 ✓（{len('择优后的Winner正文')} 字）" in text
    assert "定稿《雨夜》第 3 章" in text


def test_write_pipeline_rewrite_still_wins_over_select():
    text = render_run_summary(
        intent="write", status="COMPLETED", terminal_reason=None,
        tasks=PIPELINE_TASKS_REWRITE, artifacts=[SELECT_ARTIFACT, REWRITE_ARTIFACT],
        gate={"passed": False, "reasons": ["字数不足：100 < 2500"], "post_passed": True, "post_reasons": []},
        chapter=CHAPTER_INFO,
    )
    assert "写作 ✓（8 字）" in text  # rewrite 产物（改写后的终稿正文）
    assert "已自动改写 ✓" in text


def test_write_pipeline_rewrite_summary_lists_findings():
    text = render_run_summary(
        intent="write", status="COMPLETED", terminal_reason=None,
        tasks=PIPELINE_TASKS_REWRITE, artifacts=[SCENE_ARTIFACT, REWRITE_ARTIFACT, REVIEW_ARTIFACT],
        gate={"passed": False, "reasons": ["字数不足：800 < 2500"], "post_passed": True, "post_reasons": []},
        chapter=CHAPTER_INFO,
    )
    assert "审校 ✓" not in text
    assert "发现 2 个问题" in text  # low severity 不计入
    assert "- [high] 时间线矛盾" in text
    assert "- [medium] 称谓不一致" in text
    assert "口头禅" not in text  # low 不列出
    assert "已自动改写 ✓" in text
    # 终稿字数取 rewrite 产物（8 字），title 取章节回写
    assert "写作 ✓（8 字）" in text
    assert "定稿《雨夜》第 3 章" in text
    assert "⚠" not in text  # 改写后复判通过：不带警告


def test_write_pipeline_warning_delivery_when_post_gate_fails():
    text = render_run_summary(
        intent="write", status="COMPLETED", terminal_reason=None,
        tasks=PIPELINE_TASKS_REWRITE, artifacts=[REWRITE_ARTIFACT, REVIEW_ARTIFACT],
        gate={"passed": False, "reasons": ["字数不足：800 < 2500"], "post_passed": False, "post_reasons": ["字数不足：100 < 2500", "评审发现 1 条有证据的 high 问题"]},
        chapter=CHAPTER_INFO,
    )
    assert "⚠ 带警告交付：字数不足：100 < 2500；评审发现 1 条有证据的 high 问题" in text


def test_write_legacy_graph_without_gate_still_surfaces_findings():
    text = render_run_summary(intent="write", status="COMPLETED", terminal_reason=None, tasks=WRITE_TASKS, artifacts=[OUTLINE_ARTIFACT, SCENE_ARTIFACT, REVIEW_ARTIFACT])
    assert text.startswith("总调度：流水线完成。")
    assert "发现 2 个问题" in text
    assert "- [high] 时间线矛盾" in text


def test_review_summary_lists_findings():
    text = render_run_summary(intent="review", status="COMPLETED", terminal_reason=None, tasks=REVIEW_TASKS, artifacts=[REVIEW_ARTIFACT])
    assert text.startswith("总调度：审校批次已完成")
    assert "verdict: WARNING" in text
    assert "[high] 时间线矛盾" in text
    assert "改写本次内容" not in text and "下一步" not in text


def test_revise_summary_renders_merge_notes():
    text = render_run_summary(intent="revise", status="COMPLETED", terminal_reason=None, tasks=REVISE_TASKS, artifacts=[STYLE_ARTIFACT])
    assert text.startswith("总调度：改写批次已完成")
    assert "句式偏长" in text
    assert "下一步" not in text


def test_failed_summary_reason_and_retry_hint():
    tasks = [dict(WRITE_TASKS[0]), dict(WRITE_TASKS[1], status="FAILED", last_error="budget exhausted")]
    text = render_run_summary(intent="write", status="FAILED", terminal_reason="task(s) failed without retry", tasks=tasks, artifacts=[])
    assert "批次未完成（FAILED）" in text
    assert "task(s) failed without retry" in text
    assert "scene 失败：budget exhausted" in text
    assert "重试" in text


def test_completed_without_artifacts_still_renders():
    text = render_run_summary(intent="review", status="COMPLETED", terminal_reason=None, tasks=REVIEW_TASKS, artifacts=[])
    assert "（无产出内容）" in text


def test_artifact_markdown_prefers_full_content():
    assert artifact_markdown(SCENE_ARTIFACT) == "# 雨夜\n\n长正文\n"
    assert "句式偏长" in artifact_markdown(STYLE_ARTIFACT)


# ---------------------------------------------------------------------------
# analyze intent (analyst artifact -> per-chapter workflow list)
# ---------------------------------------------------------------------------

ANALYZE_TASKS = [{"task_key": "analyze", "role": "analyst", "status": "SUCCEEDED", "last_error": None}]
ANALYST_ARTIFACT = {
    "task_key": "analyze",
    "artifact_type": "candidate",
    "preview": "大纲解析",
    "payload": json.dumps(
        {
            "title": "烛龙传",
            "total_chapters": 3,
            "chapters": [
                {"chapter_no": 1, "title": "风起", "summary": "相遇", "hooks": "玉佩来历", "target_words": "2500-3500"},
                {"chapter_no": 2, "title": "云涌", "summary": "冲突", "hooks": "回收玉佩", "target_words": "2500-3500"},
                {"chapter_no": 3, "title": "惊雷", "summary": "决战", "hooks": "新悬念", "target_words": "3000-4000"},
            ],
        },
        ensure_ascii=False,
    ),
}


def test_infer_intent_analyze():
    assert infer_intent(ANALYZE_TASKS) == "analyze"


def test_analyze_summary_lists_full_chapter_workflow():
    text = render_run_summary(intent="analyze", status="COMPLETED", terminal_reason=None, tasks=ANALYZE_TASKS, artifacts=[ANALYST_ARTIFACT])
    assert text.startswith("总调度：大纲解析完成，共 3 章工作流。")
    # Every chapter listed, one per line.
    assert "\n第1章《风起》\n" in text
    assert "\n第2章《云涌》\n" in text
    assert "第3章《惊雷》" in text
    assert "已自动开始逐章批量写作（单章失败自动跳过，进度见 run 列表）。" in text
    # No review/rewrite stage vocabulary in an analyze writeback.
    assert "进入审校" not in text and "进入改写" not in text


def test_analyze_summary_falls_back_when_artifact_missing():
    text = render_run_summary(intent="analyze", status="COMPLETED", terminal_reason=None, tasks=ANALYZE_TASKS, artifacts=[])
    assert text == "总调度：批次已完成"


def test_analyze_summary_falls_back_on_broken_payload():
    broken = {"task_key": "analyze", "artifact_type": "candidate", "preview": "", "payload": "{not json"}
    text = render_run_summary(intent="analyze", status="COMPLETED", terminal_reason=None, tasks=ANALYZE_TASKS, artifacts=[broken])
    assert text == "总调度：批次已完成"


# ---------------------------------------------------------------------------
# 分析三席位图（analyze_structure/cast/hooks -> analyze_merge）
# ---------------------------------------------------------------------------

ANALYZE_SEAT_TASKS = [
    {"task_key": "analyze_structure", "role": "analyst", "status": "SUCCEEDED", "last_error": None},
    {"task_key": "analyze_cast", "role": "analyst", "status": "SUCCEEDED", "last_error": None},
    {"task_key": "analyze_hooks", "role": "analyst", "status": "SUCCEEDED", "last_error": None},
    {"task_key": "analyze_merge", "role": "merge_editor", "status": "SUCCEEDED", "last_error": None},
]


def test_infer_intent_analyze_three_seats():
    assert infer_intent(ANALYZE_SEAT_TASKS) == "analyze"


def test_analyze_summary_prefers_merge_artifact():
    """席位 artifact 也带 chapters（结构席位的原始 chapters）：渲染取融合产出。"""
    seat = {
        "task_key": "analyze_structure",
        "artifact_type": "candidate",
        "preview": "",
        "payload": json.dumps({"title": "席位稿", "chapters": [{"chapter_no": 1, "title": "未融合", "summary": "s"}]}, ensure_ascii=False),
    }
    merged = {
        "task_key": "analyze_merge",
        "artifact_type": "candidate",
        "preview": "",
        "payload": json.dumps({"title": "融合稿", "chapters": [{"chapter_no": 1, "title": "已融合", "summary": "s", "hooks": "玉佩来历"}]}, ensure_ascii=False),
    }
    text = render_run_summary(intent="analyze", status="COMPLETED", terminal_reason=None, tasks=ANALYZE_SEAT_TASKS, artifacts=[seat, merged])
    assert "第1章《已融合》" in text
    assert "未融合" not in text

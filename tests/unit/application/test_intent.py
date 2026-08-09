"""classify_intent: all four branches + precedence; graph_for_intent templates."""

from __future__ import annotations

import pytest

from proseforge.application.agents.intent import classify_intent, graph_for_intent


@pytest.mark.parametrize("text", ["审校第三章", "帮我审查一下正文", "评审这份稿子", "检查第一章内容"])
def test_review_intent(text):
    assert classify_intent(text) == "review"


@pytest.mark.parametrize("text", ["改写第二章", "重写这段", "润色一下", "修改正文"])
def test_revise_intent(text):
    assert classify_intent(text) == "revise"


@pytest.mark.parametrize("text", ["开始写作", "写第三章", "续写吧", "写一章", "写几章", "帮我写个开头", "起草大纲", "创作一个故事", "给我个大纲"])
def test_write_intent(text):
    assert classify_intent(text) == "write"


@pytest.mark.parametrize("text", ["今天天气不错", "你觉得主角该怎么办", "你好", "聊聊设定"])
def test_chat_intent(text):
    assert classify_intent(text) == "chat"


def test_review_outranks_revise_and_write():
    # "审校并改写第三章" contains revise + write keywords but is a review.
    assert classify_intent("审校并改写第三章") == "review"


def test_revise_outranks_write():
    assert classify_intent("改写第三章") == "revise"


def test_write_graph_template():
    graph = graph_for_intent("write")
    assert [(item["id"], item["role"]) for item in graph] == [
        ("planner", "chief_planner"),
        ("character", "character_designer"),
        ("promise_contract", "promise_keeper"),
        ("scene_a", "scene_writer"),
        ("scene_b", "scene_writer"),
        ("scene_c", "scene_writer"),
        ("scene_d", "scene_writer"),
        ("select", "merge_editor"),
        ("review_continuity", "continuity_reviewer"),
        ("review_adversarial", "adversarial_reviewer"),
        ("review_style", "style_editor"),
        ("review_council", "merge_editor"),
        ("merge", "merge_editor"),
        ("rewrite", "chief_editor"),
        ("recheck", "continuity_reviewer"),
        ("promise_verify", "promise_keeper"),
        ("promise_register", "promise_keeper"),
    ]
    deps = {item["id"]: item["depends_on"] for item in graph}
    assert deps["planner"] == []
    assert deps["character"] == ["planner"]
    # 承诺契约卡在角色卡之后、四份场景草稿之前；草稿等待契约卡以注入 goal_hint
    assert deps["promise_contract"] == ["character"]
    # 四份草稿并联（scene_d 为人味写作席位），全部依赖 character + promise_contract
    assert deps["scene_a"] == ["character", "promise_contract"]
    assert deps["scene_b"] == ["character", "promise_contract"]
    assert deps["scene_c"] == ["character", "promise_contract"]
    assert deps["scene_d"] == ["character", "promise_contract"]
    # 择优依赖四份草稿；审校三任务并联，全部只依赖 select
    assert deps["select"] == ["scene_a", "scene_b", "scene_c", "scene_d"]
    assert deps["review_continuity"] == ["select"]
    assert deps["review_adversarial"] == ["select"]
    assert deps["review_style"] == ["select"]
    # 评审合议（约翰逊协作化）：三评审之后、merge 之前，裁定冲突组
    assert deps["review_council"] == ["review_continuity", "review_adversarial", "review_style"]
    assert deps["merge"] == ["review_council"]
    assert deps["rewrite"] == ["merge"]
    assert deps["recheck"] == ["rewrite"]
    # 承诺核对在终审之后，登记在图尾
    assert deps["promise_verify"] == ["recheck"]
    assert deps["promise_register"] == ["promise_verify"]


def test_write_graph_topology_is_legal():
    from proseforge.domain.agents.task_graph import AgentTaskSpec, TaskGraph

    graph = graph_for_intent("write")
    specs = tuple(AgentTaskSpec(id=str(item["id"]), role=str(item["role"]), depends_on=tuple(item["depends_on"])) for item in graph)
    order = TaskGraph(revision=1, tasks=specs).topological_order()
    assert set(order) == {str(item["id"]) for item in graph}
    assert order.index("select") > max(order.index(key) for key in ("scene_a", "scene_b", "scene_c", "scene_d"))


def test_review_graph_template_is_parallel():
    graph = graph_for_intent("review")
    assert [(item["id"], item["role"]) for item in graph] == [
        ("review_continuity", "continuity_reviewer"),
        ("review_adversarial", "adversarial_reviewer"),
        ("review_style", "style_editor"),
    ]
    assert all(item["depends_on"] == [] for item in graph)


def test_revise_graph_template():
    graph = graph_for_intent("revise")
    assert [(item["id"], item["role"]) for item in graph] == [
        ("merge", "merge_editor"), ("rewrite", "chief_editor"), ("recheck", "continuity_reviewer"),
    ]
    assert graph[0]["depends_on"] == []
    assert graph[1]["depends_on"] == ["merge"]
    assert graph[2]["depends_on"] == ["rewrite"]


def test_chat_has_no_graph():
    with pytest.raises(ValueError):
        graph_for_intent("chat")


# ---------------------------------------------------------------------------
# analyze intent (dumped multi-chapter outline)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "大纲：第一章 相遇\n第二章 冲突\n第三章 决战",
    "第1章 开端\n第2章 发展\n第3章 高潮",
    "第一章 第二章 第三章 第四章",
    "第一百章 回归\n第一百零一章 新家\n第102章 尾声",
])
def test_analyze_intent(text):
    # 3+ "第 X 章" chapter marks (digits or Chinese numerals) = a dumped outline.
    assert classify_intent(text) == "analyze"


def test_analyze_outranks_write():
    # Outline text usually carries writing keywords ("写"/"大纲"): chapter
    # marks win over the write patterns.
    assert classify_intent("按这个大纲写：第一章 相遇，第二章 冲突，第三章 决战") == "analyze"
    assert classify_intent("帮我写下面的故事。第一章 第2章 第三章") == "analyze"


def test_single_chapter_mark_stays_write():
    assert classify_intent("写第 3 章") == "write"
    assert classify_intent("写第三章") == "write"


def test_two_chapter_marks_not_analyze():
    assert classify_intent("第一章和第二章是什么关系") == "chat"


def test_analyze_outranks_review_revise_with_enough_chapter_marks():
    # B1: 3+ chapter marks mean the text is a dumped outline — "审校/改写"
    # inside it describes the chapters, it is not a review/revise instruction.
    assert classify_intent("审校这份大纲：第一章 第二章 第三章") == "analyze"
    assert classify_intent("改写一下：第一章 第二章 第三章") == "analyze"


def test_review_revise_keep_precedence_without_chapter_marks():
    assert classify_intent("审校这份稿子") == "review"
    assert classify_intent("改写一下这段") == "revise"
    assert classify_intent("审校并改写第三章") == "review"


# ---------------------------------------------------------------------------
# Anchored write: a write request quoting its outline inline must not be
# flipped to revise by revise keywords inside that outline (production
# failure: "写第3章《阵法黑客》……现场改写符文程序……" classified revise).
# ---------------------------------------------------------------------------


def test_anchored_write_outranks_revise_keywords_in_quoted_outline():
    text = "写第3章《阵法黑客》\n本章大纲：沈砚解析阵法错误，唐临川现场改写符文程序，陆沉舟抵挡污染生物。"
    assert classify_intent(text) == "write"
    text2 = "请从零新写第3章《阵法黑客》正文（约3000字，全新创作章节）。\n本章大纲：唐临川现场改写符文程序。"
    assert classify_intent(text2) == "write"


def test_bare_revise_instructions_stay_revise():
    # The lookbehind keeps genuine revise commands off the anchored-write tier.
    assert classify_intent("改写第三章") == "revise"
    assert classify_intent("重写第5章开头") == "revise"
    assert classify_intent("把第三章润色一下") == "revise"
    assert classify_intent("修改第2章的结局") == "revise"


# ---------------------------------------------------------------------------
# B1: markdown chapter headings ("# 十七、") count toward analyze
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "# 十七、回城\n# 十八、夜谈\n# 十九、决战",
    "## 第3章 开端\n## 第4章 发展\n## 第5章 高潮",
    "### 第十二章.\n### 第十三章.\n### 第十四章.",
    "# 第一章 相遇\n# 第二章 冲突\n# 第三章 决战",
])
def test_markdown_chapter_headings_trigger_analyze(text):
    assert classify_intent(text) == "analyze"


def test_mixed_marks_are_counted_together():
    # Merged counting: inline "第 X 章" marks + markdown headings.
    assert classify_intent("第一章 第二章\n# 三、冲突") == "analyze"


def test_outline_with_revise_keywords_and_write_request_is_analyze():
    # The original failure: a 20-chapter markdown outline containing 修改/改写
    # plus "一口气写完20章" must be analyze, not revise.
    outline = "\n".join(f"# {mark}、章节标题" for mark in ("一", "二", "三", "四", "五"))
    assert classify_intent(f"{outline}\n第十七章需要修改，第十八章要改写。一口气写完20章") == "analyze"


def test_few_markdown_headings_not_analyze():
    assert classify_intent("# 一、项目介绍\n# 二、联系方式") == "chat"


def test_analyze_graph_template():
    # 分析三席位（福尔摩斯协作化）：结构/人物/伏笔三个 analyst 并行 ->
    # analyze_merge（merge_editor 角色）融合成最终逐章工作流。
    graph = graph_for_intent("analyze")
    assert [(item["id"], item["role"]) for item in graph] == [
        ("analyze_structure", "analyst"),
        ("analyze_cast", "analyst"),
        ("analyze_hooks", "analyst"),
        ("analyze_merge", "merge_editor"),
    ]
    deps = {item["id"]: item["depends_on"] for item in graph}
    assert deps["analyze_structure"] == deps["analyze_cast"] == deps["analyze_hooks"] == []
    assert deps["analyze_merge"] == ["analyze_structure", "analyze_cast", "analyze_hooks"]


def test_analyze_graph_topology_is_legal():
    from proseforge.domain.agents.task_graph import AgentTaskSpec, TaskGraph

    graph = graph_for_intent("analyze")
    specs = tuple(AgentTaskSpec(id=str(item["id"]), role=str(item["role"]), depends_on=tuple(item["depends_on"])) for item in graph)
    order = TaskGraph(revision=1, tasks=specs).topological_order()
    assert len(order) == 4
    assert order.index("analyze_merge") > max(order.index(key) for key in ("analyze_structure", "analyze_cast", "analyze_hooks"))


from proseforge.application.agents.intent import (
    orchestrator_intent_prompt,
    parse_intent_answer,
)


@pytest.mark.parametrize("text,expected", [
    ("write", "write"),
    ("Write.", "write"),
    (" REVISE\n", "revise"),
    ("答案是 review", "review"),
    ("analyze", "analyze"),
    ("Analyze.", "analyze"),
    ("答案是 analyze", "analyze"),
    ("chat", "chat"),
    ("我认为应该归类为 CHAT。", "chat"),
])
def test_parse_intent_answer_forms(text, expected):
    assert parse_intent_answer(text) == expected


@pytest.mark.parametrize("text", ["", "不知道", "writer", "reviews", "analysis", "42"])
def test_parse_intent_answer_unparseable(text):
    assert parse_intent_answer(text) is None


def test_orchestrator_prompt_contains_few_shots():
    prompt = orchestrator_intent_prompt()
    assert "write" in prompt and "review" in prompt
    assert "revise" in prompt and "chat" in prompt
    assert "analyze" in prompt


def test_orchestrator_prompt_rereads_persona_file(tmp_path, monkeypatch):
    import os

    from proseforge.settings import get_settings

    persona_path = tmp_path / "chief_planner.md"
    persona_path.write_text("人格 v1", encoding="utf-8")
    monkeypatch.setenv("PROSEFORGE_PERSONAS_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        assert orchestrator_intent_prompt().startswith("人格 v1")
        # Editing the persona file (mtime bump) must be visible on the next
        # call — the prompt is no longer frozen at import time.
        persona_path.write_text("人格 v2", encoding="utf-8")
        os.utime(persona_path, (persona_path.stat().st_atime, persona_path.stat().st_mtime + 10))
        assert orchestrator_intent_prompt().startswith("人格 v2")
    finally:
        get_settings.cache_clear()


def test_orchestrator_prompt_falls_back_when_persona_missing(tmp_path, monkeypatch):
    from proseforge.application.agents.intent import _ORCHESTRATOR_FALLBACK_IDENTITY
    from proseforge.settings import get_settings

    monkeypatch.setenv("PROSEFORGE_PERSONAS_DIR", str(tmp_path))  # 目录存在但无人格文件
    get_settings.cache_clear()
    try:
        assert orchestrator_intent_prompt().startswith(_ORCHESTRATOR_FALLBACK_IDENTITY)
    finally:
        get_settings.cache_clear()


def test_orchestrator_prompt_prefers_orchestrator_persona(tmp_path, monkeypatch):
    from proseforge.settings import get_settings

    (tmp_path / "orchestrator.md").write_text("总调度人格", encoding="utf-8")
    (tmp_path / "chief_planner.md").write_text("主策划人格", encoding="utf-8")
    monkeypatch.setenv("PROSEFORGE_PERSONAS_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        assert orchestrator_intent_prompt().startswith("总调度人格")
    finally:
        get_settings.cache_clear()

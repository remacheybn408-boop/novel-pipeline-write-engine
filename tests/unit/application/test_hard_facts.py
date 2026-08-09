"""硬事实卡（application/agents/hard_facts.py）确定性提取与渲染测试。"""

from __future__ import annotations

from proseforge.application.agents.hard_facts import (
    book_outline_from_run_goal,
    extract_hard_facts,
    render_hard_fact_card,
)


def test_extracts_numeric_facts_with_quantifiers():
    outline = "主角持青铜钥匙解开七道封印，1997年的雨夜回城。十五年后，三名守夜人在此殉职。"
    facts = extract_hard_facts(outline)
    assert "七道封印" in facts  # 中文数字 + 量词「道」+ 紧邻名词
    assert "1997年" in facts  # 阿拉伯数字 + 量词「年」，助词「的」处截断
    assert "十五年后" in facts
    assert "三名守夜人" in facts


def test_numeric_facts_dedup_and_longer_entry_wins():
    facts = extract_hard_facts("七道封印，镇守旧城。七道封印，不可破。这七道，缺一不可。")
    assert facts.count("七道封印") == 1  # 重复出现只列一次
    assert "七道" not in facts  # 已被更长条目覆盖


def test_chapter_no_prefix_is_not_a_hard_fact():
    # 「第N章」是章节序号而非全书硬事实，不提取
    facts = extract_hard_facts("第一章 相遇\n第二章 冲突\n第十二章 决战")
    assert not any("章" in fact for fact in facts)


def test_proper_names_from_structured_line_and_quotes():
    outline = "人物：沈青梧、顾衍、老船夫\n信物是《青铜钥匙》与「双鱼玉佩」。"
    facts = extract_hard_facts(outline)
    assert "专名：沈青梧" in facts
    assert "专名：顾衍" in facts
    assert "专名：青铜钥匙" in facts
    assert "专名：双鱼玉佩" in facts


def test_empty_or_factless_outline_returns_no_card():
    assert render_hard_fact_card("") == ""
    assert extract_hard_facts("雨夜，主角回城。") == []
    assert render_hard_fact_card("雨夜，主角回城。") == ""


def test_book_outline_from_run_goal_chapter_goal():
    # batch_dispatch.chapter_goal 形态：取「全书大纲（…）：」标头之后的段落，
    # 本章自己的「目标字数」行不参与硬事实提取
    goal = (
        "写第3章《云涌》\n目标字数：不少于 3000 字\n"
        "全书大纲（仅作全局设定与伏笔参照，本章只写「写第3章」指定的内容）：\n"
        "七道封印镇守旧城。"
    )
    assert book_outline_from_run_goal(goal) == "七道封印镇守旧城。"


def test_book_outline_from_run_goal_strips_trailing_directive():
    # analyze run goal 形态：尾部指令行剥掉，其余整个 goal 视为大纲
    goal = "七道封印镇守旧城。\n\n---\n\n请严格按照以上大纲，一口气写完全部12章正文。"
    assert book_outline_from_run_goal(goal) == "七道封印镇守旧城。"


def test_render_card_format():
    card = render_hard_fact_card("七道封印镇守旧城，1997年建成。")
    assert card.startswith("本书硬事实（禁止擅改）")
    assert "- 七道封印" in card
    assert "- 1997年" in card

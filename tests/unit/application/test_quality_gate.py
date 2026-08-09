"""quality_gate: deterministic write-pipeline gate (word floor / required
clues / evidenced-high findings), all branches."""

from __future__ import annotations

from proseforge.application.agents.quality_gate import (
    DEFAULT_MIN_WORDS,
    _repetition_findings,
    evaluate_gate,
    parse_min_words,
    parse_required_clues,
)


def _scene(length: int) -> dict:
    return {"title": "t", "content": "字" * length}


def test_parse_min_words_range_lower_bound():
    assert parse_min_words("写第三章，3000-5000字，雨夜回城") == 3000
    assert parse_min_words("篇幅 2000~4000 字") == 2000
    assert parse_min_words("2500—3500字") == 2500
    assert parse_min_words("写 3000 到 5000 字") == 3000


def test_parse_min_words_explicit_target_line():
    # batch_dispatch.chapter_goal 注入的显式目标行，优先级最高
    assert parse_min_words("写第3章《风起》\n本章大纲：相遇\n目标字数：不少于 3000 字") == 3000
    assert parse_min_words("目标字数:2800") == 2800
    assert parse_min_words("目标字数：约 3500 字") == 3500


def test_parse_min_words_explicit_line_beats_range():
    assert parse_min_words("目标字数：不少于 2800 字\n篇幅 3000-5000字") == 2800


def test_parse_min_words_qualified_single_value():
    # 用户口语化单值：「一口气写完全部10章正文（每章约3000字）」
    assert parse_min_words("一口气写完全部10章正文，每章约3000字") == 3000
    assert parse_min_words("不少于2800字") == 2800
    assert parse_min_words("至少 2600 字") == 2600


def test_parse_min_words_bare_number_not_misjudged():
    # 裸「3000字」（无约/不少于/至少/每章限定）不匹配，防大纲正文数字误判
    assert parse_min_words("收到一封3000字密电") == DEFAULT_MIN_WORDS
    assert parse_min_words("第7号档案长达3000字") == DEFAULT_MIN_WORDS


def test_parse_min_words_default():
    assert parse_min_words("写第三章") == DEFAULT_MIN_WORDS == 2500
    assert parse_min_words("") == 2500
    assert parse_min_words("99-999字") == 2500  # 3-5 位数字才匹配


def test_parse_required_clues_full_and_half_width_colon():
    assert parse_required_clues("必须埋入或回收的线索：青铜钥匙。其余") == ["青铜钥匙"]
    assert parse_required_clues("必须埋入或回收的线索:青铜钥匙") == ["青铜钥匙"]
    assert parse_required_clues("必须埋入或回收的线索：青铜钥匙和旧剑。") == ["青铜钥匙和旧剑"]
    assert parse_required_clues("没有线索要求") == []


def test_parse_required_clues_batch_hooks_line():
    # batch_dispatch.chapter_goal 的「伏笔/钩子：」行：指令词过滤，只留线索本体
    assert parse_required_clues("写第2章《云涌》\n伏笔/钩子：埋入：林雪的真实身份") == ["林雪的真实身份"]
    assert parse_required_clues("伏笔/钩子：回收师父的遗言") == ["师父的遗言"]
    # 纯指令/元描述（无线索本体）不产生必含线索
    assert parse_required_clues("伏笔/钩子：本章无新伏笔，延续上文") == []
    # 显式「必须埋入或回收的线索」优先于「伏笔/钩子」行
    assert parse_required_clues("必须埋入或回收的线索：青铜钥匙。\n伏笔/钩子：林雪身份") == ["青铜钥匙"]


def test_gate_fails_when_batch_hook_missing():
    result = evaluate_gate(
        goal="写第2章《云涌》\n伏笔/钩子：埋入：林雪的真实身份",
        scene_payload=_scene(3000),
        review_payloads=[],
    )
    assert result.passed is False
    assert any("林雪的真实身份" in reason for reason in result.reasons)


def test_gate_passes_when_batch_hook_present():
    result = evaluate_gate(
        goal="写第2章《云涌》\n伏笔/钩子：埋入：林雪的真实身份",
        scene_payload={"title": "t", "content": "字" * 2600 + "林雪的真实身份"},
        review_payloads=[],
    )
    assert result.passed is True


def test_gate_passes_when_all_clear():
    result = evaluate_gate(
        goal="写第三章，2500-4000字。必须埋入或回收的线索：青铜钥匙。",
        scene_payload={"content": "钥" * 2600 + "青铜钥匙"},
        review_payloads=[{"findings": [{"severity": "medium", "evidence_spans": [{"quote": "x"}]}]}],
    )
    assert result.passed is True
    assert result.reasons == []


def test_gate_fails_on_word_floor():
    result = evaluate_gate(goal="写第三章，3000-5000字", scene_payload=_scene(800), review_payloads=[])
    assert result.passed is False
    assert result.reasons == ["字数不足：800 < 3000"]


def test_gate_default_floor_2500():
    assert evaluate_gate(goal="写第三章", scene_payload=_scene(2499), review_payloads=[]).passed is False
    assert evaluate_gate(goal="写第三章", scene_payload=_scene(2500), review_payloads=[]).passed is True


def test_gate_fails_when_clue_missing():
    result = evaluate_gate(goal="必须埋入或回收的线索：青铜钥匙。", scene_payload=_scene(3000), review_payloads=[])
    assert result.passed is False
    assert any("青铜钥匙" in reason for reason in result.reasons)


def test_gate_clue_fuzzy_hit_punctuation_broken():
    # 5172 第 14 章实证冤案：正文写「雾江七号，三号货舱」，精确匹配漏判
    result = evaluate_gate(
        goal="写第14章《罗湾西岸》\n伏笔/钩子：回收雾江七号三号货舱；埋入货轮大副\n目标字数：不少于 3000 字",
        scene_payload={"title": "t", "content": "字" * 3000 + "他找到雾江七号，在三号货舱的木板下。山城货轮的大副姓吴。"},
        review_payloads=[],
    )
    assert result.passed is True
    assert result.reasons == []


def test_gate_clue_fuzzy_hit_split_fragments():
    # 片段允许分处出现：「货轮」「大副」各自命中「货轮大副」
    result = evaluate_gate(
        goal="伏笔/钩子：埋入货轮大副",
        scene_payload={"title": "t", "content": "字" * 2600 + "他登上货轮，大副递来一支烟。"},
        review_payloads=[],
    )
    assert result.passed is True


def test_gate_clue_fuzzy_miss_low_coverage():
    # 覆盖率不足仍判未命中：7 字线索只出现 2 字片段（2/7 < 0.6）
    result = evaluate_gate(
        goal="伏笔/钩子：埋入货轮大副的密令",
        scene_payload={"title": "t", "content": "字" * 2600 + "远处一艘货轮驶过。"},
        review_payloads=[],
    )
    assert result.passed is False
    assert any("货轮大副的密令" in reason for reason in result.reasons)


def test_gate_ignores_evidence_less_high_findings():
    # high without evidence_spans: hallucination-prone, does not block.
    result = evaluate_gate(
        goal="",
        scene_payload=_scene(3000),
        review_payloads=[{"findings": [{"severity": "high", "evidence_spans": []}, {"severity": "high"}]}],
    )
    assert result.passed is True


def test_gate_fails_on_evidenced_high_findings():
    result = evaluate_gate(
        goal="",
        scene_payload=_scene(3000),
        review_payloads=[
            {"issues": [{"severity": "high", "evidence_spans": [{"quote": "矛盾"}]}]},
            {"risks": [{"severity": "HIGH", "evidence_spans": [{"quote": "崩"}]}]},
        ],
    )
    assert result.passed is False
    assert result.reasons == ["评审发现 2 条有证据的 high 问题"]


def test_gate_missing_scene_always_fails():
    result = evaluate_gate(goal="", scene_payload=None, review_payloads=[])
    assert result.passed is False
    assert result.reasons == ["scene missing"]


def test_gate_collects_multiple_reasons():
    result = evaluate_gate(
        goal="3000-5000字。必须埋入或回收的线索：青铜钥匙。",
        scene_payload=_scene(100),
        review_payloads=[{"findings": [{"severity": "high", "evidence_spans": [{"quote": "x"}]}]}],
    )
    assert result.passed is False
    # 单条 evidenced high 降级为警告，不再计入阻断原因
    assert len(result.reasons) == 2
    assert len(result.warnings) == 1


# ---------------------------------------------------------------------------
# 单 high 降级：evidenced high >= GATE_EVIDENCED_HIGH_THRESHOLD(2) 才卡章；
# 1 条放行但记录警告（进 gate.evaluated 事件的 warnings 字段）
# ---------------------------------------------------------------------------


def test_gate_passes_on_single_evidenced_high_with_warning():
    result = evaluate_gate(
        goal="",
        scene_payload=_scene(3000),
        review_payloads=[{"findings": [{"severity": "high", "evidence_spans": [{"quote": "矛盾"}]}]}],
    )
    assert result.passed is True
    assert result.reasons == []
    assert result.warnings == ["评审发现 1 条有证据的 high 问题（低于阻断阈值 2，放行并记录）"]


def test_gate_fails_on_two_evidenced_highs():
    result = evaluate_gate(
        goal="",
        scene_payload=_scene(3000),
        review_payloads=[
            {"findings": [{"severity": "high", "evidence_spans": [{"quote": "矛盾"}]}, {"severity": "high", "evidence_spans": [{"quote": "崩"}]}]},
        ],
    )
    assert result.passed is False
    assert result.reasons == ["评审发现 2 条有证据的 high 问题"]
    assert result.warnings == []


def test_gate_passes_without_evidenced_high():
    result = evaluate_gate(
        goal="",
        scene_payload=_scene(3000),
        review_payloads=[{"findings": [{"severity": "medium", "evidence_spans": [{"quote": "x"}]}, {"severity": "low", "evidence_spans": [{"quote": "y"}]}]}],
    )
    assert result.passed is True
    assert result.reasons == []
    assert result.warnings == []


# ---------------------------------------------------------------------------
# 复读检测：CJK n-gram 分档阈值（4字>=8 / 5字>=6 / 6字>=5）；虚字超 1/3
# 的语气串/单字重复不计；长片段命中后其覆盖的短片段不再重复上报
# ---------------------------------------------------------------------------


def test_repetition_normal_text_not_flagged():
    # 无周期重复的 CJK 文（等差取字，周期远大于文长）：不报
    content = "".join(chr(0x4E00 + (index * 37) % 0x2000) for index in range(3000))
    assert _repetition_findings(content) == []


def test_repetition_flagged_at_threshold():
    # 同一 4 字片段在不同语境出现 8 次（达到 4 字档阈值）：报；单字「字」重复不计
    content = "字" * 2600 + "".join(f"{lead}摩挲怀表。" for lead in "他她你我它咱俺某")
    assert _repetition_findings(content) == ["复读超标：「摩挲怀表」出现 8 次"]


def test_repetition_below_threshold_not_flagged():
    # 7 次低于 4 字档阈值（专名高频的正当重复不再误伤）
    content = "字" * 2600 + "".join(f"{lead}摩挲怀表。" for lead in "他她你我它咱俺")
    assert _repetition_findings(content) == []


def test_repetition_function_word_fragments_ignored():
    # 虚字占比超 1/3 的语气串（「他的了是」类）不算复读
    content = "字" * 2600 + "他的了是他的了是他的了是他的了是他的了是他的了是他的了是他的了是"
    assert _repetition_findings(content) == []


def test_repetition_longer_ngram_shadows_shorter():
    # 6 字片段命中（>=5 次）后，其覆盖的 5/4 字子串（「眼底闪过」等）不再重复上报
    content = "字" * 2600 + "眼底闪过一丝。" * 5
    assert _repetition_findings(content) == ["复读超标：「眼底闪过一丝」出现 5 次"]


def test_gate_fails_on_repetition():
    result = evaluate_gate(
        goal="写第3章",
        scene_payload={"title": "t", "content": "字" * 2600 + "".join(f"{lead}摩挲怀表。" for lead in "他她你我它咱俺某")},
        review_payloads=[],
    )
    assert result.passed is False
    assert result.reasons == ["复读超标：「摩挲怀表」出现 8 次"]

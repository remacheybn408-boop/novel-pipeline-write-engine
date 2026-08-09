"""硬事实卡（hard-fact card）：从全书大纲确定性提取不可擅改的硬事实。

背景：复合测试显示写作/改写环节对全书级硬事实（数量设定、年代、专名
写法）漂移。本模块用纯正则/启发式从大纲文本提取「数字+量词」短语与
专名清单，渲染为「本书硬事实（禁止擅改）」文案块，注入 scene_writer
的 goal_hint 与 chief_editor 的改写 prompt（注入点见 role_handlers /
chief_handler）。无大纲或无硬事实时返回空串，调用方跳过注入。
"""

from __future__ import annotations

import re

from proseforge.application.agents.batch_dispatch import book_outline_from_goal

# batch_dispatch.chapter_goal 追加全书大纲段的标头前缀
_BOOK_OUTLINE_SECTION_MARKER = "全书大纲（"

# 数字+量词硬事实：阿拉伯数字或中文数字 + 常见量词（年月日/数量/器物/场次等）
_CN_NUMERAL_CHARS = "零一二三四五六七八九十百千万亿两"
_QUANTIFIERS = (
    "年|月|日|天|夜|岁|载|世|代|纪|"
    "道|个|名|位|条|次|层|场|回|卷|部|章|节|"
    "座|间|栋|艘|柄|把|颗|枚|尺|丈|里|斤|文|块|"
    "种|类|项|级|品|阶|重|环|关|劫|批|群|只|匹"
)
# lookbehind 同时排除中文章节序号：「第十二章」的「二章」不该成为硬事实
_NUMERIC_FACT_PATTERN = re.compile(
    rf"(?<![第{_CN_NUMERAL_CHARS}])(?:\d+(?:\.\d+)?|[{_CN_NUMERAL_CHARS}]+)(?:{_QUANTIFIERS})[一-鿿]{{0,4}}"
)
# 量词后的名词最多补 4 字，遇到助词/标点即截断（「1997年的雨夜」->「1997年」）
_TRAILING_STOP_CHARS = set("的了是在有与及或和而就并又也都还很极最不已无没之其于以及将要把被让给从向往用以为到")

# 专名清单：大纲里的结构化人物/设定行 + 《》/「」引用短语
_NAME_LINE_PATTERN = re.compile(r"^(?:主要人物|登场人物|人物|角色|主角|设定)\s*[:：]\s*(.+)$", re.MULTILINE)
_QUOTED_NAME_PATTERN = re.compile(r"《([^《》\n]{1,16})》|「([^「」\n]{1,16})」")
_NAME_SPLIT_PATTERN = re.compile(r"[、,，/／;；\s]+")

HARD_FACT_MAX_ITEMS = 40  # 卡片条数上限，防超长大纲撑爆 prompt

_CARD_HEADER = "本书硬事实（禁止擅改）：以下事实取自全书大纲，正文必须与之一致，禁止擅改数量、年代与专名写法。"


def book_outline_from_run_goal(goal: str) -> str:
    """run goal -> 全书大纲文本（硬事实提取的语料）。

    章节写作 goal（batch_dispatch.chapter_goal）在末尾以「全书大纲（…）：」
    标头追加全书大纲，取该段（本章自己的目标字数/伏笔行不参与硬事实提取，
    避免把单章字数当成全书硬事实）；其他 goal 回退
    batch_dispatch.book_outline_from_goal（去掉尾部指令行，整个 goal 视为大纲）。
    """
    text = (goal or "").strip()
    if not text:
        return ""
    marker_index = text.find(_BOOK_OUTLINE_SECTION_MARKER)
    if marker_index >= 0:
        header_end = text.find("\n", marker_index)
        if header_end >= 0:
            return text[header_end + 1 :].strip()
        return ""
    return book_outline_from_goal(text)


def _extract_numeric_facts(outline: str) -> list[str]:
    """「数字+量词」短语（可带紧邻名词）：七道封印 / 1997年 / 十五年。"""
    facts: list[str] = []
    for match in _NUMERIC_FACT_PATTERN.finditer(outline):
        candidate = match.group(0)
        # 名词部分在助词/标点处截断
        cut = next((index for index, char in enumerate(candidate) if char in _TRAILING_STOP_CHARS), len(candidate))
        candidate = candidate[:cut]
        if not candidate:
            continue
        if any(candidate in existing for existing in facts):
            continue  # 去重：与既有条目相同或为其子串
        # 既有条目是新候选的子串时，用更长的候选替换（「七道」让位「七道封印」）
        facts = [existing for existing in facts if existing not in candidate]
        facts.append(candidate)
    return facts


def _extract_proper_names(outline: str) -> list[str]:
    """专名清单：结构化人物/设定行直接拆分；《》/「」引用短语作启发式补充。"""
    names: list[str] = []

    def _add(candidate: str) -> None:
        candidate = candidate.strip()
        if not candidate or len(candidate) > 16:
            return
        if any(candidate in existing for existing in names):
            return
        names.append(candidate)

    for line in _NAME_LINE_PATTERN.findall(outline):
        for token in _NAME_SPLIT_PATTERN.split(line):
            _add(token)
    for book_title, quoted in _QUOTED_NAME_PATTERN.findall(outline):
        _add(book_title or quoted)
    return names


def extract_hard_facts(outline: str) -> list[str]:
    """大纲文本 -> 有序去重的硬事实条目（数字事实在前，专名以「专名：」前缀殿后）。"""
    text = (outline or "").strip()
    if not text:
        return []
    facts = _extract_numeric_facts(text)
    facts.extend(f"专名：{name}" for name in _extract_proper_names(text))
    return facts[:HARD_FACT_MAX_ITEMS]


def render_hard_fact_card(goal_or_outline: str, *, is_run_goal: bool = True) -> str:
    """渲染注入用硬事实卡；无大纲或无硬事实返回 ""。

    ``is_run_goal=True``（默认）时先经 book_outline_from_run_goal 解析出
    全书大纲段；直接给大纲文本传 False。
    """
    outline = book_outline_from_run_goal(goal_or_outline) if is_run_goal else (goal_or_outline or "").strip()
    facts = extract_hard_facts(outline)
    if not facts:
        return ""
    lines = [_CARD_HEADER]
    lines.extend(f"- {fact}" for fact in facts)
    return "\n".join(lines)

"""Character name/alias matcher: CJK substring, Latin word boundaries,
longest-match dedup, case-insensitivity, mixed text."""

from __future__ import annotations

from dataclasses import dataclass, field

from proseforge.domain.characters.matching import match_characters


@dataclass
class FakeCharacter:
    id: str
    name: str
    aliases: list[str] = field(default_factory=list)


def _chars(*specs: tuple[str, ...]) -> list[FakeCharacter]:
    return [FakeCharacter(id=f"c{i}", name=spec[0], aliases=list(spec[1:])) for i, spec in enumerate(specs)]


def test_cjk_substring_match():
    characters = _chars(("李雷",), ("韩梅梅",))
    matched = match_characters("李雷走进教室，看见韩梅梅在读书。", characters)
    assert [c.name for c in matched] == ["李雷", "韩梅梅"]


def test_alias_hit():
    characters = _chars(("烛龙", "老龙", "钟山之神"))
    matched = match_characters("没人见过老龙睁眼。", characters)
    assert [c.id for c in matched] == ["c0"]


def test_latin_word_boundary_no_partial_hit():
    characters = _chars(("Ann",), ("Anna",))
    # "Ann" must not hit inside "Anna"; "Hannah" contains "Anna" but not on
    # word boundaries either.
    matched = match_characters("Anna talked to Hannah.", characters)
    assert [c.name for c in matched] == ["Anna"]


def test_latin_case_insensitive():
    characters = _chars(("Sherlock", "holmes"))
    matched = match_characters("SHERLOCK and Holmes arrived.", characters)
    assert [c.id for c in matched] == ["c0"]


def test_longest_match_wins_on_overlap():
    characters = _chars(("李",), ("李雷",))
    matched = match_characters("李雷来了", characters)
    assert [c.name for c in matched] == ["李雷"]


def test_alias_shadows_shorter_name():
    characters = _chars(("龙", "烛龙"))
    matched = match_characters("烛龙睁眼为昼", characters)
    assert [c.id for c in matched] == ["c0"]


def test_mixed_cjk_latin_text():
    characters = _chars(("李雷",), ("Bob", "小鲍勃"))
    matched = match_characters("李雷和 bob 打了个赌，小鲍勃输了。", characters)
    assert [c.name for c in matched] == ["李雷", "Bob"]


def test_order_by_first_match_position():
    characters = _chars(("乙",), ("甲",))
    matched = match_characters("甲先出场，乙随后。", characters)
    assert [c.name for c in matched] == ["甲", "乙"]


def test_no_match_returns_empty():
    characters = _chars(("路人甲",))
    assert match_characters("什么都没有。", characters) == []
    assert match_characters("", characters) == []
    assert match_characters("text", []) == []


def test_blank_terms_ignored():
    characters = [FakeCharacter(id="c0", name="  ", aliases=["", "小明"])]
    matched = match_characters("小明出现了", characters)
    assert [c.id for c in matched] == ["c0"]

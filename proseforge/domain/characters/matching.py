"""Character name/alias matching against chapter text.

Pure domain logic, no DB. Rules:
- a character's name and every alias participate;
- terms containing CJK characters match as plain substrings;
- pure-Latin terms match on word boundaries (no partial-word hits);
- matching is case-insensitive (Latin; CJK has no case);
- longest match wins on overlapping spans (a longer alias shadows a
  shorter name covering the same text).

Returns characters ordered by first match position.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


class Matchable(Protocol):
    id: str
    name: str
    aliases: list[str]


_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")


@dataclass(frozen=True)
class _Match:
    start: int
    end: int
    character_index: int


def _term_spans(text_lower: str, term: str) -> list[tuple[int, int]]:
    needle = term.strip().lower()
    if not needle:
        return []
    if _CJK_RE.search(needle):
        spans: list[tuple[int, int]] = []
        start = text_lower.find(needle)
        while start != -1:
            spans.append((start, start + len(needle)))
            start = text_lower.find(needle, start + 1)
        return spans
    pattern = re.compile(r"(?<![a-z0-9_])" + re.escape(needle) + r"(?![a-z0-9_])")
    return [(m.start(), m.end()) for m in pattern.finditer(text_lower)]


def match_characters(text: str, characters: list[Matchable]) -> list[Matchable]:
    """Return the characters whose name/alias appears in text."""
    if not text or not characters:
        return []
    text_lower = text.lower()
    matches: list[_Match] = []
    for index, character in enumerate(characters):
        for term in [character.name, *character.aliases]:
            for start, end in _term_spans(text_lower, term):
                matches.append(_Match(start, end, index))
    if not matches:
        return []
    # Longest match wins: drop spans contained in (or overlapping) a longer
    # accepted span. Ties keep the earliest occurrence.
    accepted: list[_Match] = []
    for candidate in sorted(matches, key=lambda m: (-(m.end - m.start), m.start)):
        if any(candidate.start < taken.end and taken.start < candidate.end for taken in accepted):
            continue
        accepted.append(candidate)
    first_position: dict[int, int] = {}
    for match in accepted:
        first_position[match.character_index] = min(match.start, first_position.get(match.character_index, match.start))
    ordered_indexes = sorted(first_position, key=lambda i: first_position[i])
    return [characters[i] for i in ordered_indexes]

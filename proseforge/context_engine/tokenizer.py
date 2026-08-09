from __future__ import annotations

from typing import Protocol


class Tokenizer(Protocol):
    def count(self, text: str) -> int: ...


class ConservativeTokenizer:
    def count(self, text: str) -> int:
        # CJK characters are deliberately counted at one token each to avoid under-budgeting.
        return sum(1 if "\u4e00" <= char <= "\u9fff" else 0.25 for char in text).__ceil__()

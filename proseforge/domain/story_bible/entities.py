from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from proseforge.domain.common.ids import new_id

VALID_KINDS = {
    "character", "relationship", "location", "timeline_event", "world_rule",
    "plot_thread", "style_rule", "promise",
    # Auto-extracted state ledger kinds (written by the chapter summarizer,
    # rendered in the scene pack's [当前状态] section):
    # - chapter_fact: per-chapter fact snapshot, key=f"ch{chapter_no}"
    # - character_state: latest per-character emotional/mental state, key=character name
    "chapter_fact", "character_state",
}
PROMISE_STATES = frozenset({"open", "developing", "resolved", "abandoned"})
PROMISE_TRANSITIONS = {
    "open": ("developing",),
    "developing": ("resolved", "abandoned"),
    "resolved": (),
    "abandoned": (),
}
# User-level exclusion control: any kind may be parked at "excluded"
# (orthogonal to the promise lifecycle machine). Retrieval layers must
# only ever see RETRIEVABLE_STATUSES — terminal promise states and
# excluded rows never re-enter the scene pack.
EXCLUDED_STATUS = "excluded"
RETRIEVABLE_STATUSES = frozenset({"active", "open", "developing"})
_VOICE_REQUIRED = frozenset({"sentence_len", "connectors", "banned_words", "emotion_baseline", "register"})
# Optional voice extensions: dialect (e.g. 东北话/川普/粤语) and
# catchphrases (口头禅/名梗). Validated when present, never required.
_VOICE_OPTIONAL = frozenset({"dialect", "catchphrases"})
_VOICE_FIELDS = _VOICE_REQUIRED | _VOICE_OPTIONAL


class StoryFactValidationError(ValueError):
    """Raised when a structured Story Bible fact does not meet its contract."""


def _non_empty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StoryFactValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise StoryFactValidationError(f"{field} must be a list of strings")
    items = [_non_empty_string(item, f"{field}[]") for item in value]
    return list(dict.fromkeys(items))


def _validate_voice(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise StoryFactValidationError("voice must be an object")
    missing = _VOICE_REQUIRED - set(value)
    unknown = set(value) - _VOICE_FIELDS
    if missing:
        raise StoryFactValidationError(f"voice is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise StoryFactValidationError(f"voice has unsupported fields: {', '.join(sorted(unknown))}")

    sentence_len = value["sentence_len"]
    if not isinstance(sentence_len, list) or len(sentence_len) != 2 or any(type(item) is not int for item in sentence_len):
        raise StoryFactValidationError("voice.sentence_len must be [min, max] integers")
    minimum, maximum = sentence_len
    if minimum < 1 or maximum < minimum:
        raise StoryFactValidationError("voice.sentence_len must satisfy 1 <= min <= max")

    normalized: dict[str, object] = {
        "sentence_len": [minimum, maximum],
        "connectors": _string_list(value["connectors"], "voice.connectors"),
        "banned_words": _string_list(value["banned_words"], "voice.banned_words"),
        "emotion_baseline": _non_empty_string(value["emotion_baseline"], "voice.emotion_baseline"),
        "register": _non_empty_string(value["register"], "voice.register"),
    }
    if "dialect" in value:
        normalized["dialect"] = _non_empty_string(value["dialect"], "voice.dialect")
    if "catchphrases" in value:
        normalized["catchphrases"] = _string_list(value["catchphrases"], "voice.catchphrases")
    return normalized


def validate_fact_value(kind: str, key: str, value: Mapping[str, object]) -> dict[str, object]:
    """Return the normalized, JSON-safe structured portion of a Story Bible fact."""
    if not isinstance(value, Mapping):
        raise StoryFactValidationError("value must be an object")

    normalized: dict[str, object] = dict(value)
    triggers = normalized.get("triggers", [key])
    normalized["triggers"] = _string_list(triggers, "triggers")

    budget_tokens = normalized.get("budget_tokens", 256)
    if type(budget_tokens) is not int or not 1 <= budget_tokens <= 200_000:
        raise StoryFactValidationError("budget_tokens must be an integer between 1 and 200000")
    normalized["budget_tokens"] = budget_tokens

    if kind == "character" and "voice" in normalized:
        normalized["voice"] = _validate_voice(normalized["voice"])
    return normalized


@dataclass(frozen=True)
class StoryFact:
    project_id: str
    kind: str
    key: str
    value: dict[str, object]
    pinned: bool = False
    status: str = ""
    id: str = ""
    version: int = 1
    confidence: float = 1.0
    source: str = "user"

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id:
            raise StoryFactValidationError("project_id must be a non-empty string")
        if self.kind not in VALID_KINDS:
            raise StoryFactValidationError("unsupported story bible kind")
        key = _non_empty_string(self.key, "key")
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "value", validate_fact_value(self.kind, key, self.value))
        if type(self.pinned) is not bool:
            raise StoryFactValidationError("pinned must be a boolean")
        if type(self.version) is not int or self.version < 1:
            raise StoryFactValidationError("version must be a positive integer")
        if not isinstance(self.confidence, (int, float)) or not 0 <= float(self.confidence) <= 1:
            raise StoryFactValidationError("confidence must be between 0 and 1")
        if not isinstance(self.source, str) or not self.source.strip():
            raise StoryFactValidationError("source must be a non-empty string")

        if self.kind == "promise":
            status = self.status or "open"
            if status not in PROMISE_STATES | {EXCLUDED_STATUS}:
                raise StoryFactValidationError("unsupported promise status")
        else:
            if self.status not in {"", "active", EXCLUDED_STATUS}:
                raise StoryFactValidationError("only promise facts may have a non-active status")
            status = self.status or "active"
        object.__setattr__(self, "status", status)

    @classmethod
    def create(
        cls,
        project_id: str,
        kind: str,
        key: str,
        value: dict[str, object],
        *,
        pinned: bool = False,
        status: str = "",
        confidence: float = 1.0,
        source: str = "user",
    ) -> StoryFact:
        return cls(project_id, kind, key, value, pinned=pinned, status=status, id=new_id(), confidence=confidence, source=source)

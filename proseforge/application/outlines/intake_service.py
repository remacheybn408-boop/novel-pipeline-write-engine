from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OutlineSpec:
    title: str = ""
    genre: str = ""
    style: str = ""
    protagonist: str = ""
    characters: tuple[str, ...] = ()
    point_of_view: str = ""
    planned_volumes: int | None = None
    planned_chapters: int | None = None
    chapter_word_target: int | None = None
    missing_required_fields: tuple[str, ...] = field(default_factory=tuple)


class OutlineIntakeService:
    REQUIRED = ("title", "genre", "characters", "point_of_view")

    @staticmethod
    def _optional_int(value: object) -> int | None:
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def parse(self, payload: dict[str, object]) -> OutlineSpec:
        spec = OutlineSpec(
            title=str(payload.get("title", "")), genre=str(payload.get("genre", "")), style=str(payload.get("style", "")),
            protagonist=str(payload.get("protagonist", "")), characters=tuple(str(item) for item in payload.get("characters", ()) or ()),
            point_of_view=str(payload.get("point_of_view", "")), planned_volumes=self._optional_int(payload.get("planned_volumes")),
            planned_chapters=self._optional_int(payload.get("planned_chapters")), chapter_word_target=self._optional_int(payload.get("chapter_word_target")),
        )
        missing = tuple(field for field in self.REQUIRED if not getattr(spec, field))
        return OutlineSpec(**{**spec.__dict__, "missing_required_fields": missing})

    def clarification_questions(self, spec: OutlineSpec) -> tuple[str, ...]:
        questions: list[str] = []
        if spec.missing_required_fields:
            questions.extend(f"请补充：{field}" for field in spec.missing_required_fields)
        if spec.planned_volumes is None and spec.planned_chapters is None:
            questions.append("计划写多少卷？每卷多少章，或全书总章节数？")
        if spec.chapter_word_target is None:
            questions.append("单章大约多少字？")
        return tuple(questions)

    def confirm(self, spec: OutlineSpec) -> bool:
        return not self.clarification_questions(spec)

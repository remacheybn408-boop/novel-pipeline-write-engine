from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ProviderModel:
    provider: str
    model_id: str
    display_name: str
    capabilities: dict[str, object]
    context_window: int | None = None
    max_output_tokens: int | None = None
    # Owner of a manual catalog row; None for synced rows and legacy shared
    # manual rows (visible to everyone, undeletable).
    owner_id: str | None = None


@dataclass(frozen=True)
class GenerationRequest:
    model: str
    system_blocks: tuple[dict[str, object], ...]
    input_blocks: tuple[dict[str, object], ...]
    response_schema: dict[str, object] | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    reasoning: dict[str, object] | None = None
    provider_options: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GenerationEvent:
    event: str
    text: str = ""
    data: dict[str, object] = field(default_factory=dict)


class ModelProvider(Protocol):
    provider_id: str

    async def validate_credentials(self) -> dict[str, object]: ...

    async def list_models(self) -> list[ProviderModel]: ...

    async def count_tokens(self, request: GenerationRequest) -> int: ...

    async def stream(
        self,
        request: GenerationRequest,
    ) -> AsyncIterator[GenerationEvent]: ...

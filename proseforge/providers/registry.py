from __future__ import annotations

from proseforge.domain.common.errors import ConflictError, NotFoundError
from proseforge.domain.ports.model_provider import ModelProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ModelProvider] = {}

    def register(self, provider: ModelProvider) -> None:
        if provider.provider_id in self._providers:
            raise ConflictError(f"provider already registered: {provider.provider_id}")
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> ModelProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise NotFoundError(f"provider not registered: {provider_id}") from exc

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

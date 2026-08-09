import pytest

from proseforge.domain.common.errors import ConflictError
from proseforge.providers.registry import ProviderRegistry


class FakeProvider:
    def __init__(self, provider_id):
        self.provider_id = provider_id


def test_duplicate_provider_id_is_rejected():
    registry = ProviderRegistry()
    registry.register(FakeProvider("fake"))
    with pytest.raises(ConflictError):
        registry.register(FakeProvider("fake"))

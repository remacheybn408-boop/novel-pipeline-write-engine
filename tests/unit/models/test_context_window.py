"""resolve_context_window（V2-004）：catalog 为事实，未知模型回落保守下限且显式记录 source。"""

import pytest

from proseforge.application.models.context_window import (
    CONSERVATIVE_FLOOR,
    catalog_model_snapshot,
    default_catalog_snapshot,
    resolve_context_window,
)
from proseforge.domain.ports.model_provider import ProviderModel


def test_catalog_window_wins_over_floor():
    resolved = resolve_context_window({"context_window": 2048, "source": "catalog"})

    assert resolved == {"context_window": 2048, "source": "catalog"}


def test_unknown_snapshot_falls_back_to_conservative_floor_and_records_source():
    assert CONSERVATIVE_FLOOR == 8192
    assert resolve_context_window(None) == {"context_window": 8192, "source": "fallback"}


@pytest.mark.parametrize("snapshot", [{}, {"context_window": 0}, {"context_window": -5}, {"context_window": "junk"}, {"context_window": None}])
def test_invalid_window_values_fall_back_and_are_recorded(snapshot):
    resolved = resolve_context_window(snapshot)

    assert resolved["context_window"] == 8192
    assert resolved["source"] == "fallback"


def test_custom_floor_is_respected():
    assert resolve_context_window(None, conservative_floor=4096)["context_window"] == 4096


class _Catalog:
    def __init__(self, models):
        self._models = models

    async def get(self, provider, model_id):
        return next((model for model in self._models if model.provider == provider and model.model_id == model_id), None)

    async def list(self, provider=None, search=None, available_only=False):
        return self._models


class _Uow:
    def __init__(self, models):
        self.model_catalog = _Catalog(models)


@pytest.mark.asyncio
async def test_catalog_model_snapshot_reads_catalog_values():
    uow = _Uow([ProviderModel("openai", "gpt-test", "GPT Test", {"reasoning": True}, context_window=2048, max_output_tokens=333)])

    snapshot = await catalog_model_snapshot(uow, "openai", "gpt-test")

    assert snapshot["context_window"] == 2048
    assert snapshot["max_output_tokens"] == 333
    assert snapshot["source"] == "catalog"


@pytest.mark.asyncio
async def test_catalog_model_snapshot_unknown_model_is_recorded_fallback():
    snapshot = await catalog_model_snapshot(_Uow([]), "openai", "ghost")

    assert snapshot["context_window"] == 8192
    assert snapshot["max_output_tokens"] == 1024
    assert snapshot["source"] == "fallback"


@pytest.mark.asyncio
async def test_default_catalog_snapshot_uses_smallest_available_window():
    uow = _Uow([
        ProviderModel("openai", "big", "Big", {}, context_window=128000, max_output_tokens=4096),
        ProviderModel("openai", "small", "Small", {}, context_window=2048, max_output_tokens=333),
    ])

    snapshot = await default_catalog_snapshot(uow)

    assert snapshot is not None
    assert snapshot["context_window"] == 2048
    assert snapshot["source"] == "catalog_default"


@pytest.mark.asyncio
async def test_default_catalog_snapshot_empty_catalog_returns_none():
    assert await default_catalog_snapshot(_Uow([])) is None

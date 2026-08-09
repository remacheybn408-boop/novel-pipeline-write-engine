"""模型目录与思考强度 API（V2-004）。

catalog 为事实：capabilities / validate / context window 全部从 catalog 解析；
不支持的级别 → 422 + supported_levels；warnings 脱敏；probe=false 不呼 provider。
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import Mock

from proseforge.domain.ports.model_provider import ProviderModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork


def _seed_catalog(api_settings, models: list[ProviderModel]) -> None:
    async def seed():
        # 独立引擎：避免在测试线程的事件循环里复用 app loop 的连接池。
        from proseforge.infrastructure.database.session import (
            create_engine_and_sessionmaker,
        )

        engine, factory = create_engine_and_sessionmaker(api_settings)
        try:
            async with SqlAlchemyUnitOfWork(factory) as uow:
                await uow.model_catalog.upsert(models)
                await uow.commit()
        finally:
            await engine.dispose()

    asyncio.run(seed())


def _provider() -> str:
    return f"pv{uuid.uuid4().hex[:8]}"


def _create_project(auth_client) -> str:
    response = auth_client.post_json("/api/v1/projects", {"slug": f"proj-{uuid.uuid4().hex[:12]}", "title": "Models"})
    assert response.status_code == 201
    return response.json()["id"]


def test_models_endpoint_lists_catalog_with_context_window(auth_client, api_settings):
    provider = _provider()
    _seed_catalog(api_settings, [
        ProviderModel(provider, "model-a", "Model A", {"reasoning": True, "reasoning_parameter": "reasoning_effort"}, context_window=2048, max_output_tokens=333),
        ProviderModel(provider, "model-b", "Model B", {"reasoning": False}, context_window=4096, max_output_tokens=777),
    ])

    response = auth_client.get(f"/api/v2/models?provider={provider}")

    assert response.status_code == 200
    rows = {row["model_id"]: row for row in response.json()}
    assert rows["model-a"]["context_window"] == 2048
    assert rows["model-b"]["context_window"] == 4096


def test_capabilities_endpoint_returns_reasoning_parameter(auth_client, api_settings):
    provider = _provider()
    _seed_catalog(api_settings, [
        ProviderModel(provider, "model-a", "Model A", {"reasoning": True, "reasoning_parameter": "reasoning_effort"}, context_window=2048, max_output_tokens=333),
    ])

    response = auth_client.get(f"/api/v2/models/{provider}/model-a/capabilities")

    assert response.status_code == 200
    body = response.json()
    assert body["supports_reasoning"] is True
    assert body["reasoning_parameter"] == "reasoning_effort"
    assert body["context_window"] == 2048
    assert body["source"] == "catalog"

    assert auth_client.get(f"/api/v2/models/{provider}/ghost/capabilities").status_code == 404


def test_validate_maps_provider_specific_reasoning_parameters(auth_client, api_settings):
    provider = _provider()
    _seed_catalog(api_settings, [
        ProviderModel(provider, "openai-style", "OpenAI Style", {"reasoning": True, "reasoning_parameter": "reasoning_effort"}, context_window=2048, max_output_tokens=333),
        ProviderModel(provider, "anthropic-style", "Anthropic Style", {"reasoning": True, "reasoning_parameter": "thinking"}, context_window=4096, max_output_tokens=4096),
        ProviderModel(provider, "google-style", "Google Style", {"reasoning": True, "reasoning_parameter": "thinking_budget"}, context_window=8192, max_output_tokens=4096),
    ])

    openai = auth_client.post_json("/api/v2/model-resolutions/validate", {"provider": provider, "model_id": "openai-style", "level": "high"})
    assert openai.status_code == 200
    body = openai.json()
    assert body["normalized_level"] == "high"
    assert body["provider_parameter"] == {"reasoning_effort": "high"}
    assert body["context_window"] == 2048
    # 契约是 {normalized_level, provider_parameter, context_window, warnings}——不冗余回传整个 policy
    assert "reasoning" not in body

    anthropic = auth_client.post_json("/api/v2/model-resolutions/validate", {"provider": provider, "model_id": "anthropic-style", "level": "medium"})
    assert anthropic.json()["provider_parameter"] == {"thinking": {"type": "enabled", "budget_tokens": 2048}}

    google = auth_client.post_json("/api/v2/model-resolutions/validate", {"provider": provider, "model_id": "google-style", "level": "low"})
    assert google.json()["provider_parameter"] == {"thinking_budget": 1024}


def test_validate_rejects_unsupported_level_with_supported_list(auth_client, api_settings):
    provider = _provider()
    _seed_catalog(api_settings, [
        ProviderModel(provider, "plain", "Plain", {"reasoning": False}, context_window=2048, max_output_tokens=333),
    ])

    response = auth_client.post_json("/api/v2/model-resolutions/validate", {"provider": provider, "model_id": "plain", "level": "max"})

    assert response.status_code == 422
    body = response.json()
    assert "supported_levels" in str(body)
    assert "auto" in str(body["detail"]["details"]["supported_levels"])


def test_validate_rejects_unknown_level_with_supported_list(auth_client, api_settings):
    provider = _provider()
    _seed_catalog(api_settings, [
        ProviderModel(provider, "plain", "Plain", {"reasoning": True, "reasoning_parameter": "reasoning_effort"}, context_window=2048, max_output_tokens=333),
    ])

    response = auth_client.post_json("/api/v2/model-resolutions/validate", {"provider": provider, "model_id": "plain", "level": "ludicrous"})

    assert response.status_code == 422
    body = response.json()
    levels = body["detail"]["details"]["supported_levels"]
    assert {"auto", "none", "low", "medium", "high", "xhigh", "max"} <= set(levels)


def test_validate_warnings_are_redacted(auth_client, api_settings):
    provider = _provider()
    _seed_catalog(api_settings, [
        ProviderModel(provider, "openai-style", "OpenAI Style", {"reasoning": True, "reasoning_parameter": "reasoning_effort"}, context_window=2048, max_output_tokens=333),
    ])

    response = auth_client.post_json("/api/v2/model-resolutions/validate", {"provider": provider, "model_id": "openai-style", "level": "max"})

    assert response.status_code == 200
    warnings = response.json()["warnings"]
    assert warnings, "max→high clamp must be recorded as a warning"
    blob = str(warnings).lower()
    for forbidden in ("http", "sk-", "api_key", "bearer", "secret", "password"):
        assert forbidden not in blob


def test_validate_probe_false_never_calls_provider(auth_client, api_settings, monkeypatch):
    provider = _provider()
    _seed_catalog(api_settings, [
        ProviderModel(provider, "openai-style", "OpenAI Style", {"reasoning": True, "reasoning_parameter": "reasoning_effort"}, context_window=2048, max_output_tokens=333),
    ])

    # 路由模块按名绑定 build_provider，必须 patch 路由侧绑定才会生效。
    builder = Mock(side_effect=AssertionError("probe=false must not build or call a provider"))
    monkeypatch.setattr("proseforge.api.routes.model_capabilities.build_provider", builder)

    response = auth_client.post_json("/api/v2/model-resolutions/validate", {"provider": provider, "model_id": "openai-style", "level": "high", "probe": False})
    assert response.status_code == 200
    builder.assert_not_called()

    skipped = auth_client.post_json("/api/v2/model-resolutions/validate", {"provider": provider, "model_id": "openai-style", "level": "high", "probe": True})
    assert skipped.status_code == 200
    # 未配置凭据时 probe 落 warning（仍不呼 provider），不脱敏任何 secret
    assert any("credentials" in warning for warning in skipped.json()["warnings"])
    builder.assert_not_called()


def test_context_route_uses_catalog_window_for_requested_model(auth_client, api_settings):
    provider = _provider()
    _seed_catalog(api_settings, [
        ProviderModel(provider, "model-a", "Model A", {"reasoning": False}, context_window=2048, max_output_tokens=333),
    ])
    project_id = _create_project(auth_client)

    response = auth_client.get(f"/api/v1/projects/{project_id}/context?provider={provider}&model=model-a")

    assert response.status_code == 200
    body = response.json()
    assert body["context_window"] == 2048  # 替换旧硬编码 128000
    assert body["context_window_source"] == "catalog"
    assert body["available_tokens"] == 2048 - body["used_tokens"]


def test_context_route_unknown_model_falls_back_and_records_source(auth_client):
    project_id = _create_project(auth_client)

    response = auth_client.get(f"/api/v1/projects/{project_id}/context?provider=ghost&model=ghost")

    assert response.status_code == 200
    body = response.json()
    assert body["context_window"] == 8192
    assert body["context_window_source"] == "fallback"


def test_context_route_without_model_uses_catalog_default(auth_client, api_settings):
    project_id = _create_project(auth_client)

    listed = auth_client.get("/api/v2/models")
    assert listed.status_code == 200
    # 与 capabilities_from_model 同一规则：列值为空的条目按 8192 计入最小窗口
    windows = [row["context_window"] if row.get("context_window") else 8192 for row in listed.json()]

    response = auth_client.get(f"/api/v1/projects/{project_id}/context")

    assert response.status_code == 200
    body = response.json()
    if windows:
        assert body["context_window"] == min(windows)
        assert body["context_window_source"] == "catalog_default"
    else:
        assert body["context_window"] == 8192
        assert body["context_window_source"] == "fallback"


def test_context_route_rejects_partial_model_params(auth_client):
    project_id = _create_project(auth_client)

    assert auth_client.get(f"/api/v1/projects/{project_id}/context?provider=openai").status_code == 422
    assert auth_client.get(f"/api/v1/projects/{project_id}/context?model=gpt-4.1-mini").status_code == 422

"""POST /api/v1/models custom-model registration.

context_window is deprecated (product rule: budget = min(700K, real window)
* 0.65, resolved backend-side). The request schema no longer carries the
field; a legacy client still sending it is accepted and the value ignored
(pydantic default extra="ignore").
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from proseforge.api.main import create_app
from proseforge.api.routes.providers import CustomModelRequest
from proseforge.settings import Settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


def test_custom_model_request_accepts_future_model_id():
    request = CustomModelRequest(provider="custom", model_id="future-model-2027")

    assert request.model_id == "future-model-2027"


def test_custom_model_request_no_longer_has_context_window():
    assert "context_window" not in CustomModelRequest.model_fields
    # Legacy payloads keep working: the extra field is ignored, not 422.
    request = CustomModelRequest(provider="custom", model_id="m", context_window=131072)  # type: ignore[call-arg]
    assert not hasattr(request, "context_window")


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        runtime_profile="native",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
    )
    with TestClient(create_app(settings)) as test_client:
        response = test_client.post("/api/v1/auth/setup", json={"email": "owner@example.com", "password": "p" * 12})
        assert response.status_code == 201
        response = test_client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "p" * 12})
        assert response.status_code == 200
        yield test_client


def test_add_custom_model_ignores_context_window(client: TestClient):
    response = client.post(
        "/api/v1/models",
        json={"provider": "custom", "model_id": "endpoint-x", "context_window": 131072},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    # Not written anywhere: neither the capabilities dict nor the resolved row.
    assert "context_window" not in body["capabilities"]
    assert body["context_window"] is None
    listing = client.get("/api/v1/models", params={"provider": "custom"})
    assert listing.status_code == 200
    row = next(item for item in listing.json() if item["model_id"] == "endpoint-x")
    assert "context_window" not in row["capabilities"]
    assert row["context_window"] == 8192  # conservative default, not the typed 131072

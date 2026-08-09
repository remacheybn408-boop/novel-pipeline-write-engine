from fastapi.testclient import TestClient

from proseforge.api.main import create_app


def test_provider_catalog_requires_authentication():
    response = TestClient(create_app()).get("/api/v1/providers")
    assert response.status_code == 401

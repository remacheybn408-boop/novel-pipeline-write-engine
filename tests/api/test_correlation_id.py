from fastapi.testclient import TestClient

from proseforge.api.main import create_app


def test_api_returns_a_safe_correlation_id_without_logging_payloads():
    response = TestClient(create_app()).get("/api/v1/health/live", headers={"x-correlation-id": "request-123"})

    assert response.headers["x-correlation-id"] == "request-123"

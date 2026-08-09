from fastapi.testclient import TestClient

from proseforge.api.main import create_app


def test_workflow_controls_require_authentication():
    response = TestClient(create_app()).post("/api/v1/workflows/run-1/retry")

    assert response.status_code == 401

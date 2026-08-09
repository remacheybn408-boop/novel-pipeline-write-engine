from fastapi.testclient import TestClient

from proseforge.api.main import create_app


def test_maintenance_recovery_requires_authentication():
    response = TestClient(create_app()).post("/api/v1/maintenance/workflows/recover-expired")
    assert response.status_code == 401


def test_blob_verification_requires_authentication():
    response = TestClient(create_app()).post("/api/v1/maintenance/blobs/verify")
    assert response.status_code == 401

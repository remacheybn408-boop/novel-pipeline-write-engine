from fastapi.testclient import TestClient

from proseforge.api.main import create_app
from proseforge.settings import Settings


def test_liveness_is_available():
    response = TestClient(create_app()).get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_storage_checks(tmp_path):
    # 存储路径显式指向临时目录：默认 /data/blobs 只在容器内可创建，
    # 裸机（CI runner、开发机）上必须让测试自带可写目录。readiness 探针是
    # 只读的（目录必须已存在），且 lifecycle/数据库检查要求应用完整启动，
    # 因此用 native sqlite 设置 + TestClient 上下文管理器跑 lifespan。
    (tmp_path / "blobs").mkdir()
    (tmp_path / "backups").mkdir()
    settings = Settings(
        runtime_profile="native",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["blob_root"] == "ok"
    assert response.json()["checks"]["master_key"] == "ok"
    assert response.json()["checks"]["pgvector"] == "ok"
    assert response.json()["checks"]["partial_messages"] == "ok"


def test_logout_clears_session_cookie_without_database_access():
    response = TestClient(create_app()).post("/api/v1/auth/logout")
    assert response.status_code == 204
    assert "proseforge_session" in response.headers.get("set-cookie", "")

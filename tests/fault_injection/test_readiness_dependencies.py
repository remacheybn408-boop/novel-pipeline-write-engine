from types import SimpleNamespace

import pytest

from proseforge.api.routes.health import ready
from proseforge.settings import Settings


class BrokenConnection:
    async def __aenter__(self):
        raise ConnectionError("database unavailable")

    async def __aexit__(self, *args):
        return None


class BrokenEngine:
    def connect(self):
        return BrokenConnection()


@pytest.mark.asyncio
async def test_readiness_is_503_when_database_and_redis_are_unavailable(tmp_path):
    settings = Settings(
        database_url="postgresql+asyncpg://unused:unused@127.0.0.1:1/unused",
        redis_url="redis://127.0.0.1:1/0",
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings, engine=BrokenEngine())))
    response = await ready(request)
    assert response.status_code == 503
    assert response.body
    assert b'"database":"error"' in response.body
    assert b'"redis":"error"' in response.body

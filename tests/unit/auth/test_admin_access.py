import pytest
from fastapi import HTTPException

from proseforge.api.dependencies import require_admin
from proseforge.api.routes.maintenance import recover_expired_workflows
from proseforge.application.auth.service import AuthUser


def test_only_admin_users_can_run_maintenance_actions():
    assert require_admin(AuthUser("admin", "admin@example.com", "ADMIN")).role == "ADMIN"
    with pytest.raises(HTTPException) as error:
        require_admin(AuthUser("user", "user@example.com", "USER"))
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_maintenance_recovers_expired_workflows_and_commits():
    class FakeWorkflows:
        async def recover_expired(self):
            return 2

    class FakeUow:
        workflows = FakeWorkflows()
        committed = False

        class Session:
            def add(self, _item):
                pass

        session = Session()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            self.committed = True

    uow = FakeUow()
    assert await recover_expired_workflows(AuthUser("admin", "admin@example.com", "ADMIN"), uow) == {"status": "ok", "recovered": 2}
    assert uow.committed is True

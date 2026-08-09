import pytest

from proseforge.operations.maintenance import (
    record_maintenance_audit,
    verify_attachment_blobs,
)


@pytest.mark.asyncio
async def test_blob_verification_reports_missing_and_valid_objects():
    class Attachment:
        def __init__(self, key, digest):
            self.storage_key = key
            self.sha256 = digest

    class Attachments:
        async def list_all(self):
            return [Attachment("sha256/aa/valid", "valid"), Attachment("sha256/bb/missing", "missing")]

    class Uow:
        attachments = Attachments()

    class Store:
        async def get(self, key):
            if "missing" in key:
                raise FileNotFoundError(key)
            return b"data"

    result = await verify_attachment_blobs(Uow(), Store())
    assert result == {"checked": 2, "valid": 0, "missing": ["sha256/bb/missing"], "corrupt": ["sha256/aa/valid"]}


def test_maintenance_audit_contains_actor_and_result():
    class Session:
        def __init__(self):
            self.items = []

        def add(self, item):
            self.items.append(item)

    class Uow:
        session = Session()

    record_maintenance_audit(Uow(), "admin-1", "verify_blobs", {"checked": 2})
    audit = Uow.session.items[0]
    assert audit.user_id == "admin-1"
    assert audit.action == "verify_blobs"
    assert audit.payload == '{"checked":2}'

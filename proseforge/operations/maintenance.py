from __future__ import annotations

import hashlib
import json

from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.remaining import AuditLogModel


async def verify_attachment_blobs(uow, store) -> dict[str, object]:
    attachments = await uow.attachments.list_all()
    missing: list[str] = []
    corrupt: list[str] = []
    for attachment in attachments:
        try:
            data = await store.get(attachment.storage_key)
        except (FileNotFoundError, ValueError):
            missing.append(attachment.storage_key)
            continue
        if hashlib.sha256(data).hexdigest() != attachment.sha256:
            corrupt.append(attachment.storage_key)
    return {"checked": len(attachments), "valid": len(attachments) - len(missing) - len(corrupt), "missing": missing, "corrupt": corrupt}


def record_maintenance_audit(uow, user_id: str, action: str, payload: dict[str, object]) -> None:
    uow.session.add(AuditLogModel(id=new_id(), user_id=user_id, action=action, target_type="maintenance", target_id="system", payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))

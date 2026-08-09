from __future__ import annotations

import json
import shutil
from pathlib import Path

from proseforge.operations.backup import BackupService


def create_backup(*, source: str | Path, output: str | Path, database_dump: bytes | None = None) -> dict[str, object]:
    target = Path(output)
    service = BackupService(target.parent if target.suffixes[-2:] == [".tar", ".gz"] else target)
    created = service.create(source, database_dump=database_dump)
    archive = Path(created.archive)
    if target.suffixes[-2:] == [".tar", ".gz"] and archive != target:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(archive), target)
        archive = target
    result = {"archive": str(archive), "files": created.files, "sha256": created.sha256, "manifest": created.metadata}
    return result


def json_result(result: dict[str, object]) -> str:
    return json.dumps(result, ensure_ascii=False, sort_keys=True)

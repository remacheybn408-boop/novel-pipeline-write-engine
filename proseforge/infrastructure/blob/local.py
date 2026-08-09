from __future__ import annotations

import hashlib
import os
from pathlib import Path


class LocalBlobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, digest: str) -> Path:
        return self.root / "sha256" / digest[:2] / digest[2:4] / digest

    async def put(self, *, data: bytes, media_type: str) -> str:
        digest = hashlib.sha256(data).hexdigest()
        target = self._path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temp = target.with_suffix(".tmp")
            with temp.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"

    async def get(self, storage_key: str) -> bytes:
        path = self.root / storage_key
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("invalid storage key")
        return path.read_bytes()

    async def delete(self, storage_key: str) -> None:
        path = self.root / storage_key
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("invalid storage key")
        path.unlink(missing_ok=True)

    async def exists(self, storage_key: str) -> bool:
        return (self.root / storage_key).is_file()

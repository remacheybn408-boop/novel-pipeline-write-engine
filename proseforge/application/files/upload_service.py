from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePath


@dataclass(frozen=True)
class UploadLimits:
    max_bytes: int = 50 * 1024 * 1024
    max_zip_entries: int = 1000
    max_expansion_ratio: int = 100


# Module-level singleton for the default argument (B008): UploadLimits is frozen
# so sharing one instance across calls is safe.
DEFAULT_UPLOAD_LIMITS = UploadLimits()


def validate_upload(filename: str, data: bytes, media_type: str, limits: UploadLimits = DEFAULT_UPLOAD_LIMITS) -> None:
    safe_name = PurePath(filename).name
    if safe_name != filename or filename in {"", ".", ".."} or not re.fullmatch(r"[\w .()\-]+", filename, re.UNICODE):
        raise ValueError("unsafe filename")
    if len(data) > limits.max_bytes:
        raise ValueError("upload exceeds size limit")
    if filename.lower().endswith(".zip") or media_type == "application/zip":
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > limits.max_zip_entries or any(PurePath(item.filename).is_absolute() or ".." in PurePath(item.filename).parts for item in entries):
                raise ValueError("unsafe zip archive")
            compressed = max(1, sum(item.compress_size for item in entries))
            if sum(item.file_size for item in entries) / compressed > limits.max_expansion_ratio:
                raise ValueError("zip expansion ratio exceeds limit")


def verify_download_digest(data: bytes, expected_sha256: str) -> bool:
    return hashlib.sha256(data).hexdigest() == expected_sha256

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class BackupVerification:
    archive: str
    files: int
    sha256: str
    metadata: dict[str, object] | None = None


class BackupService:
    def __init__(self, backup_root: str | Path):
        self.backup_root = Path(backup_root)

    def create(
        self,
        source_root: str | Path,
        *,
        database_dump: bytes | None = None,
        application_version: str = "unknown",
        migration_revision: str = "unknown",
    ) -> BackupVerification:
        source = Path(source_root).resolve()
        if not source.is_dir():
            raise ValueError("backup source does not exist")
        self.backup_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        archive = self.backup_root / f"proseforge-{stamp}.tar.gz"
        files = 0
        manifest_entries: list[dict[str, object]] = []
        with tarfile.open(archive, "w:gz") as tar:
            for path in sorted(source.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    tar.add(path, arcname=path.relative_to(source))
                    files += 1
                    manifest_entries.append({"path": path.relative_to(source).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size})
            if database_dump is not None:
                info = tarfile.TarInfo("database.dump")
                info.size = len(database_dump)
                tar.addfile(info, io.BytesIO(database_dump))
                files += 1
                manifest_entries.append({"path": "database.dump", "sha256": hashlib.sha256(database_dump).hexdigest(), "bytes": len(database_dump)})
            metadata = {"archive": archive.name, "files": files, "sha256": "", "application_version": application_version, "migration_revision": migration_revision, "entries": manifest_entries}
            raw_metadata = json.dumps(metadata, ensure_ascii=False).encode()
            info = tarfile.TarInfo("backup-manifest.json")
            info.size = len(raw_metadata)
            tar.addfile(info, io.BytesIO(raw_metadata))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        manifest = archive.with_suffix(archive.suffix + ".json")
        metadata["sha256"] = digest
        manifest.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        return BackupVerification(str(archive), len(manifest_entries), digest, metadata)

    def verify(self, archive: str | Path) -> BackupVerification:
        path = Path(archive)
        if not path.is_file() or path.suffixes[-2:] != [".tar", ".gz"]:
            raise ValueError("invalid backup archive")
        with tarfile.open(path, "r:gz") as tar:
            members = [member for member in tar.getmembers() if member.isfile()]
            if any(Path(member.name).is_absolute() or ".." in Path(member.name).parts for member in members):
                raise ValueError("backup contains unsafe path")
            manifest_member = next((member for member in members if member.name == "backup-manifest.json"), None)
            metadata = json.loads(tar.extractfile(manifest_member).read()) if manifest_member else None
            if metadata:
                member_map = {member.name: member for member in members}
                for entry in metadata.get("entries", []):
                    member = member_map.get(str(entry["path"]))
                    if member is None:
                        raise ValueError(f"backup is missing {entry['path']}")
                    content = tar.extractfile(member).read()
                    if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                        raise ValueError(f"backup checksum mismatch: {entry['path']}")
        logical_files = len(metadata.get("entries", [])) if metadata else len(members)
        return BackupVerification(str(path), logical_files, hashlib.sha256(path.read_bytes()).hexdigest(), metadata)

    def list(self) -> list[Path]:
        self.backup_root.mkdir(parents=True, exist_ok=True)
        return sorted(self.backup_root.glob("proseforge-*.tar.gz"), reverse=True)

    def restore(self, archive: str | Path, destination: str | Path) -> BackupVerification:
        """Restore into a staging directory after validating every archive member."""
        verified = self.verify(archive)
        target = Path(destination).resolve()
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(verified.archive, "r:gz") as tar:
            for member in tar.getmembers():
                member_path = (target / member.name).resolve()
                if target != member_path and target not in member_path.parents:
                    raise ValueError("backup contains unsafe path")
            tar.extractall(target, filter="data")
        return verified

    def restore_database(self, archive: str | Path, database_url: str) -> BackupVerification:
        """Restore the SQL dump into an explicitly named staging database only."""
        verified = self.verify(archive)
        target_name = database_url.rsplit("/", 1)[-1].split("?", 1)[0]
        if "staging" not in target_name.lower():
            raise ValueError("database restore requires a staging database target")
        with tarfile.open(verified.archive, "r:gz") as tar:
            member = tar.getmember("database.dump")
            dump = tar.extractfile(member)
            if dump is None:
                raise ValueError("backup does not contain database.dump")
            with tempfile.NamedTemporaryFile(suffix=".sql", mode="wb") as handle:
                # pg_dump 17 can emit this setting even when targeting PostgreSQL 16;
                # PostgreSQL 16 rejects it before executing the actual dump.
                compatible_dump = dump.read().replace(b"SET transaction_timeout = 0;\n", b"")
                handle.write(compatible_dump)
                handle.flush()
                normalized = database_url.replace("postgresql+psycopg://", "postgresql://").replace("postgresql+asyncpg://", "postgresql://")
                result = subprocess.run(["psql", "--dbname", normalized, "--set", "ON_ERROR_STOP=1", "--file", handle.name], check=False, capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace").strip() or "psql restore failed")
        return verified

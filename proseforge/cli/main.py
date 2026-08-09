from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
from pathlib import Path

from proseforge.infrastructure.legacy_import.importer import LegacyImporter
from proseforge.operations.backup import BackupService
from version import get_version


def _database_dump(database_url: str | None = None) -> bytes:
    url = database_url or os.getenv("PROSEFORGE_SYNC_DATABASE_URL") or os.getenv("PROSEFORGE_DATABASE_URL")
    if not url:
        raise RuntimeError("database URL is required for automatic database backup")
    normalized = url.replace("postgresql+psycopg://", "postgresql://").replace("postgresql+asyncpg://", "postgresql://")
    result = subprocess.run(["pg_dump", "--format=plain", "--no-owner", normalized], check=False, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or "pg_dump failed")
    return result.stdout


def _run_upgrade_command(args: argparse.Namespace) -> int:
    """Wire the real native upgrade path (V15-009).

    Defaults resolve from the native platform data dir instead of a
    hard-coded /data; database URL precedence is --database-url >
    PROSEFORGE_DATABASE_URL > the native SQLite file. stop/start hooks
    stay None: the CLI upgrades a stopped instance — while the native
    service owns the data dir an upgrade attempt holds .upgrade.lock and
    concurrent runs are rejected with UpgradeBusyError.
    """
    import json

    from proseforge.cli.commands.doctor import doctor_report
    from proseforge.operations.upgrade import (
        UpgradeBusyError,
        alembic_migration_callable,
        check_upgrade,
        run_upgrade,
    )
    from proseforge.runtime.paths import resolve_paths
    from proseforge.runtime.profile import RuntimeProfile

    env = dict(os.environ)
    if args.data_dir:
        env["PROSEFORGE_DATA_DIR"] = args.data_dir
    paths = resolve_paths(RuntimeProfile.NATIVE, env)
    data_dir = paths.data_dir
    backup_dir = Path(args.backup_dir) if args.backup_dir else paths.backup_dir
    database_path = paths.database_path or data_dir / "proseforge.sqlite3"
    database_url = args.database_url or os.getenv("PROSEFORGE_DATABASE_URL") or f"sqlite+aiosqlite:///{database_path.as_posix()}"
    if args.check:
        report = check_upgrade(data_dir=data_dir, backup_dir=backup_dir, database_url=database_url)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["status"] == "ready" else 1

    def _doctor() -> None:
        if doctor_report(profile=RuntimeProfile.NATIVE, data_dir=data_dir)["status"] != "ok":
            raise RuntimeError("post-upgrade health check failed")

    try:
        report_path = run_upgrade(
            data_dir=data_dir,
            backup_dir=backup_dir,
            migrate=alembic_migration_callable(database_url),
            doctor=_doctor,
            database_url=database_url,
        )
    except UpgradeBusyError:
        print(json.dumps({"status": "blocked", "reason": "upgrade lock held", "data_dir": str(data_dir)}, ensure_ascii=False, sort_keys=True))
        return 1
    except Exception as exc:
        # Redacted: exception messages may embed connection strings with
        # credentials, so only the error type name is reported.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "data_dir": str(data_dir)}, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps({"status": "upgraded", "report": str(report_path), "data_dir": str(data_dir)}, ensure_ascii=False, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="proseforge")
    parser.add_argument("--version", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--data-dir")
    upgrade = subparsers.add_parser("upgrade")
    upgrade.add_argument("--check", action="store_true")
    upgrade.add_argument("--data-dir")
    upgrade.add_argument("--backup-dir")
    upgrade.add_argument("--database-url")
    migrate = subparsers.add_parser("migrate")
    migrate_subparsers = migrate.add_subparsers(dest="migration")
    legacy = migrate_subparsers.add_parser("legacy")
    legacy.add_argument("--workspace", required=True)
    legacy.add_argument("--archive-root", default="/data/backups/legacy-import")
    legacy.add_argument("--owner-id", help="Web user ID that will own imported projects")
    backup = subparsers.add_parser("backup")
    backup.add_argument("action", choices=("create", "list", "verify", "restore"))
    backup.add_argument("archive", nargs="?")
    backup.add_argument("--source", default="/data")
    backup.add_argument("--root", default="/data/backups")
    backup.add_argument("--destination")
    backup.add_argument("--database-dump")
    backup.add_argument("--include-database", action="store_true")
    backup.add_argument("--database-url")
    backup.add_argument("--restore-database-url")
    backup.add_argument("--output")
    backup.add_argument("--staging")
    web = subparsers.add_parser("web")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    web.add_argument("--data-dir")
    web.add_argument("--frontend-dir")
    start = subparsers.add_parser("start")
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=8000)
    start.add_argument("--data-dir")
    stop = subparsers.add_parser("stop")
    stop.add_argument("--data-dir")
    status = subparsers.add_parser("status")
    status.add_argument("--host", default="127.0.0.1")
    status.add_argument("--port", type=int, default=8000)
    status.add_argument("--data-dir")
    update = subparsers.add_parser("update")
    update.add_argument("--data-dir")
    update.add_argument("--backup-dir")
    update.add_argument("--database-url")
    args = parser.parse_args(argv)
    if args.version:
        print(get_version())
    elif args.command == "doctor":
        from proseforge.cli.commands.doctor import doctor_report
        report = doctor_report(data_dir=args.data_dir)
        if args.json:
            import json
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        else:
            print(f"{report['status']}: {report['profile']} runtime; data={report['checks']['data_dir']}")
        return 0 if report["status"] == "ok" else 1
    elif args.command == "upgrade":
        return _run_upgrade_command(args)
    elif args.command == "migrate" and args.migration == "legacy":
        session_factory = None
        if args.owner_id:
            from proseforge.infrastructure.database.session import (
                create_engine_and_sessionmaker,
            )
            from proseforge.settings import get_settings
            _, session_factory = create_engine_and_sessionmaker(get_settings())
        report = asyncio.run(LegacyImporter(args.archive_root, session_factory, args.owner_id).import_workspace(args.workspace))
        print(report)
        return 0 if report.status == "COMPLETED" else 1
    elif args.command == "backup":
        service = BackupService(args.root)
        if args.action == "create":
            if args.database_dump:
                with open(args.database_dump, "rb") as dump_file:
                    dump = dump_file.read()
            elif args.include_database:
                dump = _database_dump(args.database_url)
            else:
                dump = None
            if args.output:
                from proseforge.cli.commands.backup import create_backup, json_result
                print(json_result(create_backup(source=args.source, output=args.output, database_dump=dump)))
            else:
                print(service.create(args.source, database_dump=dump))
        elif args.action == "list":
            for archive in service.list():
                print(archive)
        elif args.archive is None:
            parser.error("backup verify/restore requires an archive")
        elif args.action == "verify":
            try:
                print(service.verify(args.archive))
            except Exception as exc:  # 损坏/截断/非本格式归档：干净报错而非堆栈
                print(f"backup verify failed: {type(exc).__name__}: {exc}")
                return 1
        elif args.action == "restore":
            destination = args.destination or args.staging
            if not destination:
                parser.error("backup restore requires --destination staging path")
            try:
                print(service.restore(args.archive, destination))
                if args.restore_database_url:
                    print(service.restore_database(args.archive, args.restore_database_url))
            except Exception as exc:
                print(f"backup restore failed: {type(exc).__name__}: {exc}")
                return 1
        return 0
    elif args.command == "web":
        from proseforge.cli.commands.web import run_web
        return run_web(host=args.host, port=args.port, data_dir=args.data_dir, frontend_dir=args.frontend_dir)
    elif args.command == "start":
        from proseforge.cli.commands.service import run_start
        return run_start(data_dir=args.data_dir, host=args.host, port=args.port)
    elif args.command == "stop":
        from proseforge.cli.commands.service import run_stop
        return run_stop(data_dir=args.data_dir)
    elif args.command == "status":
        from proseforge.cli.commands.service import run_status
        return run_status(data_dir=args.data_dir, host=args.host, port=args.port)
    elif args.command == "update":
        from proseforge.cli.commands.update import run_update
        return run_update(data_dir=args.data_dir, backup_dir=args.backup_dir, database_url=args.database_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

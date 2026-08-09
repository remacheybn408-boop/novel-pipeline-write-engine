from __future__ import annotations

import json

from proseforge.cli.commands.doctor import doctor_report
from proseforge.cli.main import main


def test_doctor_report_is_redacted_and_stable(tmp_path):
    report = doctor_report(profile="native", data_dir=tmp_path / "data")
    assert set(report) == {"status", "checks", "version", "profile", "database", "queue", "backup_path"}
    assert report["profile"] == "native"
    assert report["database"] == "sqlite"
    assert "secret" not in json.dumps(report).lower()


def test_doctor_json_cli(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PROSEFORGE_RUNTIME_PROFILE", "native")
    assert main(["doctor", "--json", "--data-dir", str(tmp_path / "data")]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ok"


def test_doctor_defaults_to_native_without_server_indicators(tmp_path, monkeypatch):
    monkeypatch.delenv("PROSEFORGE_RUNTIME_PROFILE", raising=False)
    monkeypatch.delenv("PROSEFORGE_DATABASE_URL", raising=False)
    monkeypatch.setenv("PROSEFORGE_DATA_DIR", str(tmp_path / "data"))
    report = doctor_report()
    assert report["profile"] == "native"
    assert report["database"] == "sqlite"


def test_doctor_infers_server_from_database_url(monkeypatch):
    monkeypatch.delenv("PROSEFORGE_RUNTIME_PROFILE", raising=False)
    monkeypatch.setenv("PROSEFORGE_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/x")
    assert doctor_report()["profile"] == "server"


def test_doctor_survives_uncreatable_data_dir(tmp_path, monkeypatch):
    """data_dir 不可创建时 doctor 如实报 error，不允许崩溃。

    回归：CI 裸机上 server profile 解析到 /data，mkdir 无权限直接
    PermissionError，诊断命令必须降级为 status=error。
    """
    monkeypatch.delenv("PROSEFORGE_DATABASE_URL", raising=False)
    blocker = tmp_path / "blocked"
    blocker.write_text("file", encoding="utf-8")
    report = doctor_report(profile="native", data_dir=blocker / "child")
    assert report["status"] == "error"
    assert report["checks"]["data_dir"] is False


def test_backup_create_json_cli(tmp_path, capsys):
    source = tmp_path / "data"
    source.mkdir()
    (source / "chapter.txt").write_text("chapter", encoding="utf-8")
    output = tmp_path / "backup.tar.gz"
    assert main(["backup", "create", "--source", str(source), "--output", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert output.is_file()
    assert result["files"] == 1


def test_backup_verify_rejects_corrupted_archive(tmp_path, capsys):
    source = tmp_path / "data"
    source.mkdir()
    (source / "chapter.txt").write_text("chapter", encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"
    assert main(["backup", "create", "--source", str(source), "--output", str(archive)]) == 0
    capsys.readouterr()
    payload = archive.read_bytes()
    archive.write_bytes(payload[:-64] + b"CORRUPTED!")
    assert main(["backup", "verify", str(archive)]) == 1
    out = capsys.readouterr().out
    assert "backup verify failed" in out
    assert "Traceback" not in out

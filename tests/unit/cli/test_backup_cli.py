from proseforge.cli.main import _database_dump


def test_database_dump_normalizes_asyncpg_url(monkeypatch):
    captured = {}

    class Result:
        returncode = 0
        stdout = b"CREATE TABLE projects;"
        stderr = b""

    def fake_run(command, **kwargs):
        captured["command"] = command
        assert kwargs["capture_output"] is True
        return Result()

    monkeypatch.setattr("proseforge.cli.main.subprocess.run", fake_run)
    assert _database_dump("postgresql+asyncpg://user:pass@postgres/db") == b"CREATE TABLE projects;"
    assert captured["command"][-1] == "postgresql://user:pass@postgres/db"

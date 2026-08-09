"""setup_logging: creates the log dir, attaches one rotating handler
(idempotent), and records actually land in app.log."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

import pytest

from proseforge.runtime.logging import setup_logging


@pytest.fixture()
def root_logger_cleanup():
    root = logging.getLogger()
    before = list(root.handlers)
    yield root
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()


def _app_handlers(root: logging.Logger, target_dir) -> list[RotatingFileHandler]:
    return [
        handler
        for handler in root.handlers
        if isinstance(handler, RotatingFileHandler)
        and handler.baseFilename == str((target_dir / "app.log").resolve())
    ]


def test_creates_directory_and_handler(tmp_path, root_logger_cleanup):
    log_dir = tmp_path / "nested" / "logs"
    assert not log_dir.exists()
    setup_logging(log_dir)
    assert log_dir.is_dir()
    assert len(_app_handlers(root_logger_cleanup, log_dir)) == 1
    assert root_logger_cleanup.level == logging.INFO


def test_idempotent_for_same_directory(tmp_path, root_logger_cleanup):
    setup_logging(tmp_path)
    setup_logging(tmp_path)
    assert len(_app_handlers(root_logger_cleanup, tmp_path)) == 1


def test_records_are_written_to_app_log(tmp_path, root_logger_cleanup):
    setup_logging(tmp_path)
    logging.getLogger("proseforge.test").error("boom happened")
    for handler in _app_handlers(root_logger_cleanup, tmp_path):
        handler.flush()
    content = (tmp_path / "app.log").read_text(encoding="utf-8")
    assert "ERROR" in content
    assert "proseforge.test" in content
    assert "boom happened" in content


def test_rollover_race_keeps_logging(monkeypatch, tmp_path, root_logger_cleanup):
    """S4: Windows multi-process rotation race — a failed rename must not
    drop the record or kill the handler's stream."""
    import logging.handlers
    import os

    from proseforge.runtime import logging as logging_module

    handler = logging_module._TolerantRotatingFileHandler(
        str(tmp_path / "app.log"), maxBytes=200, backupCount=2, encoding="utf-8"
    )
    test_logger = logging.getLogger("proseforge.test.rotation")
    test_logger.addHandler(handler)

    real_rename = os.rename
    calls = {"count": 0}

    def flaky_rename(src, dst, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise PermissionError(13, "another process holds the file")
        return real_rename(src, dst, *args, **kwargs)

    # Patch os.rename where logging.handlers looks it up.
    monkeypatch.setattr(logging.handlers.os, "rename", flaky_rename)

    for index in range(20):
        test_logger.error("race line %d", index)
    handler.flush()
    # The failing rotation was skipped; the stream survived and records kept
    # flowing into the current file.
    assert handler.stream is not None
    content = (tmp_path / "app.log").read_text(encoding="utf-8")
    assert "race line 19" in content
    test_logger.removeHandler(handler)
    handler.close()


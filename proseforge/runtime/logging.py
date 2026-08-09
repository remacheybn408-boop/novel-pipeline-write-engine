"""File logging setup (app.log under the resolved runtime log_dir).

The codebase logs through stdlib ``logging.getLogger`` but never configured
any handler, so nothing ever reached log_dir. ``setup_logging`` attaches one
RotatingFileHandler (10 MB x 3) to the root logger and creates the log
directory for every profile (server included — bootstrap_runtime only
creates it for native). Idempotent: repeated calls for the same file do not
add duplicate handlers.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_FILENAME = "app.log"
# Public so the error-log report (api/routes/logs.py) reads the same set of
# rotated backups the handler keeps: app.log.1 .. app.log.LOG_BACKUP_COUNT.
LOG_BACKUP_COUNT = 3
_MAX_BYTES = 10 * 1024 * 1024
_HANDLER_MARKER = "_proseforge_app_log"


class _TolerantRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that survives multi-process rotation races.

    The API and the Celery worker share one app.log. On Windows the backup
    renames in ``doRollover`` fail with ``PermissionError`` while another
    process holds the file open, and the stdlib handler then drops the
    record through ``handleError``. Skipping the failed rotation keeps the
    record; the file simply grows past maxBytes until a later attempt
    succeeds.
    """

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError:
            # Another process is rotating or holds the file open. Reopen the
            # stream so logging continues into the current file.
            if self.stream is None:
                self.stream = self._open()


def setup_logging(log_dir: Path) -> None:
    """Attach the app.log rotating file handler to the root logger.

    Creates log_dir if missing (parents, exist_ok). Safe to call from both
    the API lifespan and the worker entrypoint; a handler for the same
    target file is only added once.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    target = os.path.abspath(str(log_dir / LOG_FILENAME))

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in root.handlers:
        if (
            getattr(handler, _HANDLER_MARKER, False)
            and isinstance(handler, RotatingFileHandler)
            and handler.baseFilename == target
        ):
            return

    handler = _TolerantRotatingFileHandler(
        target, maxBytes=_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    setattr(handler, _HANDLER_MARKER, True)
    root.addHandler(handler)

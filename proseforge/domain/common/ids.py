from __future__ import annotations

from threading import Lock
from time import time_ns

_lock = Lock()
_last_timestamp = 0
_sequence = 0


def new_id() -> str:
    """Return a time-ordered opaque identifier."""
    global _last_timestamp, _sequence
    with _lock:
        timestamp = time_ns()
        if timestamp <= _last_timestamp:
            _sequence += 1
            timestamp = _last_timestamp
        else:
            _sequence = 0
            _last_timestamp = timestamp
        return f"{timestamp:016x}{_sequence:08x}"

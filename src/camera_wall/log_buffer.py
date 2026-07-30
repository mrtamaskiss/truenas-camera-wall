from __future__ import annotations

from collections import deque
import logging
import os
import threading
import time
from typing import Any

from .ffmpeg import mask_text


DEFAULT_MAX_LINES = int(os.environ.get("CAMERA_WALL_LOG_BUFFER_LINES", "500"))

_lock = threading.Lock()
_logs: deque[dict[str, str]] = deque(maxlen=DEFAULT_MAX_LINES)
_handler: RingLogHandler | None = None


class RingLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        item = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "message": mask_text(record.getMessage()),
        }
        with _lock:
            _logs.append(item)


def install_log_buffer(level: int = logging.INFO) -> None:
    global _handler
    root = logging.getLogger()
    if _handler is not None and _handler in root.handlers:
        return
    _handler = RingLogHandler(level=level)
    root.addHandler(_handler)


def get_logs(limit: int = 200) -> list[dict[str, str]]:
    limit = max(1, min(limit, DEFAULT_MAX_LINES))
    with _lock:
        return list(_logs)[-limit:]


def clear_logs() -> None:
    with _lock:
        _logs.clear()


def log_payload(limit: int = 200) -> dict[str, Any]:
    logs = get_logs(limit)
    return {"logs": logs, "count": len(logs), "limit": limit}

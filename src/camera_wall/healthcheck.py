from __future__ import annotations

import os
from pathlib import Path
import signal


PID_FILE = Path(os.environ.get("CAMERA_WALL_PID_FILE", "/tmp/camera-wall/ffmpeg.pid"))


def main() -> int:
    try:
        pid_text = PID_FILE.read_text(encoding="utf-8").strip()
        pid = int(pid_text)
    except (FileNotFoundError, ValueError):
        return 1

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return 1
    except PermissionError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

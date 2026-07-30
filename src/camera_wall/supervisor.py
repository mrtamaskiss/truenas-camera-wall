from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .config import ConfigError, load_config
from .ffmpeg import build_ffmpeg_command, masked_command


PID_FILE = Path(os.environ.get("CAMERA_WALL_PID_FILE", "/tmp/camera-wall/ffmpeg.pid"))
DEFAULT_CONFIG = os.environ.get("CAMERA_WALL_CONFIG", "/config/config.yaml")

_stop_requested = False
_current_process: subprocess.Popen[bytes] | None = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the TrueNAS camera wall FFmpeg supervisor")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to config YAML")
    parser.add_argument(
        "--print-command",
        action="store_true",
        help="Print the sanitized FFmpeg command and exit",
    )
    args = parser.parse_args(argv)

    _configure_logging()
    _install_signal_handlers()

    try:
        config = load_config(args.config)
        command = build_ffmpeg_command(config)
    except ConfigError as exc:
        logging.error("Configuration error: %s", exc)
        return 2

    if args.print_command:
        print(masked_command(command))
        return 0

    logging.info("Starting camera wall supervisor with config %s", args.config)
    logging.info("FFmpeg command: %s", masked_command(command))

    while not _stop_requested:
        exit_code = _run_once(command)
        if _stop_requested:
            return exit_code
        logging.warning(
            "FFmpeg exited with code %s; restarting in %s seconds",
            exit_code,
            config.ffmpeg.restart_delay_seconds,
        )
        time.sleep(config.ffmpeg.restart_delay_seconds)
        try:
            config = load_config(args.config)
            command = build_ffmpeg_command(config)
        except ConfigError as exc:
            logging.error("Configuration error after restart: %s", exc)
            time.sleep(config.ffmpeg.restart_delay_seconds)
    return 0


def _run_once(command: list[str]) -> int:
    global _current_process
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        _current_process = subprocess.Popen(command)
        PID_FILE.write_text(str(_current_process.pid), encoding="utf-8")
        logging.info("FFmpeg started with pid %s", _current_process.pid)
        return _current_process.wait()
    except FileNotFoundError:
        logging.error("FFmpeg binary not found: %s", command[0])
        return 127
    finally:
        _current_process = None
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("CAMERA_WALL_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


def _install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)


def _handle_stop(signum: int, _frame: object) -> None:
    global _stop_requested
    _stop_requested = True
    logging.info("Received signal %s, stopping FFmpeg", signum)
    if _current_process and _current_process.poll() is None:
        _current_process.terminate()

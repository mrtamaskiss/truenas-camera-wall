from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .config import AppConfig, ConfigError, load_config
from .ffmpeg import build_ffmpeg_command, mask_text, masked_command
from .gpu import GpuMonitor
from .input_health import InputHealthTracker
from .log_buffer import install_log_buffer
from .web import WebSettings, start_admin_server


PID_FILE = Path(os.environ.get("CAMERA_WALL_PID_FILE", "/tmp/camera-wall/ffmpeg.pid"))
DEFAULT_CONFIG = os.environ.get("CAMERA_WALL_CONFIG", "/config/config.yaml")

_active_supervisor: CameraWallSupervisor | None = None


class CameraWallSupervisor:
    def __init__(self, config_path: str | Path) -> None:
        self.config_path = Path(config_path)
        self.stop_requested = threading.Event()
        self.restart_requested = threading.Event()
        self._lock = threading.Lock()
        self._current_process: subprocess.Popen[str] | None = None
        self._restart_reason: str | None = None
        self._last_error: str | None = None
        self._last_exit_code: int | None = None
        self._last_command: str | None = None
        self._last_started_at: str | None = None
        self._runtime: dict[str, object] = {}
        self._restart_count = 0
        self._restart_delay_seconds = 5
        self._gpu_monitor = GpuMonitor.from_env()
        self._input_health = InputHealthTracker()
        self._gpu_monitor.start()

    def run(self) -> int:
        logging.info("Starting camera wall supervisor with config %s", self.config_path)
        exit_code = 0
        try:
            while not self.stop_requested.is_set():
                self._consume_restart_request()
                try:
                    config = load_config(self.config_path)
                    self._input_health.configure(config)
                    command = build_ffmpeg_command(config)
                    self._restart_delay_seconds = config.ffmpeg.restart_delay_seconds
                except ConfigError as exc:
                    self._set_state(last_error=str(exc), last_command=None, runtime={})
                    self._input_health.clear(str(exc))
                    logging.error("Configuration error: %s", exc)
                    self._sleep_or_wake(self._restart_delay_seconds)
                    continue

                rendered_command = masked_command(command)
                self._set_state(
                    last_error=None,
                    last_command=rendered_command,
                    runtime=_runtime_summary(config),
                )
                logging.info("FFmpeg command: %s", rendered_command)
                exit_code = self._run_once(command)
                if self.stop_requested.is_set():
                    return exit_code
                if self.restart_requested.is_set():
                    reason = self._consume_restart_request()
                    logging.info("Restarting FFmpeg after %s", reason or "request")
                    continue
                logging.warning(
                    "FFmpeg exited with code %s; restarting in %s seconds",
                    exit_code,
                    self._restart_delay_seconds,
                )
                self._sleep_or_wake(self._restart_delay_seconds)
            return exit_code
        finally:
            self._gpu_monitor.stop()

    def request_restart(self, reason: str) -> None:
        with self._lock:
            self._restart_reason = reason
        self.restart_requested.set()
        process = self._current_process
        if process and process.poll() is None:
            logging.info("Restart requested by %s; stopping FFmpeg pid %s", reason, process.pid)

    def stop(self) -> None:
        self.stop_requested.set()
        process = self._current_process
        if process and process.poll() is None:
            logging.info("Stopping FFmpeg pid %s", process.pid)
            self._terminate_process(process)
        self._gpu_monitor.stop()

    def status_snapshot(self) -> dict[str, object]:
        process = self._current_process
        ffmpeg_running = bool(process and process.poll() is None)
        with self._lock:
            return {
                "config_path": str(self.config_path),
                "ffmpeg_running": ffmpeg_running,
                "pid": process.pid if ffmpeg_running and process else None,
                "restart_requested": self.restart_requested.is_set(),
                "restart_reason": self._restart_reason,
                "restart_count": self._restart_count,
                "last_error": self._last_error,
                "last_exit_code": self._last_exit_code,
                "last_started_at": self._last_started_at,
                "last_command": self._last_command,
                "runtime": self._runtime,
                "input_health": self._input_health.snapshot(),
                "gpu": self._gpu_monitor.snapshot(),
            }

    def _run_once(self, command: list[str]) -> int:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        reader: threading.Thread | None = None
        try:
            self._current_process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            reader = threading.Thread(
                target=_log_process_output,
                args=(self._current_process, self._input_health.process_ffmpeg_line),
                name="ffmpeg-output",
                daemon=True,
            )
            reader.start()
            PID_FILE.write_text(str(self._current_process.pid), encoding="utf-8")
            self._set_state(last_started_at=_utc_now(), last_exit_code=None)
            self._input_health.mark_started()
            logging.info("FFmpeg started with pid %s", self._current_process.pid)
            while True:
                exit_code = self._current_process.poll()
                if exit_code is not None:
                    self._set_state(last_exit_code=exit_code)
                    self._input_health.mark_stopped(exit_code)
                    return exit_code
                if self.stop_requested.is_set() or self.restart_requested.is_set():
                    requested_restart = self.restart_requested.is_set()
                    self._input_health.mark_restarting()
                    self._terminate_process(self._current_process)
                    exit_code = self._current_process.wait()
                    self._set_state(last_exit_code=exit_code)
                    if not requested_restart:
                        self._input_health.mark_stopped(exit_code)
                    return exit_code
                time.sleep(0.5)
        except FileNotFoundError:
            logging.error("FFmpeg binary not found: %s", command[0])
            self._set_state(last_exit_code=127, last_error=f"FFmpeg binary not found: {command[0]}")
            return 127
        finally:
            if reader:
                reader.join(timeout=2)
            self._current_process = None
            try:
                PID_FILE.unlink()
            except FileNotFoundError:
                pass

    def _terminate_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logging.warning("FFmpeg did not stop after SIGTERM; killing pid %s", process.pid)
            process.kill()

    def _set_state(self, **values: object) -> None:
        with self._lock:
            for key, value in values.items():
                setattr(self, f"_{key}", value)

    def _consume_restart_request(self) -> str | None:
        if not self.restart_requested.is_set():
            return None
        self.restart_requested.clear()
        with self._lock:
            reason = self._restart_reason
            self._restart_reason = None
            self._restart_count += 1
        return reason

    def _sleep_or_wake(self, seconds: int) -> None:
        deadline = time.monotonic() + max(0, seconds)
        while not self.stop_requested.is_set() and not self.restart_requested.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.5, remaining))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the TrueNAS camera wall FFmpeg supervisor")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to config YAML")
    parser.add_argument(
        "--print-command",
        action="store_true",
        help="Print the sanitized FFmpeg command and exit",
    )
    parser.add_argument("--no-web", action="store_true", help="Disable the admin web UI")
    parser.add_argument("--web-only", action="store_true", help="Start the admin web UI without FFmpeg")
    args = parser.parse_args(argv)

    _configure_logging()

    if args.print_command:
        try:
            config = load_config(args.config)
            command = build_ffmpeg_command(config)
        except ConfigError as exc:
            logging.error("Configuration error: %s", exc)
            return 2
        print(masked_command(command))
        return 0

    supervisor = CameraWallSupervisor(args.config)
    _install_signal_handlers(supervisor)
    server = None
    web_thread = None

    if not args.no_web and _env_bool("CAMERA_WALL_WEB_ENABLED", True):
        settings = _web_settings()
        if settings:
            try:
                server = start_admin_server(supervisor.config_path, supervisor, settings)
            except OSError as exc:
                logging.error("Could not start admin web UI: %s", exc)
                return 2
            web_thread = threading.Thread(
                target=server.serve_forever,
                name="camera-wall-admin",
                daemon=True,
            )
            web_thread.start()
            logging.info(
                "Admin web UI listening on http://%s:%s",
                settings.host,
                settings.port,
            )

    if args.web_only:
        if not server:
            logging.error("Web-only mode requires an enabled admin web UI")
            return 2
        try:
            while not supervisor.stop_requested.is_set():
                time.sleep(0.5)
            return 0
        finally:
            _shutdown_server(server, web_thread)

    try:
        return supervisor.run()
    finally:
        _shutdown_server(server, web_thread)


def _web_settings() -> WebSettings | None:
    password = os.environ.get("CAMERA_WALL_ADMIN_PASSWORD", "")
    if not password:
        logging.warning("Admin web UI disabled because CAMERA_WALL_ADMIN_PASSWORD is not set")
        return None
    try:
        port = int(os.environ.get("CAMERA_WALL_WEB_PORT", "8088"))
    except ValueError:
        logging.error("CAMERA_WALL_WEB_PORT must be an integer")
        return None
    return WebSettings(
        host=os.environ.get("CAMERA_WALL_WEB_HOST", "0.0.0.0"),
        port=port,
        username=os.environ.get("CAMERA_WALL_ADMIN_USER", "admin"),
        password=password,
    )


def _shutdown_server(server: object | None, thread: threading.Thread | None) -> None:
    if not server:
        return
    server.shutdown()
    server.server_close()
    if thread:
        thread.join(timeout=2)


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("CAMERA_WALL_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    install_log_buffer()


def _install_signal_handlers(supervisor: CameraWallSupervisor) -> None:
    global _active_supervisor
    _active_supervisor = supervisor
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)


def _handle_stop(signum: int, _frame: object) -> None:
    logging.info("Received signal %s", signum)
    if _active_supervisor:
        _active_supervisor.stop()


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _runtime_summary(config: AppConfig) -> dict[str, object]:
    output = config.output
    ffmpeg = config.ffmpeg
    return {
        "output_url": output.url,
        "resolution": f"{output.width}x{output.height}",
        "fps": output.fps,
        "bitrate": output.bitrate,
        "encoder": output.encoder,
        "input_hwaccel": ffmpeg.input_hwaccel,
        "enabled_inputs": len(config.enabled_inputs),
    }


def _log_process_output(process: subprocess.Popen[str], on_line: object | None = None) -> None:
    if not process.stdout:
        return
    for raw_line in process.stdout:
        line = raw_line.rstrip()
        if line:
            if callable(on_line):
                on_line(line)
            logging.warning("ffmpeg %s", mask_text(line))

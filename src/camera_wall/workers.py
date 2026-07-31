from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
import logging
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from .config import AppConfig, InputConfig
from .ffmpeg import mask_text, mask_url, masked_command


@dataclass(frozen=True)
class RemuxSlot:
    index: int
    input_cfg: InputConfig
    output_url: str
    command: list[str]

    @property
    def name(self) -> str:
        return self.input_cfg.name


@dataclass
class _WorkerRuntime:
    slot: RemuxSlot
    restart_delay_seconds: int
    process: subprocess.Popen[str] | None = None
    reader: threading.Thread | None = None
    state: str = "stopped"
    restarts: int = 0
    last_exit_code: int | None = None
    last_error: str | None = None
    started_at: str | None = None
    next_start_after: float = 0


class RemuxWorkerManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: dict[str, _WorkerRuntime] = {}

    def reconcile(self, config: AppConfig) -> bool:
        if not config.workers.enabled:
            changed = bool(self.snapshot())
            self.stop()
            return changed

        desired = {slot.name: slot for slot in build_remux_slots(config)}
        changed = False
        for name in self._worker_names():
            worker = self._worker(name)
            slot = desired.get(name)
            if not worker or slot is None or worker.slot.command != slot.command:
                self._stop_and_remove(name)
                changed = True

        for slot in desired.values():
            if self._worker(slot.name):
                continue
            worker = _WorkerRuntime(
                slot=slot,
                restart_delay_seconds=config.workers.restart_delay_seconds,
            )
            with self._lock:
                self._workers[slot.name] = worker
            self._start_worker(worker, initial=True)
            changed = True
        return changed

    def poll(self) -> None:
        now = time.monotonic()
        for worker in self._workers_snapshot():
            process = worker.process
            if process is not None:
                exit_code = process.poll()
                if exit_code is None:
                    continue
                if worker.reader:
                    worker.reader.join(timeout=1)
                with self._lock:
                    worker.process = None
                    worker.reader = None
                    worker.last_exit_code = exit_code
                    worker.state = "stopped" if exit_code == 0 else "failed"
                    worker.last_error = (
                        None if exit_code == 0 else f"Worker exited with code {exit_code}"
                    )
                    worker.next_start_after = now + worker.restart_delay_seconds
                logging.warning(
                    "Worker %s exited with code %s; restarting in %s seconds",
                    worker.slot.name,
                    exit_code,
                    worker.restart_delay_seconds,
                )
                continue

            if worker.next_start_after and now >= worker.next_start_after:
                with self._lock:
                    worker.restarts += 1
                    worker.next_start_after = 0
                self._start_worker(worker, initial=False)

    def stop(self) -> None:
        for name in self._worker_names():
            self._stop_and_remove(name)

    def snapshot(self) -> list[dict[str, object]]:
        payloads: list[dict[str, object]] = []
        for worker in self._workers_snapshot():
            process = worker.process
            running = bool(process and process.poll() is None)
            payloads.append(
                {
                    "index": worker.slot.index,
                    "name": worker.slot.name,
                    "state": "running" if running else worker.state,
                    "pid": process.pid if running and process else None,
                    "restarts": worker.restarts,
                    "last_exit_code": worker.last_exit_code,
                    "last_error": worker.last_error,
                    "started_at": worker.started_at,
                    "source_url": mask_url(worker.slot.input_cfg.url),
                    "output_url": mask_url(worker.slot.output_url),
                    "command": masked_command(worker.slot.command),
                }
            )
        return sorted(payloads, key=lambda item: int(item["index"]))

    def _start_worker(self, worker: _WorkerRuntime, initial: bool) -> None:
        name = worker.slot.name
        if initial:
            logging.info("Worker %s command: %s", name, masked_command(worker.slot.command))
        else:
            logging.info("Restarting worker %s", name)
        try:
            process = subprocess.Popen(
                worker.slot.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError:
            with self._lock:
                worker.process = None
                worker.reader = None
                worker.state = "failed"
                worker.last_exit_code = 127
                worker.last_error = f"FFmpeg binary not found: {worker.slot.command[0]}"
                worker.next_start_after = time.monotonic() + worker.restart_delay_seconds
            logging.error("Worker %s FFmpeg binary not found: %s", name, worker.slot.command[0])
            return

        reader = threading.Thread(
            target=_log_worker_output,
            args=(name, process),
            name=f"worker-{name}-output",
            daemon=True,
        )
        reader.start()
        with self._lock:
            worker.process = process
            worker.reader = reader
            worker.state = "running"
            worker.last_error = None
            worker.last_exit_code = None
            worker.started_at = _utc_now()
        logging.info("Worker %s started with pid %s", name, process.pid)

    def _stop_and_remove(self, name: str) -> None:
        worker = self._worker(name)
        if not worker:
            return
        process = worker.process
        if process and process.poll() is None:
            logging.info("Stopping worker %s pid %s", name, process.pid)
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logging.warning(
                    "Worker %s did not stop after SIGTERM; killing pid %s",
                    name,
                    process.pid,
                )
                process.kill()
        if worker.reader:
            worker.reader.join(timeout=1)
        with self._lock:
            self._workers.pop(name, None)

    def _worker(self, name: str) -> _WorkerRuntime | None:
        with self._lock:
            return self._workers.get(name)

    def _worker_names(self) -> list[str]:
        with self._lock:
            return list(self._workers)

    def _workers_snapshot(self) -> list[_WorkerRuntime]:
        with self._lock:
            return list(self._workers.values())


def build_remux_slots(config: AppConfig) -> tuple[RemuxSlot, ...]:
    slots: list[RemuxSlot] = []
    for index, input_cfg in enumerate(config.inputs):
        if not input_cfg.enabled:
            continue
        output_url = worker_output_url(config, input_cfg, index)
        slots.append(
            RemuxSlot(
                index=index,
                input_cfg=input_cfg,
                output_url=output_url,
                command=build_remux_worker_command(config, input_cfg, output_url),
            )
        )
    return tuple(slots)


def build_worker_wall_config(config: AppConfig) -> AppConfig:
    if not config.workers.enabled:
        return config
    output_urls = {
        slot.input_cfg.name: slot.output_url
        for slot in build_remux_slots(config)
    }
    inputs = tuple(
        replace(input_cfg, url=output_urls[input_cfg.name])
        if input_cfg.enabled and input_cfg.name in output_urls
        else input_cfg
        for input_cfg in config.inputs
    )
    return replace(config, inputs=inputs)


def build_remux_worker_command(
    config: AppConfig, input_cfg: InputConfig, output_url: str
) -> list[str]:
    ffmpeg = config.ffmpeg
    workers = config.workers
    args = [
        ffmpeg.binary,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        ffmpeg.log_level,
        "-thread_queue_size",
        "512",
        "-fflags",
        "+genpts",
    ]
    if _is_rtsp_url(input_cfg.url):
        args.extend(["-rtsp_transport", ffmpeg.input_rtsp_transport])
    if _is_http_url(input_cfg.url):
        args.extend(
            [
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                str(ffmpeg.http_reconnect_delay_max_seconds),
            ]
        )
    if ffmpeg.input_timeout_seconds > 0:
        args.extend(["-rw_timeout", str(ffmpeg.input_timeout_seconds * 1_000_000)])
    args.extend(
        [
            "-i",
            input_cfg.url,
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
            "-f",
            "rtsp",
            "-rtsp_transport",
            workers.rtsp_transport,
            "-muxdelay",
            "0.1",
            output_url,
        ]
    )
    return args


def worker_output_url(config: AppConfig, input_cfg: InputConfig, index: int) -> str:
    slug = _slug(input_cfg.name, index)
    template = config.workers.output_template.strip()
    if template:
        return template.replace("{name}", slug).replace("{index}", str(index + 1))
    return _derive_output_url(config.output.url, slug)


def _derive_output_url(output_url: str, slug: str) -> str:
    parsed = urlsplit(output_url)
    base_path = parsed.path.rstrip("/") or "/camera_wall"
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"{base_path}_{slug}",
            parsed.query,
            parsed.fragment,
        )
    )


def _slug(name: str, index: int) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_.-")
    return value or f"camera_{index + 1}"


def _is_rtsp_url(value: str) -> bool:
    return value.lower().startswith(("rtsp://", "rtsps://"))


def _is_http_url(value: str) -> bool:
    return value.lower().startswith(("http://", "https://"))


def _log_worker_output(name: str, process: subprocess.Popen[str]) -> None:
    if not process.stdout:
        return
    for raw_line in process.stdout:
        line = raw_line.rstrip()
        if line:
            logging.warning("worker %s %s", name, mask_text(line))


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
import logging
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from .config import AppConfig, InputConfig
from .diagnostics import StreamProbeRequest, diagnose_stream
from .ffmpeg import mask_text, mask_url, masked_command


@dataclass(frozen=True)
class RemuxSlot:
    index: int
    input_cfg: InputConfig
    output_url: str
    wall_input_url: str
    command: list[str]
    mode: str = "remux"
    fallback_command: list[str] | None = None
    retry_live_seconds: int = 15
    retry_probe_timeout_seconds: int = 3
    stall_timeout_seconds: int = 3
    input_rtsp_transport: str = "tcp"

    @property
    def name(self) -> str:
        return self.input_cfg.name


@dataclass
class _WorkerRuntime:
    slot: RemuxSlot
    restart_delay_seconds: int
    process: subprocess.Popen[str] | None = None
    reader: threading.Thread | None = None
    process_kind: str = "live"
    state: str = "stopped"
    restarts: int = 0
    last_exit_code: int | None = None
    last_error: str | None = None
    started_at: str | None = None
    last_progress_at: float = 0
    next_start_after: float = 0
    next_live_retry_at: float = 0
    source_state: str | None = None
    last_source_at: str | None = None


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
            if not worker or slot is None or worker.slot != slot:
                self._stop_and_remove(name)
                changed = True
            elif worker.restart_delay_seconds != config.workers.restart_delay_seconds:
                with self._lock:
                    worker.restart_delay_seconds = config.workers.restart_delay_seconds

        for slot in desired.values():
            if self._worker(slot.name):
                continue
            worker = _WorkerRuntime(
                slot=slot,
                restart_delay_seconds=config.workers.restart_delay_seconds,
            )
            with self._lock:
                self._workers[slot.name] = worker
            self._start_live(worker, initial=True)
            changed = True
        return changed

    def poll(self) -> None:
        now = time.monotonic()
        for worker in self._workers_snapshot():
            process = worker.process
            if process is not None:
                exit_code = process.poll()
                if exit_code is None:
                    if self._is_stalled(worker, now):
                        if worker.process_kind == "stable":
                            logging.warning(
                                "Worker %s has no output progress for %s seconds; restarting stable worker",
                                worker.slot.name,
                                self._stall_timeout(worker),
                            )
                        else:
                            logging.warning(
                                "Worker %s has no output progress for %s seconds; switching to fallback",
                                worker.slot.name,
                                self._stall_timeout(worker),
                            )
                        self._stop_process(worker, kill=True)
                        self._handle_exit(worker, -9, now)
                    elif self._should_retry_live(worker, now):
                        self._retry_live_from_fallback(worker, now)
                    continue
                self._handle_exit(worker, exit_code, now)
                continue

            if worker.next_start_after and now >= worker.next_start_after:
                self._start_preferred(worker)

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
                    "mode": worker.process_kind,
                    "pid": process.pid if running and process else None,
                    "restarts": worker.restarts,
                    "last_exit_code": worker.last_exit_code,
                    "last_error": worker.last_error,
                    "started_at": worker.started_at,
                    "source_state": worker.source_state,
                    "last_source_at": worker.last_source_at,
                    "source_url": mask_url(worker.slot.input_cfg.url),
                    "output_url": mask_url(worker.slot.output_url),
                    "wall_input_url": mask_url(worker.slot.wall_input_url),
                    "command": masked_command(worker.slot.command),
                }
            )
        return sorted(payloads, key=lambda item: int(item["index"]))

    def _start_preferred(self, worker: _WorkerRuntime) -> None:
        with self._lock:
            worker.restarts += 1
            worker.next_start_after = 0
        self._start_live(worker)

    def _start_live(self, worker: _WorkerRuntime, initial: bool = False) -> None:
        process_kind = "stable" if worker.slot.mode == "stable" else "live"
        self._start_worker(worker, process_kind, worker.slot.command, initial)

    def _start_fallback(self, worker: _WorkerRuntime) -> None:
        if not worker.slot.fallback_command:
            with self._lock:
                worker.process = None
                worker.reader = None
                worker.process_kind = "live"
                worker.state = "failed"
                worker.next_start_after = time.monotonic() + worker.restart_delay_seconds
            return
        self._start_worker(worker, "fallback", worker.slot.fallback_command, initial=False)

    def _start_worker(
        self,
        worker: _WorkerRuntime,
        process_kind: str,
        command: list[str],
        initial: bool,
    ) -> None:
        name = worker.slot.name
        if initial:
            logging.info("Worker %s command: %s", name, masked_command(command))
        else:
            logging.info("Starting worker %s in %s mode", name, process_kind)
        try:
            process = subprocess.Popen(
                command,
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
                worker.process_kind = process_kind
                worker.state = "failed"
                worker.last_exit_code = 127
                worker.last_error = f"FFmpeg binary not found: {command[0]}"
                worker.next_start_after = time.monotonic() + worker.restart_delay_seconds
            logging.error("Worker %s FFmpeg binary not found: %s", name, command[0])
            return

        reader = threading.Thread(
            target=_log_worker_output,
            args=(name, process, worker),
            name=f"worker-{name}-output",
            daemon=True,
        )
        reader.start()
        now = time.monotonic()
        with self._lock:
            worker.process = process
            worker.reader = reader
            worker.process_kind = process_kind
            worker.state = "running"
            worker.last_error = None
            worker.last_exit_code = None
            worker.started_at = _utc_now()
            worker.last_progress_at = now
            worker.next_start_after = 0
            if process_kind == "live":
                worker.next_live_retry_at = 0
        logging.info("Worker %s started with pid %s", name, process.pid)

    def _stop_and_remove(self, name: str) -> None:
        worker = self._worker(name)
        if not worker:
            return
        self._stop_process(worker)
        with self._lock:
            self._workers.pop(name, None)

    def _stop_process(self, worker: _WorkerRuntime, kill: bool = False) -> None:
        process = worker.process
        if process and process.poll() is None:
            logging.info("Stopping worker %s pid %s", worker.slot.name, process.pid)
            if kill:
                process.kill()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    logging.warning(
                        "Worker %s did not stop after SIGKILL; leaving process cleanup to OS",
                        worker.slot.name,
                    )
            else:
                self._terminate_process(worker, process)
        if worker.reader:
            worker.reader.join(timeout=1)
        with self._lock:
            worker.process = None
            worker.reader = None

    def _terminate_process(
        self, worker: _WorkerRuntime, process: subprocess.Popen[str]
    ) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logging.warning(
                "Worker %s did not stop after SIGTERM; killing pid %s",
                worker.slot.name,
                process.pid,
            )
            process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                logging.warning(
                    "Worker %s did not stop after SIGKILL; leaving process cleanup to OS",
                    worker.slot.name,
                )

    def _handle_exit(self, worker: _WorkerRuntime, exit_code: int, now: float) -> None:
        if worker.reader:
            worker.reader.join(timeout=1)
        process_kind = worker.process_kind
        with self._lock:
            worker.process = None
            worker.reader = None
            worker.last_exit_code = exit_code
            worker.state = "stopped" if exit_code == 0 else "failed"
            worker.last_error = None if exit_code == 0 else f"Worker exited with code {exit_code}"

        if process_kind == "live" and worker.slot.fallback_command:
            with self._lock:
                worker.next_live_retry_at = now + self._retry_live_seconds(worker)
            logging.warning(
                "Worker %s live input exited with code %s; starting fallback",
                worker.slot.name,
                exit_code,
            )
            self._start_fallback(worker)
            return

        with self._lock:
            worker.next_start_after = now + worker.restart_delay_seconds
        logging.warning(
            "Worker %s exited with code %s; restarting in %s seconds",
            worker.slot.name,
            exit_code,
            worker.restart_delay_seconds,
        )

    def _is_stalled(self, worker: _WorkerRuntime, now: float) -> bool:
        if worker.process_kind not in {"live", "stable"}:
            return False
        stall_timeout = self._stall_timeout(worker)
        if stall_timeout <= 0:
            return False
        return bool(worker.process and now - worker.last_progress_at > stall_timeout)

    def _should_retry_live(self, worker: _WorkerRuntime, now: float) -> bool:
        return (
            worker.process_kind == "fallback"
            and worker.next_live_retry_at > 0
            and now >= worker.next_live_retry_at
        )

    def _stall_timeout(self, worker: _WorkerRuntime) -> int:
        return max(0, worker.slot.stall_timeout_seconds)

    def _retry_live_seconds(self, worker: _WorkerRuntime) -> int:
        return max(1, worker.slot.retry_live_seconds)

    def _retry_live_from_fallback(self, worker: _WorkerRuntime, now: float) -> None:
        result = self._probe_live(worker)
        if not result.get("ok"):
            with self._lock:
                worker.next_live_retry_at = now + self._retry_live_seconds(worker)
                worker.last_error = str(result.get("message") or "Live probe failed")
            logging.info(
                "Worker %s live probe failed while fallback stays active: %s",
                worker.slot.name,
                result.get("message") or "Live probe failed",
            )
            return

        logging.info("Worker %s live probe succeeded; switching from fallback", worker.slot.name)
        self._stop_process(worker)
        self._start_live(worker)

    def _probe_live(self, worker: _WorkerRuntime) -> dict[str, object]:
        request = StreamProbeRequest(
            url=worker.slot.input_cfg.url,
            name=worker.slot.name,
            rtsp_transport=worker.slot.input_rtsp_transport,
            timeout_seconds=worker.slot.retry_probe_timeout_seconds,
        )
        return diagnose_stream(request)

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
        mode = config.workers.mode
        command = (
            build_stable_worker_command(config, input_cfg, output_url)
            if mode == "stable"
            else build_remux_worker_command(config, input_cfg, output_url)
        )
        fallback_command = None
        if mode == "remux" and config.workers.fallback_enabled:
            fallback_command = build_fallback_worker_command(config, input_cfg, output_url)
        slots.append(
            RemuxSlot(
                index=index,
                input_cfg=input_cfg,
                output_url=output_url,
                wall_input_url=worker_wall_input_url(config, input_cfg, index, output_url),
                command=command,
                mode=mode,
                fallback_command=fallback_command,
                retry_live_seconds=config.workers.retry_live_seconds,
                retry_probe_timeout_seconds=config.workers.retry_probe_timeout_seconds,
                stall_timeout_seconds=config.workers.stall_timeout_seconds,
                input_rtsp_transport=config.ffmpeg.input_rtsp_transport,
            )
        )
    return tuple(slots)


def build_worker_wall_config(config: AppConfig) -> AppConfig:
    if not config.workers.enabled:
        return config
    output_urls = {
        slot.input_cfg.name: slot.wall_input_url
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
            "-progress",
            "pipe:1",
            "-stats_period",
            _progress_interval(config),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
        ]
    )
    args.extend(_worker_output_args(config, output_url))
    return args


def build_stable_worker_command(
    config: AppConfig, input_cfg: InputConfig, output_url: str
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "camera_wall.slot_worker",
        "--ffmpeg-binary",
        config.ffmpeg.binary,
        "--log-level",
        config.ffmpeg.log_level,
        "--name",
        input_cfg.name,
        "--input-url",
        input_cfg.url,
        "--output-url",
        output_url,
        "--width",
        str(input_cfg.width),
        "--height",
        str(input_cfg.height),
        "--fps",
        str(config.output.fps),
        "--preserve-aspect",
        "true" if input_cfg.preserve_aspect else "false",
        "--pad-color",
        input_cfg.pad_color,
        "--offline-text",
        f"{input_cfg.label or input_cfg.name} offline",
        "--input-rtsp-transport",
        config.ffmpeg.input_rtsp_transport,
        "--input-hwaccel",
        config.ffmpeg.input_hwaccel,
        "--hwaccel-device",
        config.ffmpeg.hwaccel_device,
        "--input-timeout-seconds",
        str(config.ffmpeg.input_timeout_seconds),
        "--http-reconnect-delay-max-seconds",
        str(config.ffmpeg.http_reconnect_delay_max_seconds),
        "--slot-transport",
        config.workers.slot_transport,
        "--worker-rtsp-transport",
        config.workers.rtsp_transport,
        "--restart-delay-seconds",
        str(config.workers.restart_delay_seconds),
        "--stall-timeout-seconds",
        str(config.workers.stall_timeout_seconds),
        "--bitrate",
        _stable_slot_bitrate(config),
    ]


def build_fallback_worker_command(
    config: AppConfig, input_cfg: InputConfig, output_url: str
) -> list[str]:
    fps = config.output.fps
    text = _escape_drawtext(f"{input_cfg.label or input_cfg.name} offline")
    source = (
        f"color=c=black:s={input_cfg.width}x{input_cfg.height}:r={fps},"
        "format=yuv420p,"
        "drawtext="
        f"text='{text}':"
        "x=(w-text_w)/2:y=(h-text_h)/2:"
        "fontcolor=white@0.76:fontsize=30:"
        "box=1:boxcolor=black@0.62:boxborderw=10"
    )
    args = [
        config.ffmpeg.binary,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        config.ffmpeg.log_level,
        "-re",
        "-f",
        "lavfi",
        "-i",
        source,
        "-progress",
        "pipe:1",
        "-stats_period",
        _progress_interval(config),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-profile:v",
        "baseline",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        "350k",
        "-maxrate",
        "350k",
        "-bufsize",
        "700k",
        "-g",
        str(fps * 2),
        "-keyint_min",
        str(fps),
        "-r",
        str(fps),
    ]
    args.extend(_worker_output_args(config, output_url))
    return args


def worker_output_url(config: AppConfig, input_cfg: InputConfig, index: int) -> str:
    slug = _slug(input_cfg.name, index)
    template = config.workers.output_template.strip()
    if template:
        return template.replace("{name}", slug).replace("{index}", str(index + 1))
    if config.workers.slot_transport == "udp_mpegts":
        return f"udp://127.0.0.1:{config.workers.udp_base_port + index}?pkt_size=1316"
    return _derive_output_url(config.output.url, slug)


def worker_wall_input_url(
    config: AppConfig, input_cfg: InputConfig, index: int, output_url: str
) -> str:
    slug = _slug(input_cfg.name, index)
    template = config.workers.wall_input_template.strip()
    if template:
        return template.replace("{name}", slug).replace("{index}", str(index + 1))
    if config.workers.slot_transport == "udp_mpegts":
        parsed = urlsplit(output_url)
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                "fifo_size=5000000&overrun_nonfatal=1",
                parsed.fragment,
            )
        )
    return output_url


def _worker_output_args(config: AppConfig, output_url: str) -> list[str]:
    if config.workers.slot_transport == "udp_mpegts":
        return ["-f", "mpegts", "-muxdelay", "0", output_url]
    return [
        "-f",
        "rtsp",
        "-rtsp_transport",
        config.workers.rtsp_transport,
        "-muxdelay",
        "0.1",
        output_url,
    ]


def _progress_interval(config: AppConfig) -> str:
    timeout = config.workers.stall_timeout_seconds
    if timeout <= 0:
        return "5"
    return str(max(1, min(5, timeout // 2 or 1)))


def _stable_slot_bitrate(config: AppConfig) -> str:
    match = re.match(r"^(\d+)([kKmM]?)$", config.output.bitrate)
    if not match:
        return "1200k"
    amount_text, suffix = match.groups()
    amount = int(amount_text)
    count = max(1, len(config.enabled_inputs))
    if suffix.lower() == "m":
        kbps = amount * 1000 // count
    elif suffix.lower() == "k":
        kbps = amount // count
    else:
        kbps = amount // 1000 // count
    return f"{max(500, kbps)}k"


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


def _escape_drawtext(value: str) -> str:
    return (
        value.replace("\\", r"\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace(",", r"\,")
        .replace("%", r"\%")
    )


def _log_worker_output(
    name: str, process: subprocess.Popen[str], worker: _WorkerRuntime
) -> None:
    if not process.stdout:
        return
    for raw_line in process.stdout:
        line = raw_line.rstrip()
        if line:
            if _update_worker_state_line(line, worker):
                continue
            if _is_progress_line(line):
                worker.last_progress_at = time.monotonic()
                continue
            logging.warning("worker %s %s", name, mask_text(line))


def _update_worker_state_line(line: str, worker: _WorkerRuntime) -> bool:
    if not line.startswith("camera_wall_source="):
        return False
    worker.last_progress_at = time.monotonic()
    worker.source_state = line.split("=", 1)[1].strip() or "unknown"
    worker.last_source_at = _utc_now()
    return True


def _is_progress_line(line: str) -> bool:
    return line.startswith(
        (
            "bitrate=",
            "drop_frames=",
            "dup_frames=",
            "fps=",
            "frame=",
            "out_time=",
            "out_time_ms=",
            "out_time_us=",
            "progress=",
            "speed=",
            "stream_",
            "total_size=",
        )
    )


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

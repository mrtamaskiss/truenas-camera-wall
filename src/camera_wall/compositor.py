from __future__ import annotations

import argparse
from dataclasses import dataclass
from dataclasses import field
import logging
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .config import AppConfig, InputConfig, load_config
from .ffmpeg import build_rawvideo_output_command, mask_text, masked_command
from .slot_worker import (
    SlotSettings,
    black_yuv420p_frame,
    build_offline_frame_command,
)


@dataclass
class CameraRuntime:
    config: InputConfig
    process: subprocess.Popen[bytes] | None = None
    reader: threading.Thread | None = None
    stderr_reader: threading.Thread | None = None
    frame: bytes | None = None
    frame_at: float = 0
    decoder_started_at: float = 0
    last_read_at: float = 0
    fast_frame_streak: int = 0
    normal_frame_streak: int = 0
    catching_up: bool = False
    catching_up_until: float = 0
    next_start_at: float = 0
    state: str = "offline"
    lock: threading.Lock = field(default_factory=threading.Lock)


class DirectCompositor:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.stop_requested = threading.Event()
        self.cameras = [CameraRuntime(input_cfg) for input_cfg in config.enabled_inputs]
        self.offline_frames = {
            input_cfg.name: render_offline_frame(config, input_cfg)
            for input_cfg in config.enabled_inputs
        }
        self.encoder: subprocess.Popen[bytes] | None = None
        self.encoder_stderr_reader: threading.Thread | None = None

    def run(self) -> int:
        self._start_encoder()
        frame_interval = 1 / max(1, self.config.output.fps)
        next_frame_at = time.monotonic()
        frames = 0
        last_progress_at = 0.0
        try:
            while not self.stop_requested.is_set():
                now = time.monotonic()
                if now < next_frame_at:
                    self.stop_requested.wait(min(0.25, next_frame_at - now))
                    continue

                self._poll_decoders()
                if self._encoder_exited():
                    self._start_encoder()

                frame = compose_frame(self.config, self.cameras, self.offline_frames)
                if self._write_frame(frame):
                    frames += 1
                else:
                    self._start_encoder()

                now = time.monotonic()
                if now - last_progress_at >= 5:
                    logging.info("compositor wrote %s frame(s)", frames)
                    last_progress_at = now

                next_frame_at += frame_interval
                if next_frame_at < now - frame_interval:
                    next_frame_at = now + frame_interval
            return 0
        finally:
            self._stop_decoders()
            self._stop_encoder()

    def request_stop(self, signum: int) -> None:
        logging.info("compositor received signal %s", signum)
        self.stop_requested.set()
        self._stop_encoder()

    def _poll_decoders(self) -> None:
        now = time.monotonic()
        for camera in self.cameras:
            if camera.process is not None:
                exit_code = camera.process.poll()
                if exit_code is not None:
                    logging.warning(
                        "camera %s decoder exited with code %s",
                        camera.config.name,
                        exit_code,
                    )
                    self._stop_decoder(camera)
                    camera.next_start_at = now + self.config.workers.restart_delay_seconds
                    self._set_camera_state(camera, "offline")
                    continue
                if self._camera_stalled(camera, now):
                    logging.warning(
                        "camera %s decoder produced no fresh frame for %s seconds; restarting decoder",
                        camera.config.name,
                        self.config.workers.stall_timeout_seconds,
                    )
                    self._stop_decoder(camera)
                    camera.next_start_at = now + self.config.workers.restart_delay_seconds
                    self._set_camera_state(camera, "offline")
                continue

            if now >= camera.next_start_at:
                self._start_decoder(camera)

    def _camera_stalled(self, camera: CameraRuntime, now: float) -> bool:
        timeout = max(0, self.config.workers.stall_timeout_seconds)
        if timeout <= 0:
            return False
        with camera.lock:
            if camera.catching_up and camera.last_read_at > camera.decoder_started_at:
                reference = camera.last_read_at
            else:
                reference = (
                    camera.frame_at
                    if camera.frame_at > camera.decoder_started_at
                    else camera.decoder_started_at
                )
        return now - reference > timeout

    def _start_decoder(self, camera: CameraRuntime) -> None:
        command = build_camera_decoder_command(self.config, camera.config)
        logging.info("camera %s decoder command: %s", camera.config.name, masked_command(command))
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError:
            logging.error("FFmpeg binary not found: %s", command[0])
            camera.next_start_at = time.monotonic() + self.config.workers.restart_delay_seconds
            self._set_camera_state(camera, "offline")
            return

        now = time.monotonic()
        had_previous_decoder = camera.decoder_started_at > 0
        catchup_seconds = (
            self.config.workers.freeze_timeout_seconds if had_previous_decoder else 0
        )
        with camera.lock:
            camera.frame = None
            camera.frame_at = 0
            camera.decoder_started_at = now
            camera.last_read_at = 0
            camera.fast_frame_streak = 0
            camera.normal_frame_streak = 0
            camera.catching_up = catchup_seconds > 0
            camera.catching_up_until = now + catchup_seconds if catchup_seconds > 0 else 0
        camera.process = process
        camera.reader = threading.Thread(
            target=_read_camera_frames,
            args=(
                camera,
                tile_frame_size(camera.config),
                self.config.output.fps,
                self.config.workers.freeze_timeout_seconds,
            ),
            name=f"compose-{camera.config.name}-decoder",
            daemon=True,
        )
        camera.stderr_reader = threading.Thread(
            target=_drain_stream,
            args=(f"camera {camera.config.name} decoder", process.stderr, self.stop_requested),
            name=f"compose-{camera.config.name}-decoder-log",
            daemon=True,
        )
        camera.reader.start()
        camera.stderr_reader.start()
        logging.info("camera %s decoder started with pid %s", camera.config.name, process.pid)
        if catchup_seconds > 0:
            logging.info(
                "camera %s holding tile offline for %s seconds while seeking live edge",
                camera.config.name,
                catchup_seconds,
            )

    def _stop_decoder(self, camera: CameraRuntime) -> None:
        process = camera.process
        if process is not None:
            _terminate_process(process)
        if camera.reader:
            camera.reader.join(timeout=2)
        if camera.stderr_reader:
            camera.stderr_reader.join(timeout=1)
        camera.process = None
        camera.reader = None
        camera.stderr_reader = None
        with camera.lock:
            camera.frame = None
            camera.frame_at = 0
            camera.last_read_at = 0
            camera.fast_frame_streak = 0
            camera.normal_frame_streak = 0
            camera.catching_up = False
            camera.catching_up_until = 0

    def _stop_decoders(self) -> None:
        for camera in self.cameras:
            self._stop_decoder(camera)

    def _set_camera_state(self, camera: CameraRuntime, state: str) -> None:
        if camera.state == state:
            return
        camera.state = state
        logging.info("camera %s source is %s", camera.config.name, state)

    def _start_encoder(self) -> None:
        self._stop_encoder()
        command = build_rawvideo_output_command(self.config)
        logging.info("compositor encoder command: %s", masked_command(command))
        try:
            self.encoder = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError:
            logging.error("FFmpeg binary not found: %s", command[0])
            self.encoder = None
            self.stop_requested.wait(self.config.ffmpeg.restart_delay_seconds)
            return
        self.encoder_stderr_reader = threading.Thread(
            target=_drain_stream,
            args=("compositor encoder", self.encoder.stderr, self.stop_requested),
            name="compose-encoder-log",
            daemon=True,
        )
        self.encoder_stderr_reader.start()
        logging.info("compositor encoder started with pid %s", self.encoder.pid)

    def _stop_encoder(self) -> None:
        process = self.encoder
        if process is None:
            return
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        _terminate_process(process)
        if self.encoder_stderr_reader:
            self.encoder_stderr_reader.join(timeout=1)
        self.encoder = None
        self.encoder_stderr_reader = None

    def _encoder_exited(self) -> bool:
        return bool(self.encoder and self.encoder.poll() is not None)

    def _write_frame(self, frame: bytes) -> bool:
        process = self.encoder
        if not process or not process.stdin or process.poll() is not None:
            return False
        try:
            stream = process.stdin
            view = memoryview(frame)
            while view:
                written = stream.write(view)
                if written is None:
                    stream.flush()
                    break
                if written <= 0:
                    raise BrokenPipeError("encoder stdin accepted zero bytes")
                view = view[written:]
            return True
        except (BrokenPipeError, OSError) as exc:
            logging.warning("compositor encoder pipe failed: %s", exc)
            return False


def build_camera_decoder_command(config: AppConfig, input_cfg: InputConfig) -> list[str]:
    ffmpeg = config.ffmpeg
    output = config.output
    args = [
        ffmpeg.binary,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        ffmpeg.log_level,
        "-thread_queue_size",
        "512",
        "-fflags",
        "+genpts+nobuffer+discardcorrupt",
        "-avioflags",
        "direct",
        "-flags",
        "low_delay",
        "-max_delay",
        "0",
        "-probesize",
        "262144",
        "-analyzeduration",
        "2000000",
        "-use_wallclock_as_timestamps",
        "1",
    ]
    if ffmpeg.input_hwaccel == "vaapi":
        args.extend(
            [
                "-hwaccel",
                "vaapi",
                "-hwaccel_device",
                ffmpeg.hwaccel_device,
                "-hwaccel_output_format",
                "vaapi",
            ]
        )
    if _is_rtsp_url(input_cfg.url):
        args.extend(["-rtsp_transport", ffmpeg.input_rtsp_transport])
        args.extend(["-reorder_queue_size", "0"])
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
            "-vf",
            _tile_filter(config, input_cfg),
            "-pix_fmt",
            "yuv420p",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
    )
    return args


def compose_frame(
    config: AppConfig,
    cameras: list[CameraRuntime],
    offline_frames: dict[str, bytes],
) -> bytes:
    output = config.output
    y_size = output.width * output.height
    uv_width = output.width // 2
    uv_size = y_size // 4
    y_plane = bytearray(b"\x10" * y_size)
    u_plane = bytearray(b"\x80" * uv_size)
    v_plane = bytearray(b"\x80" * uv_size)
    now = time.monotonic()
    timeout = max(0, config.workers.stall_timeout_seconds)

    for camera in cameras:
        input_cfg = camera.config
        with camera.lock:
            live_frame = camera.frame
            frame_at = camera.frame_at
            catching_up = camera.catching_up
        if live_frame and not catching_up and (timeout <= 0 or now - frame_at <= timeout):
            frame = live_frame
            state = "live"
        else:
            frame = offline_frames[input_cfg.name]
            state = "catching-up" if catching_up else "offline"
        if camera.state != state:
            camera.state = state
            logging.info("camera %s source is %s", input_cfg.name, state)
        _copy_tile(y_plane, u_plane, v_plane, output.width, uv_width, input_cfg, frame)

    return bytes(y_plane) + bytes(u_plane) + bytes(v_plane)


def render_offline_frame(config: AppConfig, input_cfg: InputConfig) -> bytes:
    settings = SlotSettings(
        ffmpeg_binary=config.ffmpeg.binary,
        log_level=config.ffmpeg.log_level,
        name=input_cfg.name,
        input_url=input_cfg.url,
        output_url="",
        width=input_cfg.width,
        height=input_cfg.height,
        fps=config.output.fps,
        preserve_aspect=input_cfg.preserve_aspect,
        pad_color=input_cfg.pad_color,
        offline_text=f"{input_cfg.label or input_cfg.name} offline",
        input_rtsp_transport=config.ffmpeg.input_rtsp_transport,
        input_hwaccel=config.ffmpeg.input_hwaccel,
        hwaccel_device=config.ffmpeg.hwaccel_device,
        input_timeout_seconds=config.ffmpeg.input_timeout_seconds,
        http_reconnect_delay_max_seconds=config.ffmpeg.http_reconnect_delay_max_seconds,
        slot_transport=config.workers.slot_transport,
        worker_rtsp_transport=config.workers.rtsp_transport,
        restart_delay_seconds=config.workers.restart_delay_seconds,
        stall_timeout_seconds=config.workers.stall_timeout_seconds,
        freeze_timeout_seconds=config.workers.freeze_timeout_seconds,
        bitrate=config.workers.stable_slot_bitrate,
    )
    command = build_offline_frame_command(settings)
    try:
        result = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logging.warning(
            "camera %s could not render offline frame; using black tile: %s",
            input_cfg.name,
            exc,
        )
        return black_yuv420p_frame(input_cfg.width, input_cfg.height)
    expected = tile_frame_size(input_cfg)
    if len(result.stdout) != expected:
        logging.warning(
            "camera %s offline frame has %s bytes, expected %s; using black tile",
            input_cfg.name,
            len(result.stdout),
            expected,
        )
        return black_yuv420p_frame(input_cfg.width, input_cfg.height)
    return result.stdout


def tile_frame_size(input_cfg: InputConfig) -> int:
    return input_cfg.width * input_cfg.height * 3 // 2


def _copy_tile(
    canvas_y: bytearray,
    canvas_u: bytearray,
    canvas_v: bytearray,
    canvas_width: int,
    canvas_uv_width: int,
    input_cfg: InputConfig,
    frame: bytes,
) -> None:
    tile_y_size = input_cfg.width * input_cfg.height
    tile_uv_width = input_cfg.width // 2
    tile_uv_height = input_cfg.height // 2
    tile_u_offset = tile_y_size
    tile_v_offset = tile_y_size + tile_y_size // 4

    for row in range(input_cfg.height):
        dst = (input_cfg.y + row) * canvas_width + input_cfg.x
        src = row * input_cfg.width
        canvas_y[dst : dst + input_cfg.width] = frame[src : src + input_cfg.width]

    canvas_uv_x = input_cfg.x // 2
    canvas_uv_y = input_cfg.y // 2
    for row in range(tile_uv_height):
        dst = (canvas_uv_y + row) * canvas_uv_width + canvas_uv_x
        src = row * tile_uv_width
        canvas_u[dst : dst + tile_uv_width] = frame[
            tile_u_offset + src : tile_u_offset + src + tile_uv_width
        ]
        canvas_v[dst : dst + tile_uv_width] = frame[
            tile_v_offset + src : tile_v_offset + src + tile_uv_width
        ]


def _read_camera_frames(
    camera: CameraRuntime,
    size: int,
    fps: int,
    catchup_timeout_seconds: int,
) -> None:
    process = camera.process
    if not process or not process.stdout:
        return
    while True:
        frame = _read_exact(process.stdout, size)
        if frame is None:
            return
        now = time.monotonic()
        with camera.lock:
            catching_up = _update_catchup_state(camera, now, fps, catchup_timeout_seconds)
            if not catching_up:
                camera.frame = frame
                camera.frame_at = now


def _update_catchup_state(
    camera: CameraRuntime,
    now: float,
    fps: int,
    catchup_timeout_seconds: int,
) -> bool:
    expected_interval = 1 / max(1, fps)
    fast_threshold = expected_interval * 0.4
    normal_threshold = expected_interval * 0.65
    previous = camera.last_read_at
    camera.last_read_at = now
    if previous <= 0:
        return camera.catching_up

    interval = now - previous
    if interval < fast_threshold:
        camera.fast_frame_streak += 1
        camera.normal_frame_streak = 0
    elif interval >= normal_threshold:
        camera.normal_frame_streak += 1
        camera.fast_frame_streak = 0

    if catchup_timeout_seconds <= 0:
        camera.catching_up = False
        camera.catching_up_until = 0
        return False

    start_streak = max(5, fps // 2)
    stop_streak = max(5, fps)
    hold_active = camera.catching_up_until > 0 and now < camera.catching_up_until
    if not camera.catching_up and camera.fast_frame_streak >= start_streak:
        camera.catching_up = True
        camera.catching_up_until = now + catchup_timeout_seconds
        camera.frame = None
        camera.frame_at = 0
        logging.warning(
            "camera %s appears to be playing buffered backlog; holding tile offline",
            camera.config.name,
        )
    elif (
        camera.catching_up
        and not hold_active
        and camera.normal_frame_streak >= stop_streak
    ):
        camera.catching_up = False
        camera.catching_up_until = 0
        camera.fast_frame_streak = 0
        logging.info("camera %s backlog cleared; accepting live frames", camera.config.name)

    return camera.catching_up


def _read_exact(stream: object, size: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)  # type: ignore[attr-defined]
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _drain_stream(
    label: str,
    stream: object | None,
    stop_requested: threading.Event,
) -> None:
    if stream is None:
        return
    while not stop_requested.is_set():
        raw = stream.readline()  # type: ignore[attr-defined]
        if not raw:
            return
        line = raw.decode("utf-8", errors="replace").rstrip()
        if line:
            logging.warning("%s %s", label, mask_text(line))


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def _tile_filter(config: AppConfig, input_cfg: InputConfig) -> str:
    prefix = "hwdownload,format=nv12," if config.ffmpeg.input_hwaccel == "vaapi" else ""
    if input_cfg.preserve_aspect:
        chain = (
            f"{prefix}fps={config.output.fps},"
            f"scale=w={input_cfg.width}:h={input_cfg.height}:force_original_aspect_ratio=decrease,"
            f"pad=w={input_cfg.width}:h={input_cfg.height}:x=(ow-iw)/2:y=(oh-ih)/2:"
            f"color={_escape_filter_value(input_cfg.pad_color)},"
            "setsar=1,format=yuv420p"
        )
    else:
        chain = (
            f"{prefix}fps={config.output.fps},"
            f"scale=w={input_cfg.width}:h={input_cfg.height},"
            "setsar=1,format=yuv420p"
        )
    if input_cfg.show_label and input_cfg.label:
        chain += (
            ",drawtext="
            f"text='{_escape_drawtext(input_cfg.label)}':"
            "x=12:y=h-th-12:fontcolor=white:fontsize=28:"
            "box=1:boxcolor=black@0.55:boxborderw=8"
        )
    return chain


def _validate_compose_config(config: AppConfig) -> None:
    values = [
        ("output.width", config.output.width),
        ("output.height", config.output.height),
    ]
    for index, input_cfg in enumerate(config.enabled_inputs):
        values.extend(
            [
                (f"inputs[{index}].x", input_cfg.x),
                (f"inputs[{index}].y", input_cfg.y),
                (f"inputs[{index}].width", input_cfg.width),
                (f"inputs[{index}].height", input_cfg.height),
            ]
        )
    odd = [name for name, value in values if value % 2]
    if odd:
        raise ValueError(
            "compose mode requires even output and tile coordinates/sizes: "
            + ", ".join(odd)
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the direct camera-wall compositor")
    parser.add_argument("--config", required=True, help="Path to config YAML")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _configure_logging()
    config = load_config(Path(args.config))
    _validate_compose_config(config)
    compositor = DirectCompositor(config)
    signal.signal(signal.SIGTERM, lambda signum, _frame: compositor.request_stop(signum))
    signal.signal(signal.SIGINT, lambda signum, _frame: compositor.request_stop(signum))
    return compositor.run()


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


def _escape_filter_value(value: str) -> str:
    return value.replace("\\", r"\\").replace(":", r"\:").replace(",", r"\,")


def _configure_logging() -> None:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


if __name__ == "__main__":
    raise SystemExit(main())

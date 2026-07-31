from __future__ import annotations

import argparse
from dataclasses import dataclass
import logging
import signal
import subprocess
import sys
import threading
import time

from .ffmpeg import mask_text, masked_command


@dataclass(frozen=True)
class SlotSettings:
    ffmpeg_binary: str
    log_level: str
    name: str
    input_url: str
    output_url: str
    width: int
    height: int
    fps: int
    preserve_aspect: bool
    pad_color: str
    offline_text: str
    input_rtsp_transport: str
    input_hwaccel: str
    hwaccel_device: str
    input_timeout_seconds: int
    http_reconnect_delay_max_seconds: int
    slot_transport: str
    worker_rtsp_transport: str
    restart_delay_seconds: int
    stall_timeout_seconds: int
    bitrate: str


@dataclass
class DecoderRuntime:
    process: subprocess.Popen[bytes]
    reader: threading.Thread
    stderr_reader: threading.Thread
    started_at: float


class LatestFrame:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: bytes | None = None
        self._updated_at = 0.0

    def update(self, frame: bytes) -> None:
        with self._lock:
            self._frame = frame
            self._updated_at = time.monotonic()

    def snapshot(self) -> tuple[bytes | None, float]:
        with self._lock:
            return self._frame, self._updated_at


class StableSlotWorker:
    def __init__(self, settings: SlotSettings) -> None:
        self.settings = settings
        self.stop_requested = threading.Event()
        self.latest_frame = LatestFrame()
        self.encoder: subprocess.Popen[bytes] | None = None
        self.encoder_stderr_reader: threading.Thread | None = None

    def run(self) -> int:
        offline_frame = self._offline_frame()
        frame_interval = 1 / max(1, self.settings.fps)
        next_frame_at = time.monotonic()
        next_decoder_start_at = 0.0
        decoder: DecoderRuntime | None = None
        frames_written = 0
        last_progress_at = 0.0
        last_source_state = ""

        try:
            self._start_encoder()
            while not self.stop_requested.is_set():
                now = time.monotonic()
                if now < next_frame_at:
                    self.stop_requested.wait(min(0.25, next_frame_at - now))
                    continue

                decoder, next_decoder_start_at = self._refresh_decoder(
                    decoder,
                    next_decoder_start_at,
                )
                if self._encoder_exited():
                    self._start_encoder()

                frame, frame_at = self.latest_frame.snapshot()
                source_state = self._source_state(frame, frame_at)
                output_frame = frame if source_state == "live" and frame else offline_frame
                if source_state != last_source_state:
                    logging.info("slot %s source is %s", self.settings.name, source_state)
                    print(f"camera_wall_source={source_state}", flush=True)
                    last_source_state = source_state

                if not self._write_frame(output_frame):
                    self._start_encoder()
                else:
                    frames_written += 1

                now = time.monotonic()
                if now - last_progress_at >= 1:
                    print(f"frame={frames_written}", flush=True)
                    print("progress=continue", flush=True)
                    last_progress_at = now

                next_frame_at += frame_interval
                if next_frame_at < now - frame_interval:
                    next_frame_at = now + frame_interval
            return 0
        finally:
            self._stop_decoder(decoder)
            self._stop_encoder()

    def request_stop(self, signum: int) -> None:
        logging.info("slot %s received signal %s", self.settings.name, signum)
        self.stop_requested.set()
        self._stop_encoder()

    def _refresh_decoder(
        self,
        decoder: DecoderRuntime | None,
        next_decoder_start_at: float,
    ) -> tuple[DecoderRuntime | None, float]:
        now = time.monotonic()
        if decoder is not None:
            exit_code = decoder.process.poll()
            if exit_code is not None:
                logging.warning(
                    "slot %s decoder exited with code %s",
                    self.settings.name,
                    exit_code,
                )
                self._stop_decoder(decoder)
                return None, now + self.settings.restart_delay_seconds
            if self._decoder_stalled(decoder, now):
                logging.warning(
                    "slot %s decoder produced no fresh frame for %s seconds; restarting decoder",
                    self.settings.name,
                    self.settings.stall_timeout_seconds,
                )
                self._stop_decoder(decoder)
                return None, now + self.settings.restart_delay_seconds
            return decoder, next_decoder_start_at

        if now < next_decoder_start_at:
            return None, next_decoder_start_at
        decoder = self._start_decoder()
        if decoder is None:
            return None, now + self.settings.restart_delay_seconds
        return decoder, next_decoder_start_at

    def _source_state(self, frame: bytes | None, frame_at: float) -> str:
        if frame is None:
            return "offline"
        timeout = max(0, self.settings.stall_timeout_seconds)
        if timeout <= 0 or time.monotonic() - frame_at <= timeout:
            return "live"
        return "offline"

    def _decoder_stalled(self, decoder: DecoderRuntime, now: float) -> bool:
        timeout = max(0, self.settings.stall_timeout_seconds)
        if timeout <= 0:
            return False
        _, frame_at = self.latest_frame.snapshot()
        reference = frame_at if frame_at > decoder.started_at else decoder.started_at
        return now - reference > timeout

    def _start_decoder(self) -> DecoderRuntime | None:
        command = build_decoder_command(self.settings)
        logging.info("slot %s decoder command: %s", self.settings.name, masked_command(command))
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                bufsize=0,
            )
        except FileNotFoundError:
            logging.error("slot %s FFmpeg binary not found: %s", self.settings.name, command[0])
            return None

        reader = threading.Thread(
            target=_read_decoder_frames,
            args=(self.settings.name, process, self.latest_frame, frame_size(self.settings)),
            name=f"slot-{self.settings.name}-decoder",
            daemon=True,
        )
        stderr_reader = threading.Thread(
            target=_drain_stream,
            args=(f"slot {self.settings.name} decoder", process.stderr, self.stop_requested),
            name=f"slot-{self.settings.name}-decoder-log",
            daemon=True,
        )
        reader.start()
        stderr_reader.start()
        logging.info("slot %s decoder started with pid %s", self.settings.name, process.pid)
        return DecoderRuntime(
            process=process,
            reader=reader,
            stderr_reader=stderr_reader,
            started_at=time.monotonic(),
        )

    def _stop_decoder(self, decoder: DecoderRuntime | None) -> None:
        if decoder is None:
            return
        _terminate_process(decoder.process)
        decoder.reader.join(timeout=2)
        decoder.stderr_reader.join(timeout=1)

    def _start_encoder(self) -> None:
        self._stop_encoder()
        command = build_encoder_command(self.settings)
        logging.info("slot %s encoder command: %s", self.settings.name, masked_command(command))
        try:
            self.encoder = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError:
            logging.error("slot %s FFmpeg binary not found: %s", self.settings.name, command[0])
            self.encoder = None
            self.stop_requested.wait(self.settings.restart_delay_seconds)
            return
        self.encoder_stderr_reader = threading.Thread(
            target=_drain_stream,
            args=(f"slot {self.settings.name} encoder", self.encoder.stderr, self.stop_requested),
            name=f"slot-{self.settings.name}-encoder-log",
            daemon=True,
        )
        self.encoder_stderr_reader.start()
        logging.info("slot %s encoder started with pid %s", self.settings.name, self.encoder.pid)

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
            logging.warning("slot %s encoder pipe failed: %s", self.settings.name, exc)
            return False

    def _offline_frame(self) -> bytes:
        command = build_offline_frame_command(self.settings)
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
                "slot %s could not render offline label; using black frame: %s",
                self.settings.name,
                exc,
            )
            return black_yuv420p_frame(self.settings.width, self.settings.height)
        expected = frame_size(self.settings)
        if len(result.stdout) != expected:
            logging.warning(
                "slot %s offline frame size was %s bytes, expected %s; using black frame",
                self.settings.name,
                len(result.stdout),
                expected,
            )
            return black_yuv420p_frame(self.settings.width, self.settings.height)
        return result.stdout


def build_decoder_command(settings: SlotSettings) -> list[str]:
    args = [
        settings.ffmpeg_binary,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        settings.log_level,
        "-thread_queue_size",
        "512",
        "-fflags",
        "+genpts",
    ]
    if settings.input_hwaccel == "vaapi":
        args.extend(
            [
                "-hwaccel",
                "vaapi",
                "-hwaccel_device",
                settings.hwaccel_device,
                "-hwaccel_output_format",
                "vaapi",
            ]
        )
    if _is_rtsp_url(settings.input_url):
        args.extend(["-rtsp_transport", settings.input_rtsp_transport])
    if _is_http_url(settings.input_url):
        args.extend(
            [
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                str(settings.http_reconnect_delay_max_seconds),
            ]
        )
    if settings.input_timeout_seconds > 0:
        args.extend(["-rw_timeout", str(settings.input_timeout_seconds * 1_000_000)])
    args.extend(
        [
            "-i",
            settings.input_url,
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            _live_video_filter(settings),
            "-pix_fmt",
            "yuv420p",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
    )
    return args


def build_encoder_command(settings: SlotSettings) -> list[str]:
    args = [
        settings.ffmpeg_binary,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        settings.log_level,
        "-f",
        "rawvideo",
        "-pix_fmt",
        "yuv420p",
        "-s",
        f"{settings.width}x{settings.height}",
        "-r",
        str(settings.fps),
        "-i",
        "pipe:0",
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
        settings.bitrate,
        "-maxrate",
        settings.bitrate,
        "-bufsize",
        _double_bitrate(settings.bitrate),
        "-g",
        str(settings.fps * 2),
        "-keyint_min",
        str(settings.fps),
        "-sc_threshold",
        "0",
        "-x264-params",
        "repeat-headers=1:scenecut=0",
        "-r",
        str(settings.fps),
    ]
    args.extend(_worker_output_args(settings))
    return args


def build_offline_frame_command(settings: SlotSettings) -> list[str]:
    source = (
        f"color=c=black:s={settings.width}x{settings.height}:r={settings.fps},"
        "drawtext="
        f"text='{_escape_drawtext(settings.offline_text)}':"
        "x=(w-text_w)/2:y=(h-text_h)/2:"
        f"fontcolor=white@0.76:fontsize={_offline_font_size(settings.height)}:"
        "box=1:boxcolor=black@0.62:boxborderw=10"
    )
    return [
        settings.ffmpeg_binary,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        source,
        "-frames:v",
        "1",
        "-pix_fmt",
        "yuv420p",
        "-f",
        "rawvideo",
        "pipe:1",
    ]


def frame_size(settings: SlotSettings) -> int:
    return settings.width * settings.height * 3 // 2


def black_yuv420p_frame(width: int, height: int) -> bytes:
    y_size = width * height
    uv_size = y_size // 2
    return b"\x10" * y_size + b"\x80" * uv_size


def parse_args(argv: list[str] | None = None) -> SlotSettings:
    parser = argparse.ArgumentParser(description="Run one stable camera-wall slot worker")
    parser.add_argument("--ffmpeg-binary", default="ffmpeg")
    parser.add_argument("--log-level", default="warning")
    parser.add_argument("--name", required=True)
    parser.add_argument("--input-url", required=True)
    parser.add_argument("--output-url", required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--fps", type=int, required=True)
    parser.add_argument("--preserve-aspect", default="true")
    parser.add_argument("--pad-color", default="black")
    parser.add_argument("--offline-text", required=True)
    parser.add_argument("--input-rtsp-transport", default="tcp")
    parser.add_argument("--input-hwaccel", default="software")
    parser.add_argument("--hwaccel-device", default="/dev/dri/renderD128")
    parser.add_argument("--input-timeout-seconds", type=int, default=0)
    parser.add_argument("--http-reconnect-delay-max-seconds", type=int, default=5)
    parser.add_argument("--slot-transport", default="udp_mpegts")
    parser.add_argument("--worker-rtsp-transport", default="tcp")
    parser.add_argument("--restart-delay-seconds", type=int, default=5)
    parser.add_argument("--stall-timeout-seconds", type=int, default=3)
    parser.add_argument("--bitrate", default="1200k")
    args = parser.parse_args(argv)
    return SlotSettings(
        ffmpeg_binary=args.ffmpeg_binary,
        log_level=args.log_level,
        name=args.name,
        input_url=args.input_url,
        output_url=args.output_url,
        width=args.width,
        height=args.height,
        fps=args.fps,
        preserve_aspect=_bool_arg(args.preserve_aspect),
        pad_color=args.pad_color,
        offline_text=args.offline_text,
        input_rtsp_transport=args.input_rtsp_transport,
        input_hwaccel=args.input_hwaccel,
        hwaccel_device=args.hwaccel_device,
        input_timeout_seconds=max(0, args.input_timeout_seconds),
        http_reconnect_delay_max_seconds=max(1, args.http_reconnect_delay_max_seconds),
        slot_transport=args.slot_transport,
        worker_rtsp_transport=args.worker_rtsp_transport,
        restart_delay_seconds=max(1, args.restart_delay_seconds),
        stall_timeout_seconds=max(0, args.stall_timeout_seconds),
        bitrate=args.bitrate,
    )


def main(argv: list[str] | None = None) -> int:
    settings = parse_args(argv)
    _configure_logging(settings.log_level)
    worker = StableSlotWorker(settings)
    signal.signal(signal.SIGTERM, lambda signum, _frame: worker.request_stop(signum))
    signal.signal(signal.SIGINT, lambda signum, _frame: worker.request_stop(signum))
    return worker.run()


def _read_decoder_frames(
    name: str,
    process: subprocess.Popen[bytes],
    latest_frame: LatestFrame,
    size: int,
) -> None:
    if not process.stdout:
        return
    while True:
        frame = _read_exact(process.stdout, size)
        if frame is None:
            return
        latest_frame.update(frame)


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


def _live_video_filter(settings: SlotSettings) -> str:
    prefix = "hwdownload,format=nv12," if settings.input_hwaccel == "vaapi" else ""
    if settings.preserve_aspect:
        return (
            f"{prefix}fps={settings.fps},"
            f"scale=w={settings.width}:h={settings.height}:force_original_aspect_ratio=decrease,"
            f"pad=w={settings.width}:h={settings.height}:x=(ow-iw)/2:y=(oh-ih)/2:"
            f"color={_escape_filter_value(settings.pad_color)},"
            "setsar=1,format=yuv420p"
        )
    return (
        f"{prefix}fps={settings.fps},"
        f"scale=w={settings.width}:h={settings.height},"
        "setsar=1,format=yuv420p"
    )


def _worker_output_args(settings: SlotSettings) -> list[str]:
    if settings.slot_transport == "udp_mpegts":
        return ["-f", "mpegts", "-muxdelay", "0", settings.output_url]
    return [
        "-f",
        "rtsp",
        "-rtsp_transport",
        settings.worker_rtsp_transport,
        "-muxdelay",
        "0.1",
        settings.output_url,
    ]


def _double_bitrate(value: str) -> str:
    number = ""
    suffix = ""
    for char in value:
        if char.isdigit():
            number += char
        else:
            suffix += char
    if not number or suffix.lower() not in {"", "k", "m"}:
        return value
    return f"{int(number) * 2}{suffix}"


def _offline_font_size(height: int) -> int:
    return max(18, min(42, height // 18))


def _bool_arg(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


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


def _configure_logging(log_level: str) -> None:
    logging.basicConfig(
        level=log_level.upper(),
        format="slot_worker %(levelname)s %(message)s",
        stream=sys.stdout,
    )


if __name__ == "__main__":
    raise SystemExit(main())

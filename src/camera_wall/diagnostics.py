from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from typing import Any
from urllib.parse import urlparse

from .ffmpeg import mask_text, mask_url
from .gpu import DEFAULT_GPU_DEVICE, read_gpu_stats


DEFAULT_TIMEOUT_SECONDS = 8


@dataclass(frozen=True)
class StreamProbeRequest:
    url: str
    name: str = ""
    rtsp_transport: str = "tcp"
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS


def diagnose_stream(request: StreamProbeRequest) -> dict[str, Any]:
    url = request.url.strip()
    if not url:
        return _result(False, "stream", "Stream URL is empty", url=url, name=request.name)

    command = [
        os.environ.get("CAMERA_WALL_FFPROBE", "ffprobe"),
        "-hide_banner",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
    ]
    if _is_rtsp_url(url):
        command.extend(["-rtsp_transport", request.rtsp_transport or "tcp"])
    command.append(url)

    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, request.timeout_seconds),
            check=False,
        )
    except FileNotFoundError:
        return _result(False, "stream", "ffprobe binary is not installed", url=url, name=request.name)
    except subprocess.TimeoutExpired:
        return _result(
            False,
            "stream",
            f"ffprobe timed out after {request.timeout_seconds} seconds",
            url=url,
            name=request.name,
            duration_ms=_duration_ms(started),
            error_kind="timeout",
        )

    stderr = mask_text(completed.stderr.strip())
    if completed.returncode != 0:
        return _result(
            False,
            "stream",
            _classify_probe_error(stderr),
            url=url,
            name=request.name,
            duration_ms=_duration_ms(started),
            error=stderr or f"ffprobe exited with code {completed.returncode}",
            error_kind=_error_kind(stderr),
        )

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        return _result(
            False,
            "stream",
            f"Could not parse ffprobe JSON: {exc.msg}",
            url=url,
            name=request.name,
            duration_ms=_duration_ms(started),
            error_kind="parse",
        )

    video = _first_stream(payload, "video")
    audio = _first_stream(payload, "audio")
    if not video:
        return _result(
            False,
            "stream",
            "ffprobe connected, but no video stream was found",
            url=url,
            name=request.name,
            duration_ms=_duration_ms(started),
            audio_present=audio is not None,
            error_kind="no_video",
        )

    return _result(
        True,
        "stream",
        "Stream probe succeeded",
        url=url,
        name=request.name,
        duration_ms=_duration_ms(started),
        video={
            "codec": video.get("codec_name"),
            "profile": video.get("profile"),
            "width": video.get("width"),
            "height": video.get("height"),
            "pix_fmt": video.get("pix_fmt"),
            "fps": _fps(video),
            "time_base": video.get("time_base"),
        },
        audio_present=audio is not None,
        audio_codec=audio.get("codec_name") if audio else None,
    )


def diagnose_output(url: str, timeout_seconds: int = 5) -> dict[str, Any]:
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.hostname:
        return _result(False, "output", "Output URL must include a scheme and host", url=url)

    port = parsed.port or _default_port(parsed.scheme)
    if port is None:
        return _result(False, "output", f"No default port for {parsed.scheme}", url=url)

    started = time.monotonic()
    try:
        with socket.create_connection((parsed.hostname, port), timeout=max(1, timeout_seconds)):
            pass
    except OSError as exc:
        return _result(
            False,
            "output",
            f"Could not connect to {parsed.hostname}:{port}",
            url=url,
            host=parsed.hostname,
            port=port,
            duration_ms=_duration_ms(started),
            error=str(exc),
            error_kind="connect",
        )

    return _result(
        True,
        "output",
        f"Connected to {parsed.hostname}:{port}",
        url=url,
        host=parsed.hostname,
        port=port,
        duration_ms=_duration_ms(started),
    )


def diagnose_gpu(device: str = DEFAULT_GPU_DEVICE, sample_ms: int = 1000) -> dict[str, Any]:
    intel_gpu_top = os.environ.get("CAMERA_WALL_INTEL_GPU_TOP") or shutil.which("intel_gpu_top")
    dri_path = Path("/dev/dri")
    render_path = Path(device)
    stats = read_gpu_stats(device, sample_ms=max(100, sample_ms))
    checks = [
        {
            "name": "/dev/dri mounted",
            "ok": dri_path.exists(),
            "detail": str(dri_path),
        },
        {
            "name": "GPU device exists",
            "ok": render_path.exists(),
            "detail": device,
        },
        {
            "name": "intel_gpu_top installed",
            "ok": bool(intel_gpu_top),
            "detail": intel_gpu_top or "not found",
        },
        {
            "name": "GPU load readable",
            "ok": bool(stats.get("available")),
            "detail": stats.get("error") or stats.get("source") or "-",
        },
    ]
    return {
        "ok": bool(stats.get("available")),
        "type": "gpu",
        "message": "GPU metrics are available" if stats.get("available") else "GPU metrics are unavailable",
        "device": device,
        "checks": checks,
        "stats": stats,
    }


def stream_request_from_payload(payload: Any) -> StreamProbeRequest:
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object")
    return StreamProbeRequest(
        url=_string(payload.get("url", ""), "url"),
        name=_string(payload.get("name", ""), "name"),
        rtsp_transport=_string(payload.get("rtsp_transport", "tcp"), "rtsp_transport"),
        timeout_seconds=_positive_int(payload.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS), "timeout_seconds"),
    )


def output_request_from_payload(payload: Any) -> tuple[str, int]:
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object")
    return (
        _string(payload.get("url", ""), "url"),
        _positive_int(payload.get("timeout_seconds", 5), "timeout_seconds"),
    )


def gpu_request_from_payload(payload: Any) -> tuple[str, int]:
    if not isinstance(payload, dict):
        payload = {}
    return (
        _string(payload.get("device", DEFAULT_GPU_DEVICE), "device"),
        _positive_int(payload.get("sample_ms", 1000), "sample_ms"),
    )


def _result(ok: bool, kind: str, message: str, **values: Any) -> dict[str, Any]:
    payload = {
        "ok": ok,
        "type": kind,
        "message": message,
    }
    payload.update(values)
    if "url" in payload:
        payload["url"] = mask_url(str(payload["url"]))
    return payload


def _first_stream(payload: dict[str, Any], codec_type: str) -> dict[str, Any] | None:
    streams = payload.get("streams", [])
    if not isinstance(streams, list):
        return None
    for stream in streams:
        if isinstance(stream, dict) and stream.get("codec_type") == codec_type:
            return stream
    return None


def _fps(stream: dict[str, Any]) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        fps = _fraction(stream.get(key))
        if fps:
            return round(fps, 2)
    return None


def _fraction(value: Any) -> float | None:
    if not isinstance(value, str) or value in {"0/0", "N/A"}:
        return None
    left, sep, right = value.partition("/")
    try:
        numerator = float(left)
        denominator = float(right) if sep else 1.0
    except ValueError:
        return None
    if denominator == 0:
        return None
    return numerator / denominator


def _classify_probe_error(stderr: str) -> str:
    kind = _error_kind(stderr)
    if kind == "auth":
        return "Authentication failed or credentials were rejected"
    if kind == "timeout":
        return "Connection timed out"
    if kind == "connect":
        return "Could not connect to the stream"
    if kind == "invalid":
        return "The endpoint responded, but ffprobe could not read video"
    return "Stream probe failed"


def _error_kind(stderr: str) -> str:
    lowered = stderr.lower()
    if any(term in lowered for term in ("401", "unauthorized", "forbidden", "403")):
        return "auth"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if any(term in lowered for term in ("connection refused", "could not connect", "no route", "network is unreachable")):
        return "connect"
    if any(term in lowered for term in ("invalid data", "server returned", "not found", "404")):
        return "invalid"
    return "ffprobe"


def _default_port(scheme: str) -> int | None:
    return {
        "rtsp": 554,
        "rtsps": 322,
        "http": 80,
        "https": 443,
    }.get(scheme.lower())


def _is_rtsp_url(value: str) -> bool:
    return value.lower().startswith(("rtsp://", "rtsps://"))


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed

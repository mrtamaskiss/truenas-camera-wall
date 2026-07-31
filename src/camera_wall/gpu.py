from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time
from typing import Any


DEFAULT_GPU_DEVICE = "/dev/dri/renderD128"


class GpuMonitor:
    def __init__(
        self,
        *,
        enabled: bool = True,
        device: str = DEFAULT_GPU_DEVICE,
        interval_seconds: int = 5,
        sample_ms: int = 1000,
    ) -> None:
        self.enabled = enabled
        self.device = device
        self.interval_seconds = max(1, interval_seconds)
        self.sample_ms = max(100, sample_ms)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot = _disabled_snapshot(device) if not enabled else _pending_snapshot(device)

    @classmethod
    def from_env(cls) -> GpuMonitor:
        return cls(
            enabled=_env_bool("CAMERA_WALL_GPU_STATS_ENABLED", True),
            device=os.environ.get("CAMERA_WALL_GPU_DEVICE", DEFAULT_GPU_DEVICE),
            interval_seconds=_env_int("CAMERA_WALL_GPU_SAMPLE_SECONDS", 5),
            sample_ms=_env_int("CAMERA_WALL_GPU_SAMPLE_MS", 1000),
        )

    def start(self) -> None:
        if not self.enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="gpu-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._snapshot)

    def _run(self) -> None:
        while not self._stop.is_set():
            snapshot = read_gpu_stats(self.device, self.sample_ms)
            with self._lock:
                self._snapshot = snapshot
            self._stop.wait(self.interval_seconds)


def read_gpu_stats(device: str = DEFAULT_GPU_DEVICE, sample_ms: int = 1000) -> dict[str, Any]:
    binary = os.environ.get("CAMERA_WALL_INTEL_GPU_TOP") or shutil.which("intel_gpu_top")
    if binary:
        stats = _read_intel_gpu_top(binary, device, sample_ms)
        if stats.get("available"):
            return stats
        fallback = _read_sysfs(device, stats.get("error"))
        if fallback.get("device_present") or fallback.get("load_percent") is not None:
            return fallback
        return stats
    return _read_sysfs(device, "intel_gpu_top is not installed")


def parse_intel_gpu_top_json(text: str, device: str = DEFAULT_GPU_DEVICE) -> dict[str, Any]:
    samples = _load_samples(text)
    if not samples:
        raise ValueError("intel_gpu_top returned no samples")
    sample = samples[-1]
    engines = sample.get("engines", {})
    if not isinstance(engines, dict):
        engines = {}

    engine_busy = {
        name: _metric_number(metrics.get("busy"))
        for name, metrics in engines.items()
        if isinstance(metrics, dict)
    }
    load = min(100.0, sum(value for value in engine_busy.values() if value is not None))
    render = _sum_engines(engine_busy, ("render", "render/3d"))
    video = _sum_engines(engine_busy, ("video",))
    blitter = _sum_engines(engine_busy, ("blitter", "copy"))

    frequency = sample.get("frequency", {})
    rc6 = sample.get("rc6", {})
    return {
        "enabled": True,
        "available": True,
        "source": "intel_gpu_top",
        "device": device,
        "device_present": Path(device).exists(),
        "load_percent": _round(load),
        "render_percent": _round(render),
        "video_percent": _round(video),
        "blitter_percent": _round(blitter),
        "frequency_mhz": _round(_metric_number(_mapping_value(frequency, "actual"))),
        "rc6_percent": _round(_metric_number(_mapping_value(rc6, "value"))),
        "updated_at": _utc_now(),
        "error": None,
    }


def _read_intel_gpu_top(binary: str, device: str, sample_ms: int) -> dict[str, Any]:
    command = [binary, "-J", "-s", str(sample_ms), "-o", "-"]
    if device:
        command.extend(["-d", f"drm:{device}"])
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return _unavailable_snapshot(device, f"Could not start intel_gpu_top: {exc}")

    try:
        time.sleep((sample_ms / 1000) + 0.25)
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=2)
    except OSError as exc:
        process.kill()
        stdout, stderr = process.communicate(timeout=2)
        return _unavailable_snapshot(device, f"intel_gpu_top failed: {exc}")

    if not stdout.strip():
        message = stderr.strip() or f"intel_gpu_top exited with code {process.returncode}"
        return _unavailable_snapshot(device, message)
    try:
        return parse_intel_gpu_top_json(stdout, device)
    except ValueError as exc:
        message = stderr.strip() or str(exc)
        return _unavailable_snapshot(device, message)


def _read_sysfs(device: str, error: object | None = None) -> dict[str, Any]:
    device_path = Path(device)
    drm_device = _sysfs_device_path(device_path)
    load = _read_first_number(
        (
            drm_device / "gpu_busy_percent",
            *_glob_numbers("/sys/class/drm/card*/device/gpu_busy_percent"),
            *_glob_numbers("/sys/class/drm/card*/gt/gt*/busy_percent"),
        )
    )
    frequency = _read_first_number(
        (
            drm_device / "gt_cur_freq_mhz",
            drm_device / "gt/gt0/rps_cur_freq_mhz",
            *_glob_numbers("/sys/class/drm/card*/gt_cur_freq_mhz"),
            *_glob_numbers("/sys/class/drm/card*/gt/gt*/rps_cur_freq_mhz"),
        )
    )
    return {
        "enabled": True,
        "available": load is not None,
        "source": "sysfs",
        "device": device,
        "device_present": device_path.exists(),
        "load_percent": _round(load),
        "render_percent": None,
        "video_percent": None,
        "blitter_percent": None,
        "frequency_mhz": _round(frequency),
        "rc6_percent": None,
        "updated_at": _utc_now(),
        "error": None if load is not None else str(error or "GPU load is unavailable"),
    }


def _load_samples(text: str) -> list[dict[str, Any]]:
    stripped = text.strip()
    if not stripped:
        return []
    candidates = [stripped]
    if stripped.startswith("[") and not stripped.endswith("]"):
        candidates.append(f"{stripped.rstrip(',')}]")
    if not stripped.startswith("["):
        candidates.append(f"[{stripped.rstrip(',')}]")

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    raise ValueError("Could not parse intel_gpu_top JSON")


def _mapping_value(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else None


def _metric_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _sum_engines(values: dict[str, float | None], prefixes: tuple[str, ...]) -> float | None:
    total = 0.0
    found = False
    for name, value in values.items():
        lowered = name.lower()
        if value is not None and any(lowered.startswith(prefix) for prefix in prefixes):
            total += value
            found = True
    return min(100.0, total) if found else None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _sysfs_device_path(device_path: Path) -> Path:
    if device_path.name:
        return Path("/sys/class/drm") / device_path.name / "device"
    return Path("/sys/class/drm/renderD128/device")


def _glob_numbers(pattern: str) -> tuple[Path, ...]:
    return tuple(Path("/").glob(pattern.lstrip("/")))


def _read_first_number(paths: tuple[Path, ...]) -> float | None:
    for path in paths:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        number = _metric_number(value)
        if number is not None:
            return number
    return None


def _pending_snapshot(device: str) -> dict[str, Any]:
    return {
        "enabled": True,
        "available": False,
        "source": "pending",
        "device": device,
        "device_present": Path(device).exists(),
        "load_percent": None,
        "render_percent": None,
        "video_percent": None,
        "blitter_percent": None,
        "frequency_mhz": None,
        "rc6_percent": None,
        "updated_at": None,
        "error": "GPU metrics have not been sampled yet",
    }


def _disabled_snapshot(device: str) -> dict[str, Any]:
    snapshot = _pending_snapshot(device)
    snapshot.update({"enabled": False, "source": "disabled", "error": "GPU metrics are disabled"})
    return snapshot


def _unavailable_snapshot(device: str, error: str) -> dict[str, Any]:
    snapshot = _pending_snapshot(device)
    snapshot.update(
        {
            "source": "intel_gpu_top",
            "device_present": Path(device).exists(),
            "updated_at": _utc_now(),
            "error": error,
        }
    )
    return snapshot


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

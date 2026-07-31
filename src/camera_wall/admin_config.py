from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any, Mapping

try:
    import yaml  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised on minimal systems
    yaml = None

from .config import AppConfig, ConfigError, parse_config


DEFAULT_ADMIN_CONFIG: dict[str, Any] = {
    "output": {
        "url": "rtsp://192.168.64.10:8554/camera_wall",
        "width": 1920,
        "height": 1080,
        "fps": 15,
        "bitrate": "5M",
        "encoder": "software",
        "rtsp_transport": "tcp",
        "vaapi_device": "/dev/dri/renderD128",
        "vaapi_rc_mode": "cqp",
        "vaapi_qp": 23,
        "qsv_device": "/dev/dri/renderD128",
    },
    "ffmpeg": {
        "log_level": "warning",
        "input_rtsp_transport": "tcp",
        "input_hwaccel": "software",
        "hwaccel_device": "/dev/dri/renderD128",
        "input_timeout_seconds": 0,
        "http_reconnect_delay_max_seconds": 5,
        "restart_delay_seconds": 5,
    },
    "workers": {
        "enabled": False,
        "mode": "remux",
        "slot_transport": "rtsp",
        "output_template": "",
        "wall_input_template": "",
        "udp_base_port": 15000,
        "rtsp_transport": "tcp",
        "fallback_enabled": True,
        "restart_delay_seconds": 5,
        "start_grace_seconds": 2,
        "retry_live_seconds": 15,
        "retry_probe_timeout_seconds": 3,
        "stall_timeout_seconds": 3,
        "wall_input_preflight": False,
    },
    "inputs": [],
}


def load_admin_config(
    path: str | Path, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    config_path = Path(path)
    raw: Any = {}
    if not config_path.exists():
        return {
            "config": deepcopy(DEFAULT_ADMIN_CONFIG),
            "valid": False,
            "error": f"Config file not found: {config_path}",
        }

    try:
        raw = _load_yaml(config_path)
        normalized = normalize_admin_config(raw)
        parsed = parse_config(normalized, env or os.environ)
    except ConfigError as exc:
        return {
            "config": _safe_normalize(raw),
            "valid": False,
            "error": str(exc),
        }

    return {"config": app_config_to_dict(parsed), "valid": True, "error": None}


def save_admin_config(
    path: str | Path, data: Mapping[str, Any], env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    normalized = normalize_admin_config(data)
    parsed = parse_config(normalized, env or os.environ)
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(config_path, dump_yaml(app_config_to_dict(parsed)))
    return app_config_to_dict(parsed)


def normalize_admin_config(raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ConfigError("Config root must be a mapping")

    normalized = deepcopy(DEFAULT_ADMIN_CONFIG)
    output = raw.get("output", {})
    ffmpeg = raw.get("ffmpeg", {})
    workers = raw.get("workers", {})
    inputs = raw.get("inputs", [])

    if not isinstance(output, Mapping):
        raise ConfigError("output must be a mapping")
    if not isinstance(ffmpeg, Mapping):
        raise ConfigError("ffmpeg must be a mapping")
    if not isinstance(workers, Mapping):
        raise ConfigError("workers must be a mapping")
    if not isinstance(inputs, list):
        raise ConfigError("inputs must be a list")

    for key in normalized["output"]:
        if key in output:
            normalized["output"][key] = output[key]
    for key in normalized["ffmpeg"]:
        if key in ffmpeg:
            normalized["ffmpeg"][key] = ffmpeg[key]
    for key in normalized["workers"]:
        if key in workers:
            normalized["workers"][key] = workers[key]

    normalized["inputs"] = [_normalize_input(item, index) for index, item in enumerate(inputs)]
    return normalized


def _safe_normalize(raw: Any) -> dict[str, Any]:
    try:
        return normalize_admin_config(raw)
    except ConfigError:
        return deepcopy(DEFAULT_ADMIN_CONFIG)


def app_config_to_dict(config: AppConfig) -> dict[str, Any]:
    return {
        "output": {
            "url": config.output.url,
            "width": config.output.width,
            "height": config.output.height,
            "fps": config.output.fps,
            "bitrate": config.output.bitrate,
            "encoder": config.output.encoder,
            "rtsp_transport": config.output.rtsp_transport,
            "vaapi_device": config.output.vaapi_device,
            "vaapi_rc_mode": config.output.vaapi_rc_mode,
            "vaapi_qp": config.output.vaapi_qp,
            "qsv_device": config.output.qsv_device,
        },
        "ffmpeg": {
            "log_level": config.ffmpeg.log_level,
            "input_rtsp_transport": config.ffmpeg.input_rtsp_transport,
            "input_hwaccel": config.ffmpeg.input_hwaccel,
            "hwaccel_device": config.ffmpeg.hwaccel_device,
            "input_timeout_seconds": config.ffmpeg.input_timeout_seconds,
            "http_reconnect_delay_max_seconds": config.ffmpeg.http_reconnect_delay_max_seconds,
            "restart_delay_seconds": config.ffmpeg.restart_delay_seconds,
        },
        "workers": {
            "enabled": config.workers.enabled,
            "mode": config.workers.mode,
            "slot_transport": config.workers.slot_transport,
            "output_template": config.workers.output_template,
            "wall_input_template": config.workers.wall_input_template,
            "udp_base_port": config.workers.udp_base_port,
            "rtsp_transport": config.workers.rtsp_transport,
            "fallback_enabled": config.workers.fallback_enabled,
            "restart_delay_seconds": config.workers.restart_delay_seconds,
            "start_grace_seconds": config.workers.start_grace_seconds,
            "retry_live_seconds": config.workers.retry_live_seconds,
            "retry_probe_timeout_seconds": config.workers.retry_probe_timeout_seconds,
            "stall_timeout_seconds": config.workers.stall_timeout_seconds,
            "wall_input_preflight": config.workers.wall_input_preflight,
        },
        "inputs": [
            {
                "name": item.name,
                "enabled": item.enabled,
                "url": item.url,
                "label": item.label or item.name,
                "show_label": item.show_label,
                "x": item.x,
                "y": item.y,
                "width": item.width,
                "height": item.height,
                "preserve_aspect": item.preserve_aspect,
                "pad_color": item.pad_color,
            }
            for item in config.inputs
        ],
    }


def dump_yaml(data: Mapping[str, Any]) -> str:
    if yaml is not None:
        return yaml.safe_dump(
            dict(data),
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=False,
        )
    return _dump_node(data, 0)


def _normalize_input(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ConfigError(f"inputs[{index}] must be a mapping")
    position = item.get("position", {})
    if position is None:
        position = {}
    if not isinstance(position, Mapping):
        raise ConfigError(f"inputs[{index}].position must be a mapping")
    dimensions = {**item, **position}
    name = item.get("name", f"camera-{index + 1}")
    label = item.get("label", name)
    return {
        "name": name,
        "enabled": item.get("enabled", True),
        "url": item.get("url", ""),
        "label": label,
        "show_label": item.get("show_label", True),
        "x": dimensions.get("x", 0),
        "y": dimensions.get("y", 0),
        "width": dimensions.get("width", 960),
        "height": dimensions.get("height", 540),
        "preserve_aspect": item.get("preserve_aspect", True),
        "pad_color": item.get("pad_color", "black"),
    }


def _load_yaml(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        return yaml.safe_load(text)
    from .simple_yaml import parse_simple_yaml

    return parse_simple_yaml(text)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _dump_node(value: Any, indent: int) -> str:
    lines: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, (Mapping, list)):
                lines.append(f"{' ' * indent}{key}:")
                lines.append(_dump_node(item, indent + 2).rstrip("\n"))
            else:
                lines.append(f"{' ' * indent}{key}: {_format_scalar(item)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping):
                lines.append(f"{' ' * indent}-")
                lines.append(_dump_node(item, indent + 2).rstrip("\n"))
            else:
                lines.append(f"{' ' * indent}- {_format_scalar(item)}")
    else:
        lines.append(f"{' ' * indent}{_format_scalar(value)}")
    return "\n".join(lines) + "\n"


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if value is None:
        return "null"
    return json.dumps(str(value))

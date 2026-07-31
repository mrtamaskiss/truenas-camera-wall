from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, Mapping

try:
    import yaml  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised implicitly in local tests
    yaml = None


class ConfigError(ValueError):
    """Raised when the camera wall configuration is invalid."""


@dataclass(frozen=True)
class InputConfig:
    name: str
    url: str
    x: int
    y: int
    width: int
    height: int
    enabled: bool = True
    label: str | None = None
    show_label: bool = True
    preserve_aspect: bool = True
    pad_color: str = "black"


@dataclass(frozen=True)
class OutputConfig:
    url: str
    width: int = 1920
    height: int = 1080
    fps: int = 15
    bitrate: str = "5M"
    encoder: str = "software"
    rtsp_transport: str = "tcp"
    vaapi_device: str = "/dev/dri/renderD128"
    vaapi_rc_mode: str = "cqp"
    vaapi_qp: int = 23
    qsv_device: str = "/dev/dri/renderD128"


@dataclass(frozen=True)
class FfmpegConfig:
    binary: str = "ffmpeg"
    log_level: str = "info"
    input_rtsp_transport: str = "tcp"
    input_hwaccel: str = "software"
    hwaccel_device: str = "/dev/dri/renderD128"
    input_timeout_seconds: int = 0
    http_reconnect_delay_max_seconds: int = 5
    restart_delay_seconds: int = 5


@dataclass(frozen=True)
class WorkerConfig:
    enabled: bool = False
    mode: str = "remux"
    output_template: str = ""
    rtsp_transport: str = "tcp"
    restart_delay_seconds: int = 5
    start_grace_seconds: int = 2
    wall_input_preflight: bool = False


@dataclass(frozen=True)
class AppConfig:
    output: OutputConfig
    inputs: tuple[InputConfig, ...]
    ffmpeg: FfmpegConfig = FfmpegConfig()
    workers: WorkerConfig = WorkerConfig()

    @property
    def enabled_inputs(self) -> tuple[InputConfig, ...]:
        return tuple(input_cfg for input_cfg in self.inputs if input_cfg.enabled)


_EXPANSION_RE = re.compile(r"\$\{([^}]+)\}")
_ENV_DEFAULT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):-([^}]*)$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    raw = _load_yaml(config_path)
    if not isinstance(raw, Mapping):
        raise ConfigError("Config root must be a mapping")
    return parse_config(raw, env or os.environ)


def parse_config(raw: Mapping[str, Any], env: Mapping[str, str] | None = None) -> AppConfig:
    env = env or os.environ
    output_raw = _mapping(raw.get("output"), "output")
    inputs_raw = raw.get("inputs")
    if not isinstance(inputs_raw, list):
        raise ConfigError("inputs must be a list")

    output = OutputConfig(
        url=_required_resolved_string(output_raw, "url", env),
        width=_positive_int(output_raw.get("width", 1920), "output.width"),
        height=_positive_int(output_raw.get("height", 1080), "output.height"),
        fps=_positive_int(output_raw.get("fps", 15), "output.fps"),
        bitrate=_resolved_string(output_raw.get("bitrate", "5M"), "output.bitrate", env),
        encoder=_encoder(_resolved_string(output_raw.get("encoder", "software"), "output.encoder", env)),
        rtsp_transport=_transport(
            _resolved_string(output_raw.get("rtsp_transport", "tcp"), "output.rtsp_transport", env),
            "output.rtsp_transport",
        ),
        vaapi_device=_resolved_string(
            output_raw.get("vaapi_device", "/dev/dri/renderD128"), "output.vaapi_device", env
        ),
        vaapi_rc_mode=_vaapi_rc_mode(
            _resolved_string(output_raw.get("vaapi_rc_mode", "cqp"), "output.vaapi_rc_mode", env)
        ),
        vaapi_qp=_qp_int(
            _resolved_maybe_int(output_raw.get("vaapi_qp", 23), "output.vaapi_qp", env),
            "output.vaapi_qp",
        ),
        qsv_device=_resolved_string(
            output_raw.get("qsv_device", "/dev/dri/renderD128"), "output.qsv_device", env
        ),
    )

    ffmpeg_raw = _optional_mapping(raw.get("ffmpeg"), "ffmpeg")
    ffmpeg = FfmpegConfig(
        binary=_string(ffmpeg_raw.get("binary", "ffmpeg"), "ffmpeg.binary"),
        log_level=_string(ffmpeg_raw.get("log_level", "info"), "ffmpeg.log_level"),
        input_rtsp_transport=_transport(
            ffmpeg_raw.get("input_rtsp_transport", "tcp"), "ffmpeg.input_rtsp_transport"
        ),
        input_hwaccel=_input_hwaccel(
            _resolved_string(
                ffmpeg_raw.get("input_hwaccel", "software"), "ffmpeg.input_hwaccel", env
            )
        ),
        hwaccel_device=_resolved_string(
            ffmpeg_raw.get("hwaccel_device", "/dev/dri/renderD128"),
            "ffmpeg.hwaccel_device",
            env,
        ),
        input_timeout_seconds=_nonnegative_int(
            ffmpeg_raw.get("input_timeout_seconds", 0), "ffmpeg.input_timeout_seconds"
        ),
        http_reconnect_delay_max_seconds=_positive_int(
            ffmpeg_raw.get("http_reconnect_delay_max_seconds", 5),
            "ffmpeg.http_reconnect_delay_max_seconds",
        ),
        restart_delay_seconds=_positive_int(
            ffmpeg_raw.get("restart_delay_seconds", 5), "ffmpeg.restart_delay_seconds"
        ),
    )

    workers_raw = _optional_mapping(raw.get("workers"), "workers")
    workers = WorkerConfig(
        enabled=_bool(workers_raw.get("enabled", False), "workers.enabled"),
        mode=_worker_mode(
            _resolved_string(workers_raw.get("mode", "remux"), "workers.mode", env)
        ),
        output_template=_resolved_string(
            workers_raw.get("output_template", ""), "workers.output_template", env
        ),
        rtsp_transport=_transport(
            workers_raw.get("rtsp_transport", "tcp"), "workers.rtsp_transport"
        ),
        restart_delay_seconds=_positive_int(
            workers_raw.get("restart_delay_seconds", 5), "workers.restart_delay_seconds"
        ),
        start_grace_seconds=_nonnegative_int(
            workers_raw.get("start_grace_seconds", 2), "workers.start_grace_seconds"
        ),
        wall_input_preflight=_bool(
            workers_raw.get("wall_input_preflight", False), "workers.wall_input_preflight"
        ),
    )

    inputs = tuple(_parse_input(item, idx, output, env) for idx, item in enumerate(inputs_raw))
    enabled = tuple(item for item in inputs if item.enabled)
    if not enabled:
        raise ConfigError("At least one input must be enabled")
    _validate_unique_names(inputs)
    return AppConfig(output=output, inputs=inputs, ffmpeg=ffmpeg, workers=workers)


def resolve_text(value: str, env: Mapping[str, str] | None = None) -> str:
    env = env or os.environ

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token.startswith("secret:"):
            name = token.removeprefix("secret:")
            return _read_secret(name, env)

        default_match = _ENV_DEFAULT_RE.match(token)
        if default_match:
            name, default = default_match.groups()
            return env.get(name, default)

        if _ENV_NAME_RE.match(token):
            if token not in env:
                raise ConfigError(f"Environment variable is not set: {token}")
            return env[token]

        raise ConfigError(f"Unsupported variable expression: {token}")

    return _EXPANSION_RE.sub(replace, value)


def _load_yaml(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        return yaml.safe_load(text)
    from .simple_yaml import parse_simple_yaml

    return parse_simple_yaml(text)


def _parse_input(
    item: Any, index: int, output: OutputConfig, env: Mapping[str, str]
) -> InputConfig:
    raw = _mapping(item, f"inputs[{index}]")
    enabled = _bool(raw.get("enabled", True), f"inputs[{index}].enabled")
    name = _string(raw.get("name", f"camera-{index + 1}"), f"inputs[{index}].name")

    position = _optional_mapping(raw.get("position"), f"inputs[{index}].position")
    dimensions = {**raw, **position}

    url_value = raw.get("url", "")
    if enabled:
        url = _required_resolved_string(raw, "url", env, prefix=f"inputs[{index}]")
    else:
        url = _string(url_value, f"inputs[{index}].url") if url_value is not None else ""

    label_value = raw.get("label")
    label = None if label_value is None else _string(label_value, f"inputs[{index}].label")
    input_cfg = InputConfig(
        name=name,
        url=url,
        enabled=enabled,
        x=_nonnegative_int(dimensions.get("x"), f"inputs[{index}].x"),
        y=_nonnegative_int(dimensions.get("y"), f"inputs[{index}].y"),
        width=_positive_int(dimensions.get("width"), f"inputs[{index}].width"),
        height=_positive_int(dimensions.get("height"), f"inputs[{index}].height"),
        label=label,
        show_label=_bool(raw.get("show_label", True), f"inputs[{index}].show_label"),
        preserve_aspect=_bool(
            raw.get("preserve_aspect", True), f"inputs[{index}].preserve_aspect"
        ),
        pad_color=_string(raw.get("pad_color", "black"), f"inputs[{index}].pad_color"),
    )
    _validate_bounds(input_cfg, output, index)
    return input_cfg


def _read_secret(name: str, env: Mapping[str, str]) -> str:
    if not name or "/" in name:
        raise ConfigError(f"Invalid secret name: {name}")
    secrets_dir = Path(env.get("CAMERA_WALL_SECRETS_DIR", "/run/secrets"))
    path = secrets_dir / name
    if not path.exists():
        raise ConfigError(f"Secret file not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def _required_resolved_string(
    raw: Mapping[str, Any],
    key: str,
    env: Mapping[str, str],
    prefix: str | None = None,
) -> str:
    path = f"{prefix}.{key}" if prefix else key
    value = _string(raw.get(key), path)
    if not value.strip():
        raise ConfigError(f"{path} is required")
    return resolve_text(value, env)


def _resolved_string(value: Any, name: str, env: Mapping[str, str]) -> str:
    return resolve_text(_string(value, name), env)


def _resolved_maybe_int(value: Any, name: str, env: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        resolved = resolve_text(value, env)
        if re.fullmatch(r"\d+", resolved):
            return int(resolved)
        return resolved
    return value


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{name} must be a mapping")
    return value


def _optional_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    return _mapping(value, name)


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def _positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigError(f"{name} must be a non-negative integer")
    return value


def _qp_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 51:
        raise ConfigError(f"{name} must be an integer between 0 and 51")
    return value


def _encoder(value: Any) -> str:
    encoder = _string(value, "output.encoder").lower()
    if encoder not in {"software", "vaapi", "qsv"}:
        raise ConfigError("output.encoder must be one of: software, vaapi, qsv")
    return encoder


def _vaapi_rc_mode(value: Any) -> str:
    rc_mode = _string(value, "output.vaapi_rc_mode").lower()
    if rc_mode not in {"cqp", "cbr", "vbr", "auto"}:
        raise ConfigError("output.vaapi_rc_mode must be one of: cqp, cbr, vbr, auto")
    return rc_mode


def _input_hwaccel(value: Any) -> str:
    input_hwaccel = _string(value, "ffmpeg.input_hwaccel").lower()
    if input_hwaccel not in {"software", "vaapi"}:
        raise ConfigError("ffmpeg.input_hwaccel must be software or vaapi")
    return input_hwaccel


def _worker_mode(value: Any) -> str:
    mode = _string(value, "workers.mode").lower()
    if mode != "remux":
        raise ConfigError("workers.mode must be remux")
    return mode


def _transport(value: Any, name: str) -> str:
    transport = _string(value, name).lower()
    if transport not in {"tcp", "udp"}:
        raise ConfigError(f"{name} must be tcp or udp")
    return transport


def _validate_bounds(input_cfg: InputConfig, output: OutputConfig, index: int) -> None:
    if input_cfg.x + input_cfg.width > output.width:
        raise ConfigError(f"inputs[{index}] extends beyond output width")
    if input_cfg.y + input_cfg.height > output.height:
        raise ConfigError(f"inputs[{index}] extends beyond output height")


def _validate_unique_names(inputs: tuple[InputConfig, ...]) -> None:
    seen: set[str] = set()
    for item in inputs:
        if item.name in seen:
            raise ConfigError(f"Duplicate input name: {item.name}")
        seen.add(item.name)

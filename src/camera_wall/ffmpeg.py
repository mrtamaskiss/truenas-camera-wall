from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit
import re
import shlex

from .config import AppConfig, InputConfig


_BITRATE_RE = re.compile(r"^(\d+)([kKmM]?)$")
_URL_CREDENTIAL_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]*://)([^/\s:@]+):([^@\s/]+)@([^/\s]+)")
_VAAPI_DEVICE_NAME = "camera_wall_vaapi"


def build_ffmpeg_command(
    config: AppConfig, active_input_names: set[str] | None = None
) -> list[str]:
    output = config.output
    ffmpeg = config.ffmpeg
    inputs = _active_inputs(config, active_input_names)

    args = [
        ffmpeg.binary,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        ffmpeg.log_level,
    ]

    if output.encoder == "vaapi":
        args.extend(
            [
                "-init_hw_device",
                f"vaapi={_VAAPI_DEVICE_NAME}:{output.vaapi_device}",
                "-filter_hw_device",
                _VAAPI_DEVICE_NAME,
            ]
        )
    elif output.encoder == "qsv":
        args.extend(["-qsv_device", output.qsv_device])

    timeout_us = str(ffmpeg.input_timeout_seconds * 1_000_000)
    for input_cfg in inputs:
        args.extend(["-thread_queue_size", "512", "-fflags", "+genpts"])
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
            args.extend(["-rw_timeout", timeout_us])
        args.extend(["-i", input_cfg.url])

    filter_graph = build_filter_graph(config, active_input_names)
    args.extend(["-filter_complex", filter_graph, "-map", "[wall]", "-an"])
    args.extend(_encoder_args(config))
    args.extend(["-r", str(output.fps), "-f", "rtsp", "-rtsp_transport", output.rtsp_transport])
    args.extend(["-muxdelay", "0.1", output.url])
    return args


def build_filter_graph(config: AppConfig, active_input_names: set[str] | None = None) -> str:
    output = config.output
    parts = [f"color=c=black:s={output.width}x{output.height}:r={output.fps},format=yuv420p[base0]"]
    base_label = _add_offline_placeholders(parts, config.enabled_inputs)
    active_inputs = _active_inputs(config, active_input_names)

    for index, input_cfg in enumerate(active_inputs):
        parts.append(_input_filter(index, input_cfg, output.fps, config.ffmpeg.input_hwaccel))

    last_label = base_label
    for index, input_cfg in enumerate(active_inputs):
        next_label = f"tmp{index}" if index < len(active_inputs) - 1 else "wall_raw"
        parts.append(
            f"[{last_label}][v{index}]"
            f"overlay=x={input_cfg.x}:y={input_cfg.y}:shortest=0:eof_action=pass"
            ":repeatlast=1"
            f"[{next_label}]"
        )
        last_label = next_label

    wall_raw = last_label
    if output.encoder == "vaapi":
        parts.append(f"[{wall_raw}]format=nv12,hwupload[wall]")
    elif output.encoder == "qsv":
        parts.append(f"[{wall_raw}]format=nv12[wall]")
    else:
        parts.append(f"[{wall_raw}]format=yuv420p[wall]")
    return ";".join(parts)


def masked_command(args: list[str]) -> str:
    masked = [mask_url(arg) for arg in args]
    return shlex.join(masked)


def mask_text(value: str) -> str:
    return _URL_CREDENTIAL_RE.sub(r"\1***:***@\4", value)


def mask_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc or "@" not in parsed.netloc:
        return value
    host = parsed.netloc.rsplit("@", 1)[1]
    return urlunsplit((parsed.scheme, f"***:***@{host}", parsed.path, parsed.query, parsed.fragment))


def _input_filter(index: int, input_cfg: InputConfig, fps: int, input_hwaccel: str) -> str:
    source = f"[{index}:v]"
    prefix = "hwdownload,format=nv12," if input_hwaccel == "vaapi" else ""
    if input_cfg.preserve_aspect:
        chain = (
            f"{source}{prefix}fps={fps},"
            f"scale=w={input_cfg.width}:h={input_cfg.height}:force_original_aspect_ratio=decrease,"
            f"pad=w={input_cfg.width}:h={input_cfg.height}:x=(ow-iw)/2:y=(oh-ih)/2:"
            f"color={_escape_filter_value(input_cfg.pad_color)},setsar=1"
        )
    else:
        chain = (
            f"{source}{prefix}fps={fps},"
            f"scale=w={input_cfg.width}:h={input_cfg.height},setsar=1"
        )

    if input_cfg.show_label and input_cfg.label:
        chain += (
            ",drawtext="
            f"text='{_escape_drawtext(input_cfg.label)}':"
            "x=12:y=h-th-12:fontcolor=white:fontsize=28:"
            "box=1:boxcolor=black@0.55:boxborderw=8"
        )
    return f"{chain}[v{index}]"


def _add_offline_placeholders(parts: list[str], inputs: tuple[InputConfig, ...]) -> str:
    last_label = "base0"
    for index, input_cfg in enumerate(inputs):
        next_label = f"base{index + 1}"
        text = _escape_drawtext(f"{input_cfg.label or input_cfg.name} offline")
        parts.append(
            f"[{last_label}]drawtext="
            f"text='{text}':"
            f"x={input_cfg.x}+({input_cfg.width}-text_w)/2:"
            f"y={input_cfg.y}+({input_cfg.height}-text_h)/2:"
            "fontcolor=white@0.76:fontsize=30:"
            "box=1:boxcolor=black@0.62:boxborderw=10"
            f"[{next_label}]"
        )
        last_label = next_label
    return last_label


def _active_inputs(
    config: AppConfig, active_input_names: set[str] | None
) -> tuple[InputConfig, ...]:
    if active_input_names is None:
        return config.enabled_inputs
    return tuple(input_cfg for input_cfg in config.enabled_inputs if input_cfg.name in active_input_names)


def _encoder_args(config: AppConfig) -> list[str]:
    output = config.output
    gop = str(output.fps * 2)
    buffer_size = _double_bitrate(output.bitrate)
    bitrate_args = [
        "-b:v",
        output.bitrate,
        "-maxrate",
        output.bitrate,
        "-bufsize",
        buffer_size,
    ]
    gop_args = [
        "-g",
        gop,
        "-keyint_min",
        str(output.fps),
    ]

    if output.encoder == "software":
        return [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            *bitrate_args,
            *gop_args,
            "-sc_threshold",
            "0",
        ]
    if output.encoder == "vaapi":
        args = ["-c:v", "h264_vaapi", "-profile:v", "high"]
        if output.vaapi_rc_mode == "cqp":
            args.extend(["-rc_mode", "CQP", "-qp", str(output.vaapi_qp)])
        elif output.vaapi_rc_mode != "auto":
            args.extend(["-rc_mode", output.vaapi_rc_mode.upper(), *bitrate_args])
        else:
            args.extend(bitrate_args)
        return [*args, *gop_args]
    return ["-c:v", "h264_qsv", "-preset", "veryfast", *bitrate_args, *gop_args, "-look_ahead", "0"]


def _double_bitrate(value: str) -> str:
    match = _BITRATE_RE.match(value)
    if not match:
        return value
    amount, suffix = match.groups()
    return f"{int(amount) * 2}{suffix}"


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

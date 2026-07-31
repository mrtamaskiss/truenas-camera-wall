from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import threading
import time

from .config import AppConfig
from .ffmpeg import mask_url


_ERROR_TERMS = (
    "connection refused",
    "connection timed out",
    "could not connect",
    "failed",
    "input/output error",
    "invalid data",
    "server returned",
    "timed out",
    "unauthorized",
)


@dataclass
class _InputState:
    index: int
    ffmpeg_index: int | None
    name: str
    label: str
    enabled: bool
    url: str
    state: str
    last_error: str | None = None
    last_seen_at: str | None = None

    def payload(self) -> dict[str, object]:
        return {
            "index": self.index,
            "ffmpeg_index": self.ffmpeg_index,
            "name": self.name,
            "label": self.label,
            "enabled": self.enabled,
            "url": mask_url(self.url) if self.url else "",
            "state": self.state,
            "last_error": self.last_error,
            "last_seen_at": self.last_seen_at,
        }


class InputHealthTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: list[_InputState] = []

    def configure(self, config: AppConfig) -> None:
        states: list[_InputState] = []
        ffmpeg_index = 0
        for index, input_cfg in enumerate(config.inputs):
            enabled = input_cfg.enabled
            states.append(
                _InputState(
                    index=index,
                    ffmpeg_index=ffmpeg_index if enabled else None,
                    name=input_cfg.name,
                    label=input_cfg.label or input_cfg.name,
                    enabled=enabled,
                    url=input_cfg.url,
                    state="connecting" if enabled else "disabled",
                )
            )
            if enabled:
                ffmpeg_index += 1
        with self._lock:
            self._states = states

    def clear(self, error: str | None = None) -> None:
        with self._lock:
            for state in self._states:
                if state.enabled:
                    state.state = "failed" if error else "unknown"
                    state.last_error = error
                    state.ffmpeg_index = None

    def mark_preflight(self, active_input_names: set[str], failures: dict[str, str]) -> None:
        with self._lock:
            ffmpeg_index = 0
            for state in self._states:
                if not state.enabled:
                    state.ffmpeg_index = None
                    state.state = "disabled"
                    continue
                if state.name in active_input_names:
                    state.ffmpeg_index = ffmpeg_index
                    state.state = "connecting"
                    state.last_error = None
                    ffmpeg_index += 1
                else:
                    state.ffmpeg_index = None
                    state.state = "offline"
                    state.last_error = failures.get(state.name, "Input is offline")

    def mark_started(self, active_input_names: set[str] | None = None) -> None:
        with self._lock:
            now = _utc_now()
            for state in self._states:
                if state.enabled and (active_input_names is None or state.name in active_input_names):
                    state.state = "active"
                    state.last_error = None
                    state.last_seen_at = now

    def mark_restarting(self, active_input_names: set[str] | None = None) -> None:
        with self._lock:
            for state in self._states:
                if state.enabled and (active_input_names is None or state.name in active_input_names):
                    state.state = "restarting"

    def mark_stopped(self, exit_code: int | None, active_input_names: set[str] | None = None) -> None:
        with self._lock:
            for state in self._states:
                if not state.enabled:
                    continue
                if active_input_names is not None and state.name not in active_input_names:
                    continue
                if exit_code == 0:
                    state.state = "stopped"
                    state.last_error = None
                else:
                    state.state = "failed"
                    state.last_error = f"FFmpeg exited with code {exit_code}"

    def mark_failed(self, name: str, error: str) -> None:
        with self._lock:
            for state in self._states:
                if state.name == name:
                    state.state = "offline"
                    state.last_error = error
                    state.ffmpeg_index = None
                    return

    def process_ffmpeg_line(self, line: str) -> None:
        lowered = line.lower()
        if not any(term in lowered for term in _ERROR_TERMS):
            return
        with self._lock:
            matched = False
            for state in self._states:
                if state.enabled and state.url and state.url in line:
                    state.state = "failed"
                    state.last_error = line
                    matched = True
            if not matched and "ffmpeg exited" not in lowered:
                return

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            return [deepcopy(state.payload()) for state in self._states]


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

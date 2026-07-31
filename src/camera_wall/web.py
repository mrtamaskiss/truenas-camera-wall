from __future__ import annotations

from base64 import b64decode
from dataclasses import dataclass
import hmac
import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .admin_config import dump_yaml, load_admin_config, save_admin_config
from .config import ConfigError
from .diagnostics import (
    diagnose_gpu,
    diagnose_output,
    diagnose_stream,
    gpu_request_from_payload,
    output_request_from_payload,
    stream_request_from_payload,
)
from .log_buffer import log_payload


STATIC_DIR = Path(__file__).with_name("static")
MAX_BODY_BYTES = 512 * 1024


@dataclass(frozen=True)
class WebSettings:
    host: str
    port: int
    username: str
    password: str


class AdminHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler: type[BaseHTTPRequestHandler],
        config_path: Path,
        supervisor: Any,
        settings: WebSettings,
    ) -> None:
        super().__init__(server_address, request_handler)
        self.config_path = config_path
        self.supervisor = supervisor
        self.settings = settings


def start_admin_server(config_path: Path, supervisor: Any, settings: WebSettings) -> AdminHTTPServer:
    server = AdminHTTPServer(
        (settings.host, settings.port),
        AdminRequestHandler,
        config_path,
        supervisor,
        settings,
    )
    return server


class AdminRequestHandler(BaseHTTPRequestHandler):
    server: AdminHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if not self._require_auth():
            return

        path = urlparse(self.path).path
        if path == "/api/config":
            self._send_json(load_admin_config(self.server.config_path))
            return
        if path == "/api/config.yaml":
            self._send_config_download()
            return
        if path == "/api/status":
            self._send_json({"status": self.server.supervisor.status_snapshot()})
            return
        if path == "/api/logs":
            self._send_json(log_payload(_query_int(self.path, "limit", 200)))
            return
        if path in {"/", "/index.html"}:
            self._send_static("index.html", "text/html; charset=utf-8")
            return
        if path == "/static/app.css":
            self._send_static("app.css", "text/css; charset=utf-8")
            return
        if path == "/static/app.js":
            self._send_static("app.js", "application/javascript; charset=utf-8")
            return
        self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        if not self._require_auth():
            return

        path = urlparse(self.path).path
        if path == "/api/config":
            self._handle_save_config()
            return
        if path == "/api/restart":
            self.server.supervisor.request_restart("admin restart")
            self._send_json({"ok": True, "status": self.server.supervisor.status_snapshot()})
            return
        if path == "/api/diagnostics/stream":
            self._handle_stream_diagnostics()
            return
        if path == "/api/diagnostics/output":
            self._handle_output_diagnostics()
            return
        if path == "/api/diagnostics/gpu":
            self._handle_gpu_diagnostics()
            return
        self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, fmt: str, *args: object) -> None:
        level = logging.DEBUG if self.path.startswith(("/api/status", "/api/logs")) else logging.INFO
        logging.log(level, "admin %s - %s", self.address_string(), fmt % args)

    def _handle_save_config(self) -> None:
        try:
            payload = self._read_json_body()
            config_data = payload.get("config", payload) if isinstance(payload, dict) else payload
            if not isinstance(config_data, dict):
                raise ConfigError("Request body must be a config object")
            saved = save_admin_config(self.server.config_path, config_data)
        except ConfigError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except OSError as exc:
            self._send_json({"ok": False, "error": f"Could not write config: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        self.server.supervisor.request_restart("admin config update")
        self._send_json(
            {
                "ok": True,
                "config": saved,
                "status": self.server.supervisor.status_snapshot(),
            }
        )

    def _handle_stream_diagnostics(self) -> None:
        try:
            request = stream_request_from_payload(self._read_json_body())
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": True, "result": diagnose_stream(request)})

    def _handle_output_diagnostics(self) -> None:
        try:
            url, timeout_seconds = output_request_from_payload(self._read_json_body())
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": True, "result": diagnose_output(url, timeout_seconds)})

    def _handle_gpu_diagnostics(self) -> None:
        try:
            device, sample_ms = gpu_request_from_payload(self._read_json_body())
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": True, "result": diagnose_gpu(device, sample_ms)})

    def _read_json_body(self) -> Any:
        length_text = self.headers.get("Content-Length", "0")
        try:
            length = int(length_text)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length > MAX_BODY_BYTES:
            raise ValueError("Request body is too large")
        body = self.rfile.read(length)
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc.msg}") from exc

    def _require_auth(self) -> bool:
        expected_user = self.server.settings.username
        expected_password = self.server.settings.password
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            self._auth_challenge()
            return False

        try:
            decoded = b64decode(header.removeprefix("Basic "), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            self._auth_challenge()
            return False
        user, sep, password = decoded.partition(":")
        if sep != ":":
            self._auth_challenge()
            return False
        if not (
            hmac.compare_digest(user, expected_user)
            and hmac.compare_digest(password, expected_password)
        ):
            self._auth_challenge()
            return False
        return True

    def _auth_challenge(self) -> None:
        body = b"Authentication required\n"
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Camera Wall"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, filename: str, content_type: str) -> None:
        path = STATIC_DIR / filename
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self._send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_config_download(self) -> None:
        try:
            body = self.server.config_path.read_bytes()
        except FileNotFoundError:
            body = dump_yaml(load_admin_config(self.server.config_path)["config"]).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "application/x-yaml; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="camera-wall-config.yaml"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"ok": False, "error": message}, status)


def _query_int(path: str, name: str, default: int) -> int:
    values = parse_qs(urlparse(path).query).get(name)
    if not values:
        return default
    try:
        return int(values[0])
    except ValueError:
        return default

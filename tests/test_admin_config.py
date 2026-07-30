from base64 import b64encode
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from camera_wall.admin_config import load_admin_config, save_admin_config
from camera_wall.config import ConfigError
from camera_wall.web import WebSettings, start_admin_server


def valid_admin_config(count: int = 3):
    return {
        "output": {
            "url": "rtsp://192.168.64.10:8554/camera_wall",
            "width": 1920,
            "height": 1080,
            "fps": 15,
            "bitrate": "5M",
            "encoder": "vaapi",
            "rtsp_transport": "tcp",
            "vaapi_device": "/dev/dri/renderD128",
            "vaapi_rc_mode": "cqp",
            "vaapi_qp": 23,
            "qsv_device": "/dev/dri/renderD128",
        },
        "ffmpeg": {
            "log_level": "warning",
            "input_rtsp_transport": "tcp",
            "input_timeout_seconds": 0,
            "http_reconnect_delay_max_seconds": 5,
            "restart_delay_seconds": 5,
        },
        "inputs": [
            {
                "name": f"camera-{index + 1}",
                "enabled": True,
                "url": f"rtsp://user:pass@192.168.64.{20 + index}/stream1",
                "label": f"Camera {index + 1}",
                "show_label": True,
                "x": 0,
                "y": 0,
                "width": 960,
                "height": 540,
                "preserve_aspect": True,
                "pad_color": "black",
            }
            for index in range(count)
        ],
    }


class AdminConfigTests(unittest.TestCase):
    def test_missing_config_returns_editable_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = load_admin_config(Path(tmp, "config.yaml"))

        self.assertFalse(payload["valid"])
        self.assertEqual(payload["config"]["inputs"], [])

    def test_save_and_load_arbitrary_camera_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.yaml")
            saved = save_admin_config(path, valid_admin_config(5))
            loaded = load_admin_config(path)
            saved_text = path.read_text(encoding="utf-8")

        self.assertEqual(len(saved["inputs"]), 5)
        self.assertTrue(loaded["valid"])
        self.assertEqual(len(loaded["config"]["inputs"]), 5)
        self.assertIn("rtsp://user:pass@", saved_text)

    def test_save_rejects_invalid_config(self) -> None:
        data = valid_admin_config()
        data["output"]["vaapi_qp"] = 99

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                save_admin_config(Path(tmp, "config.yaml"), data)


class FakeSupervisor:
    def __init__(self) -> None:
        self.restart_count = 0

    def status_snapshot(self):
        return {"ffmpeg_running": True, "pid": 1234}

    def request_restart(self, _reason: str) -> None:
        self.restart_count += 1


class WebTests(unittest.TestCase):
    def test_admin_api_requires_basic_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = self._start_server(Path(tmp, "config.yaml"), FakeSupervisor())
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/status"
                with self.assertRaises(urllib.error.HTTPError) as exc:
                    urllib.request.urlopen(url, timeout=5)
                self.assertEqual(exc.exception.code, 401)
                exc.exception.close()

                request = urllib.request.Request(url)
                token = b64encode(b"admin:secret").decode("ascii")
                request.add_header("Authorization", f"Basic {token}")
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
            finally:
                self._stop_server(server, thread)

    def test_admin_api_saves_config_and_requests_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = FakeSupervisor()
            path = Path(tmp, "config.yaml")
            server, thread = self._start_server(path, supervisor)
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/config"
                request = self._request(
                    url,
                    data=json.dumps({"config": valid_admin_config(2)}).encode("utf-8"),
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(supervisor.restart_count, 1)
                self.assertIn("camera-2", path.read_text(encoding="utf-8"))
            finally:
                self._stop_server(server, thread)

    def _start_server(self, path: Path, supervisor: FakeSupervisor):
        try:
            server = start_admin_server(
                path,
                supervisor,
                WebSettings("127.0.0.1", 0, "admin", "secret"),
            )
        except PermissionError:
            self.skipTest("local socket binding is unavailable")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _request(self, url: str, data: bytes | None = None, method: str | None = None):
        request = urllib.request.Request(url, data=data, method=method)
        token = b64encode(b"admin:secret").decode("ascii")
        request.add_header("Authorization", f"Basic {token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        return request

    def _stop_server(self, server, thread: threading.Thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

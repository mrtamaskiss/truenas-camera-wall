import unittest
from unittest.mock import patch

from camera_wall.config import parse_config
from camera_wall.supervisor import CameraWallSupervisor, _runtime_summary


def make_config():
    return parse_config(
        {
            "output": {
                "url": "rtsp://192.168.64.10:8554/camera_wall",
                "width": 1920,
                "height": 1080,
            },
            "inputs": [
                {
                    "name": "camera-1",
                    "enabled": True,
                    "url": "rtsp://camera-1/stream1",
                    "x": 0,
                    "y": 0,
                    "width": 960,
                    "height": 540,
                },
                {
                    "name": "camera-2",
                    "enabled": True,
                    "url": "rtsp://camera-2/stream1",
                    "x": 960,
                    "y": 0,
                    "width": 960,
                    "height": 540,
                },
            ],
        }
    )


def make_worker_config():
    return parse_config(
        {
            "output": {
                "url": "rtsp://192.168.64.10:8554/camera_wall",
                "width": 1920,
                "height": 1080,
            },
            "workers": {
                "enabled": True,
                "mode": "remux",
                "wall_input_preflight": False,
            },
            "inputs": [
                {
                    "name": "camera-1",
                    "enabled": True,
                    "url": "rtsp://camera-1/stream1",
                    "x": 0,
                    "y": 0,
                    "width": 960,
                    "height": 540,
                },
            ],
        }
    )


class SupervisorTests(unittest.TestCase):
    @patch.dict("os.environ", {"CAMERA_WALL_GPU_STATS_ENABLED": "false"})
    def test_preflight_omits_failed_inputs(self) -> None:
        supervisor = CameraWallSupervisor("/tmp/config.yaml")

        def fake_probe(request):
            if request.name == "camera-1":
                return {"ok": True, "message": "ok"}
            return {"ok": False, "message": "Connection timed out"}

        with patch("camera_wall.supervisor.diagnose_stream", side_effect=fake_probe):
            active, failures = supervisor._preflight_inputs(make_config())

        self.assertEqual(active, {"camera-1"})
        self.assertEqual(failures, {"camera-2": "Connection timed out"})
        supervisor.stop()

    def test_runtime_summary_counts_active_and_offline_inputs(self) -> None:
        summary = _runtime_summary(make_config(), {"camera-1"}, True)

        self.assertEqual(summary["enabled_inputs"], 2)
        self.assertEqual(summary["active_inputs"], 1)
        self.assertEqual(summary["offline_inputs"], 1)
        self.assertTrue(summary["input_preflight"])

    def test_runtime_summary_reports_workers(self) -> None:
        summary = _runtime_summary(make_worker_config(), {"camera-1"}, False)

        self.assertEqual(summary["workers"], "remux")
        self.assertEqual(summary["worker_inputs"], 1)
        self.assertFalse(summary["input_preflight"])
        self.assertFalse(summary["worker_wall_preflight"])


if __name__ == "__main__":
    unittest.main()

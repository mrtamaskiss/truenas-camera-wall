import unittest

from camera_wall.config import parse_config
from camera_wall.input_health import InputHealthTracker


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
                    "name": "front",
                    "enabled": True,
                    "url": "rtsp://user:pass@192.168.64.21/stream1",
                    "label": "Front",
                    "x": 0,
                    "y": 0,
                    "width": 960,
                    "height": 540,
                },
                {
                    "name": "disabled",
                    "enabled": False,
                    "url": "",
                    "x": 960,
                    "y": 0,
                    "width": 960,
                    "height": 540,
                },
            ],
        }
    )


class InputHealthTests(unittest.TestCase):
    def test_tracks_configured_inputs_and_masks_urls(self) -> None:
        tracker = InputHealthTracker()
        tracker.configure(make_config())

        snapshot = tracker.snapshot()

        self.assertEqual(snapshot[0]["state"], "connecting")
        self.assertEqual(snapshot[0]["ffmpeg_index"], 0)
        self.assertIn("rtsp://***:***@192.168.64.21/stream1", snapshot[0]["url"])
        self.assertEqual(snapshot[1]["state"], "disabled")
        self.assertIsNone(snapshot[1]["ffmpeg_index"])

    def test_marks_running_and_url_specific_errors(self) -> None:
        tracker = InputHealthTracker()
        tracker.configure(make_config())
        tracker.mark_started()
        tracker.process_ffmpeg_line(
            "rtsp://user:pass@192.168.64.21/stream1: Connection timed out"
        )

        snapshot = tracker.snapshot()

        self.assertEqual(snapshot[0]["state"], "failed")
        self.assertIn("Connection timed out", snapshot[0]["last_error"])
        self.assertEqual(snapshot[1]["state"], "disabled")

    def test_marks_preflight_offline_inputs(self) -> None:
        tracker = InputHealthTracker()
        tracker.configure(make_config())
        tracker.mark_preflight(set(), {"front": "Connection timed out"})

        snapshot = tracker.snapshot()

        self.assertEqual(snapshot[0]["state"], "offline")
        self.assertEqual(snapshot[0]["last_error"], "Connection timed out")
        self.assertIsNone(snapshot[0]["ffmpeg_index"])
        self.assertEqual(snapshot[1]["state"], "disabled")

    def test_started_only_marks_active_preflight_inputs(self) -> None:
        tracker = InputHealthTracker()
        tracker.configure(make_config())
        tracker.mark_preflight(set(), {"front": "Connection timed out"})
        tracker.mark_started(set())

        snapshot = tracker.snapshot()

        self.assertEqual(snapshot[0]["state"], "offline")


if __name__ == "__main__":
    unittest.main()

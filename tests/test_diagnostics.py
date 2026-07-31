import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from camera_wall.diagnostics import (
    StreamProbeRequest,
    diagnose_output,
    diagnose_stream,
    output_request_from_payload,
    stream_request_from_payload,
)


class DiagnosticsTests(unittest.TestCase):
    def test_stream_probe_parses_video_details(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ffprobe"],
            0,
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "h264",
                            "profile": "Main",
                            "width": 1920,
                            "height": 1080,
                            "pix_fmt": "yuv420p",
                            "avg_frame_rate": "25/1",
                        },
                        {"codec_type": "audio", "codec_name": "aac"},
                    ]
                }
            ),
            stderr="",
        )

        with patch("camera_wall.diagnostics.subprocess.run", return_value=completed):
            result = diagnose_stream(
                StreamProbeRequest(
                    url="rtsp://user:pass@192.168.64.21/stream1",
                    name="front",
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["video"]["codec"], "h264")
        self.assertEqual(result["video"]["width"], 1920)
        self.assertEqual(result["video"]["fps"], 25.0)
        self.assertTrue(result["audio_present"])
        self.assertNotIn("user:pass", result["url"])

    def test_stream_probe_reports_auth_error(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ffprobe"],
            1,
            stdout="",
            stderr="Server returned 401 Unauthorized",
        )

        with patch("camera_wall.diagnostics.subprocess.run", return_value=completed):
            result = diagnose_stream(StreamProbeRequest(url="rtsp://camera/stream1"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_kind"], "auth")
        self.assertIn("Authentication failed", result["message"])

    def test_stream_probe_reports_timeout(self) -> None:
        with patch(
            "camera_wall.diagnostics.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["ffprobe"], 1),
        ):
            result = diagnose_stream(StreamProbeRequest(url="rtsp://camera/stream1", timeout_seconds=1))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_kind"], "timeout")

    def test_output_diagnostic_connects_to_target(self) -> None:
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)

        with patch("camera_wall.diagnostics.socket.create_connection", return_value=connection) as connect:
            result = diagnose_output("rtsp://192.168.64.10:8554/camera_wall")

        self.assertTrue(result["ok"])
        self.assertEqual(result["host"], "192.168.64.10")
        self.assertEqual(result["port"], 8554)
        connect.assert_called_once()

    def test_request_payload_validation(self) -> None:
        request = stream_request_from_payload(
            {"url": "rtsp://camera/stream1", "rtsp_transport": "tcp", "timeout_seconds": "3"}
        )
        output_url, timeout = output_request_from_payload(
            {"url": "rtsp://192.168.64.10:8554/camera_wall", "timeout_seconds": "4"}
        )

        self.assertEqual(request.timeout_seconds, 3)
        self.assertEqual(output_url, "rtsp://192.168.64.10:8554/camera_wall")
        self.assertEqual(timeout, 4)


if __name__ == "__main__":
    unittest.main()

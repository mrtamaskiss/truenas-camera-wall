from pathlib import Path
import tempfile
import unittest

from camera_wall.config import ConfigError, parse_config, resolve_text


BASE_CONFIG = {
    "output": {
        "url": "${OUTPUT_URL}",
        "width": 1920,
        "height": 1080,
        "fps": 15,
        "bitrate": "5M",
        "encoder": "software",
    },
    "inputs": [
        {
            "name": "camera-1",
            "enabled": True,
            "url": "${CAMERA_1_URL}",
            "x": 0,
            "y": 0,
            "width": 960,
            "height": 540,
        }
    ],
}


class ConfigTests(unittest.TestCase):
    def test_env_expansion(self) -> None:
        config = parse_config(
            BASE_CONFIG,
            {
                "OUTPUT_URL": "rtsp://192.168.64.10:8554/camera_wall",
                "CAMERA_1_URL": "rtsp://user:pass@192.168.64.21/stream1",
            },
        )

        self.assertEqual(config.output.url, "rtsp://192.168.64.10:8554/camera_wall")
        self.assertEqual(config.enabled_inputs[0].url, "rtsp://user:pass@192.168.64.21/stream1")

    def test_secret_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "camera_1_url").write_text(
                "rtsp://user:pass@192.168.64.21/stream1\n", encoding="utf-8"
            )
            value = resolve_text(
                "${secret:camera_1_url}",
                {"CAMERA_WALL_SECRETS_DIR": tmp},
            )

        self.assertEqual(value, "rtsp://user:pass@192.168.64.21/stream1")

    def test_disabled_inputs_do_not_require_url_env(self) -> None:
        raw = {
            **BASE_CONFIG,
            "inputs": [
                {
                    "name": "disabled",
                    "enabled": False,
                    "url": "${MISSING_CAMERA_URL}",
                    "x": 0,
                    "y": 0,
                    "width": 960,
                    "height": 540,
                },
                BASE_CONFIG["inputs"][0],
            ],
        }
        config = parse_config(
            raw,
            {
                "OUTPUT_URL": "rtsp://192.168.64.10:8554/camera_wall",
                "CAMERA_1_URL": "rtsp://camera/stream1",
            },
        )

        self.assertEqual(len(config.enabled_inputs), 1)

    def test_rejects_out_of_bounds_layout(self) -> None:
        raw = {
            **BASE_CONFIG,
            "inputs": [
                {
                    **BASE_CONFIG["inputs"][0],
                    "x": 1800,
                    "width": 960,
                }
            ],
        }

        with self.assertRaises(ConfigError):
            parse_config(
                raw,
                {
                    "OUTPUT_URL": "rtsp://192.168.64.10:8554/camera_wall",
                    "CAMERA_1_URL": "rtsp://camera/stream1",
                },
            )


if __name__ == "__main__":
    unittest.main()

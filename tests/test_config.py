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

    def test_allows_zero_input_timeout(self) -> None:
        raw = {
            **BASE_CONFIG,
            "ffmpeg": {
                "input_timeout_seconds": 0,
            },
        }
        config = parse_config(
            raw,
            {
                "OUTPUT_URL": "rtsp://192.168.64.10:8554/camera_wall",
                "CAMERA_1_URL": "rtsp://camera/stream1",
            },
        )

        self.assertEqual(config.ffmpeg.input_timeout_seconds, 0)

    def test_parses_vaapi_input_hwaccel(self) -> None:
        raw = {
            **BASE_CONFIG,
            "ffmpeg": {
                "input_hwaccel": "${CAMERA_WALL_INPUT_HWACCEL:-vaapi}",
                "hwaccel_device": "/dev/dri/renderD128",
            },
        }
        config = parse_config(
            raw,
            {
                "OUTPUT_URL": "rtsp://192.168.64.10:8554/camera_wall",
                "CAMERA_1_URL": "rtsp://camera/stream1",
            },
        )

        self.assertEqual(config.ffmpeg.input_hwaccel, "vaapi")
        self.assertEqual(config.ffmpeg.hwaccel_device, "/dev/dri/renderD128")

    def test_rejects_invalid_input_hwaccel(self) -> None:
        raw = {
            **BASE_CONFIG,
            "ffmpeg": {
                "input_hwaccel": "cuda",
            },
        }

        with self.assertRaises(ConfigError):
            parse_config(
                raw,
                {
                    "OUTPUT_URL": "rtsp://192.168.64.10:8554/camera_wall",
                    "CAMERA_1_URL": "rtsp://camera/stream1",
                },
            )

    def test_parses_vaapi_cqp_options(self) -> None:
        raw = {
            **BASE_CONFIG,
            "output": {
                **BASE_CONFIG["output"],
                "encoder": "vaapi",
                "vaapi_rc_mode": "cqp",
                "vaapi_qp": "${CAMERA_WALL_VAAPI_QP:-24}",
            },
        }
        config = parse_config(
            raw,
            {
                "OUTPUT_URL": "rtsp://192.168.64.10:8554/camera_wall",
                "CAMERA_1_URL": "rtsp://camera/stream1",
            },
        )

        self.assertEqual(config.output.vaapi_rc_mode, "cqp")
        self.assertEqual(config.output.vaapi_qp, 24)

    def test_rejects_invalid_vaapi_qp(self) -> None:
        raw = {
            **BASE_CONFIG,
            "output": {
                **BASE_CONFIG["output"],
                "encoder": "vaapi",
                "vaapi_qp": 52,
            },
        }

        with self.assertRaises(ConfigError):
            parse_config(
                raw,
                {
                    "OUTPUT_URL": "rtsp://192.168.64.10:8554/camera_wall",
                    "CAMERA_1_URL": "rtsp://camera/stream1",
                },
            )

    def test_parses_remux_workers(self) -> None:
        raw = {
            **BASE_CONFIG,
            "workers": {
                "enabled": True,
                "mode": "remux",
                "slot_transport": "udp_mpegts",
                "output_template": "${WORKER_TEMPLATE}",
                "wall_input_template": "udp://127.0.0.1:1600{index}?fifo_size=1000",
                "udp_base_port": 15100,
                "udp_fifo_size": 2048,
                "stable_slot_bitrate": "4000k",
                "rtsp_transport": "tcp",
                "fallback_enabled": True,
                "restart_delay_seconds": 7,
                "start_grace_seconds": 1,
                "retry_live_seconds": 17,
                "retry_probe_timeout_seconds": 4,
                "stall_timeout_seconds": 21,
                "freeze_timeout_seconds": 31,
                "wall_input_preflight": True,
            },
        }
        config = parse_config(
            raw,
            {
                "OUTPUT_URL": "rtsp://192.168.64.10:8554/camera_wall",
                "CAMERA_1_URL": "rtsp://camera/stream1",
                "WORKER_TEMPLATE": "rtsp://go2rtc:8554/wall_{name}",
            },
        )

        self.assertTrue(config.workers.enabled)
        self.assertEqual(config.workers.slot_transport, "udp_mpegts")
        self.assertEqual(config.workers.output_template, "rtsp://go2rtc:8554/wall_{name}")
        self.assertEqual(
            config.workers.wall_input_template,
            "udp://127.0.0.1:1600{index}?fifo_size=1000",
        )
        self.assertEqual(config.workers.udp_base_port, 15100)
        self.assertEqual(config.workers.udp_fifo_size, 2048)
        self.assertEqual(config.workers.stable_slot_bitrate, "4000k")
        self.assertEqual(config.workers.restart_delay_seconds, 7)
        self.assertEqual(config.workers.retry_live_seconds, 17)
        self.assertEqual(config.workers.retry_probe_timeout_seconds, 4)
        self.assertEqual(config.workers.stall_timeout_seconds, 21)
        self.assertEqual(config.workers.freeze_timeout_seconds, 31)
        self.assertTrue(config.workers.wall_input_preflight)

    def test_parses_stable_workers(self) -> None:
        raw = {
            **BASE_CONFIG,
            "workers": {
                "enabled": True,
                "mode": "stable",
                "slot_transport": "udp_mpegts",
            },
        }
        config = parse_config(
            raw,
            {
                "OUTPUT_URL": "rtsp://192.168.64.10:8554/camera_wall",
                "CAMERA_1_URL": "rtsp://camera/stream1",
            },
        )

        self.assertTrue(config.workers.enabled)
        self.assertEqual(config.workers.mode, "stable")
        self.assertEqual(config.workers.slot_transport, "udp_mpegts")

    def test_parses_compose_workers(self) -> None:
        raw = {
            **BASE_CONFIG,
            "workers": {
                "enabled": True,
                "mode": "compose",
                "slot_transport": "rtsp",
            },
        }
        config = parse_config(
            raw,
            {
                "OUTPUT_URL": "rtsp://192.168.64.10:8554/camera_wall",
                "CAMERA_1_URL": "rtsp://camera/stream1",
            },
        )

        self.assertTrue(config.workers.enabled)
        self.assertEqual(config.workers.mode, "compose")

    def test_rejects_odd_compose_tile_geometry(self) -> None:
        raw = {
            **BASE_CONFIG,
            "workers": {
                "enabled": True,
                "mode": "compose",
            },
            "inputs": [
                {
                    **BASE_CONFIG["inputs"][0],
                    "width": 959,
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

    def test_rejects_invalid_worker_mode(self) -> None:
        raw = {
            **BASE_CONFIG,
            "workers": {
                "enabled": True,
                "mode": "transcode",
            },
        }

        with self.assertRaises(ConfigError):
            parse_config(
                raw,
                {
                    "OUTPUT_URL": "rtsp://192.168.64.10:8554/camera_wall",
                    "CAMERA_1_URL": "rtsp://camera/stream1",
                },
            )

    def test_rejects_invalid_worker_slot_transport(self) -> None:
        raw = {
            **BASE_CONFIG,
            "workers": {
                "enabled": True,
                "slot_transport": "srt",
            },
        }

        with self.assertRaises(ConfigError):
            parse_config(
                raw,
                {
                    "OUTPUT_URL": "rtsp://192.168.64.10:8554/camera_wall",
                    "CAMERA_1_URL": "rtsp://camera/stream1",
                },
            )

    def test_rejects_worker_udp_port_overflow(self) -> None:
        raw = {
            **BASE_CONFIG,
            "workers": {
                "enabled": True,
                "slot_transport": "udp_mpegts",
                "udp_base_port": 65535,
            },
            "inputs": [
                BASE_CONFIG["inputs"][0],
                {
                    **BASE_CONFIG["inputs"][0],
                    "name": "camera-2",
                    "x": 960,
                },
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

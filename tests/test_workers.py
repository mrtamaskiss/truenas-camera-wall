import unittest

from camera_wall.config import parse_config
from camera_wall.workers import (
    build_remux_slots,
    build_remux_worker_command,
    build_worker_wall_config,
    worker_output_url,
)


def make_config(worker_overrides: dict | None = None):
    workers = {
        "enabled": True,
        "mode": "remux",
        "slot_transport": "rtsp",
        "rtsp_transport": "tcp",
        "fallback_enabled": True,
        "restart_delay_seconds": 5,
        "start_grace_seconds": 2,
        "retry_live_seconds": 15,
        "stall_timeout_seconds": 20,
        "wall_input_preflight": False,
    }
    if worker_overrides:
        workers.update(worker_overrides)
    return parse_config(
        {
            "output": {
                "url": "rtsp://192.168.64.10:8554/camera_wall",
                "width": 1920,
                "height": 1080,
                "fps": 15,
                "bitrate": "5M",
                "encoder": "software",
            },
            "ffmpeg": {
                "input_rtsp_transport": "tcp",
            },
            "workers": workers,
            "inputs": [
                {
                    "name": "camera-1",
                    "enabled": True,
                    "url": "rtsp://user:pass@192.168.64.21/stream1",
                    "x": 0,
                    "y": 0,
                    "width": 960,
                    "height": 540,
                },
                {
                    "name": "Back Yard",
                    "enabled": True,
                    "url": "http://192.168.64.22/live.m3u8",
                    "x": 960,
                    "y": 0,
                    "width": 960,
                    "height": 540,
                },
            ],
        }
    )


class WorkerTests(unittest.TestCase):
    def test_derives_worker_output_url_from_wall_output(self) -> None:
        config = make_config()

        self.assertEqual(
            worker_output_url(config, config.inputs[0], 0),
            "rtsp://192.168.64.10:8554/camera_wall_camera-1",
        )
        self.assertEqual(
            worker_output_url(config, config.inputs[1], 1),
            "rtsp://192.168.64.10:8554/camera_wall_Back_Yard",
        )

    def test_worker_output_template_supports_name_and_index(self) -> None:
        config = make_config(
            {"output_template": "rtsp://go2rtc:8554/wall_slot_{index}_{name}"}
        )

        self.assertEqual(
            worker_output_url(config, config.inputs[1], 1),
            "rtsp://go2rtc:8554/wall_slot_2_Back_Yard",
        )

    def test_builds_remux_worker_command(self) -> None:
        config = make_config()
        command = build_remux_worker_command(
            config,
            config.inputs[0],
            "rtsp://192.168.64.10:8554/camera_wall_camera-1",
        )

        self.assertIn("-c:v", command)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertIn("-progress", command)
        self.assertIn("-map", command)
        self.assertEqual(command[command.index("-map") + 1], "0:v:0")
        self.assertIn("-rtsp_transport", command)
        self.assertEqual(command[-1], "rtsp://192.168.64.10:8554/camera_wall_camera-1")

    def test_udp_mpegts_slots_use_local_ports_and_fallback(self) -> None:
        config = make_config({"slot_transport": "udp_mpegts", "udp_base_port": 15100})
        slots = build_remux_slots(config)
        wall_config = build_worker_wall_config(config)

        self.assertEqual(slots[0].output_url, "udp://127.0.0.1:15100?pkt_size=1316")
        self.assertEqual(
            slots[0].wall_input_url,
            "udp://127.0.0.1:15100?fifo_size=5000000&overrun_nonfatal=1",
        )
        self.assertIn("-f", slots[0].command)
        self.assertEqual(slots[0].command[slots[0].command.index("-f") + 1], "mpegts")
        self.assertIsNotNone(slots[0].fallback_command)
        self.assertIn("lavfi", slots[0].fallback_command or [])
        self.assertEqual(wall_config.inputs[0].url, slots[0].wall_input_url)

    def test_udp_wall_input_template_overrides_derived_url(self) -> None:
        config = make_config(
            {
                "slot_transport": "udp_mpegts",
                "output_template": "udp://127.0.0.1:1600{index}?pkt_size=1316",
                "wall_input_template": "udp://127.0.0.1:1600{index}?fifo_size=1000",
            }
        )
        slots = build_remux_slots(config)

        self.assertEqual(slots[1].output_url, "udp://127.0.0.1:16002?pkt_size=1316")
        self.assertEqual(slots[1].wall_input_url, "udp://127.0.0.1:16002?fifo_size=1000")

    def test_http_worker_command_keeps_reconnect_options(self) -> None:
        config = make_config()
        command = build_remux_worker_command(
            config,
            config.inputs[1],
            "rtsp://192.168.64.10:8554/camera_wall_Back_Yard",
        )

        self.assertIn("-reconnect", command)
        self.assertIn("-reconnect_streamed", command)

    def test_wall_config_reads_worker_outputs(self) -> None:
        config = make_config()
        wall_config = build_worker_wall_config(config)

        self.assertEqual(
            wall_config.inputs[0].url,
            "rtsp://192.168.64.10:8554/camera_wall_camera-1",
        )
        self.assertEqual(config.inputs[0].url, "rtsp://user:pass@192.168.64.21/stream1")

    def test_remux_slots_include_enabled_inputs_only(self) -> None:
        config = make_config()
        slots = build_remux_slots(config)

        self.assertEqual([slot.name for slot in slots], ["camera-1", "Back Yard"])
        self.assertTrue(all("-c:v" in slot.command for slot in slots))


if __name__ == "__main__":
    unittest.main()

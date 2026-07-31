import time
import unittest

from camera_wall.compositor import (
    CameraRuntime,
    build_camera_decoder_command,
    compose_frame,
    tile_frame_size,
)
from camera_wall.config import parse_config


def make_config():
    return parse_config(
        {
            "output": {
                "url": "rtsp://192.168.64.10:8554/camera_wall",
                "width": 4,
                "height": 4,
                "fps": 10,
                "bitrate": "1M",
                "encoder": "software",
            },
            "workers": {
                "enabled": True,
                "mode": "compose",
            },
            "inputs": [
                {
                    "name": "a",
                    "enabled": True,
                    "url": "rtsp://camera/a",
                    "label": "A",
                    "x": 0,
                    "y": 0,
                    "width": 2,
                    "height": 2,
                },
                {
                    "name": "b",
                    "enabled": True,
                    "url": "rtsp://camera/b",
                    "label": "B",
                    "x": 2,
                    "y": 2,
                    "width": 2,
                    "height": 2,
                },
            ],
        }
    )


def yuv_tile(y_value: int, u_value: int, v_value: int) -> bytes:
    return bytes([y_value] * 4 + [u_value] + [v_value])


class CompositorTests(unittest.TestCase):
    def test_camera_decoder_command_outputs_raw_tile(self) -> None:
        config = make_config()
        command = build_camera_decoder_command(config, config.inputs[0])

        self.assertIn("-vf", command)
        filter_graph = command[command.index("-vf") + 1]
        self.assertIn("scale=w=2:h=2:force_original_aspect_ratio=decrease", filter_graph)
        self.assertEqual(command[-2:], ["rawvideo", "pipe:1"])

    def test_compose_frame_uses_live_and_offline_tiles(self) -> None:
        config = make_config()
        camera_a = CameraRuntime(config.inputs[0])
        camera_b = CameraRuntime(config.inputs[1])
        camera_a.frame = yuv_tile(80, 90, 100)
        camera_a.frame_at = time.monotonic()
        offline = {
            "a": yuv_tile(16, 128, 128),
            "b": yuv_tile(40, 130, 140),
        }

        frame = compose_frame(config, [camera_a, camera_b], offline)
        y_size = 16
        u_size = 4
        y_plane = frame[:y_size]
        u_plane = frame[y_size : y_size + u_size]
        v_plane = frame[y_size + u_size :]

        self.assertEqual(len(frame), 24)
        self.assertEqual(tile_frame_size(config.inputs[0]), 6)
        self.assertEqual(y_plane[0], 80)
        self.assertEqual(y_plane[1], 80)
        self.assertEqual(y_plane[4], 80)
        self.assertEqual(y_plane[5], 80)
        self.assertEqual(y_plane[10], 40)
        self.assertEqual(y_plane[15], 40)
        self.assertEqual(u_plane[0], 90)
        self.assertEqual(v_plane[0], 100)
        self.assertEqual(u_plane[3], 130)
        self.assertEqual(v_plane[3], 140)


if __name__ == "__main__":
    unittest.main()

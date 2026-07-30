import unittest

from camera_wall.config import parse_config
from camera_wall.ffmpeg import build_ffmpeg_command, build_filter_graph, masked_command


def make_config(
    encoder: str = "software",
    output_overrides: dict | None = None,
    ffmpeg_overrides: dict | None = None,
):
    output = {
        "url": "rtsp://192.168.64.10:8554/camera_wall",
        "width": 1920,
        "height": 1080,
        "fps": 15,
        "bitrate": "5M",
        "encoder": encoder,
    }
    if output_overrides:
        output.update(output_overrides)
    ffmpeg = {}
    if ffmpeg_overrides:
        ffmpeg.update(ffmpeg_overrides)
    return parse_config(
        {
            "output": output,
            "ffmpeg": ffmpeg,
            "inputs": [
                {
                    "name": "camera-1",
                    "enabled": True,
                    "url": "rtsp://user:pass@192.168.64.21/stream1",
                    "label": "Camera 1",
                    "x": 0,
                    "y": 0,
                    "width": 960,
                    "height": 540,
                },
                {
                    "name": "camera-2",
                    "enabled": True,
                    "url": "rtsp://user:pass@192.168.64.22/stream1",
                    "x": 960,
                    "y": 0,
                    "width": 960,
                    "height": 540,
                },
                {
                    "name": "camera-3",
                    "enabled": True,
                    "url": "rtsp://user:pass@192.168.64.23/stream1",
                    "x": 0,
                    "y": 540,
                    "width": 1920,
                    "height": 540,
                },
            ],
        }
    )


def make_config_with_timeout(seconds: int):
    raw = {
        "output": {
            "url": "rtsp://192.168.64.10:8554/camera_wall",
            "width": 1920,
            "height": 1080,
            "fps": 15,
            "bitrate": "5M",
            "encoder": "software",
        },
        "ffmpeg": {
            "input_timeout_seconds": seconds,
        },
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
        ],
    }
    return parse_config(raw)


class FfmpegTests(unittest.TestCase):
    def test_filter_preserves_aspect_and_pads_first_layout(self) -> None:
        graph = build_filter_graph(make_config())

        self.assertIn("color=c=black:s=1920x1080:r=15", graph)
        self.assertIn("scale=w=960:h=540:force_original_aspect_ratio=decrease", graph)
        self.assertIn("pad=w=960:h=540:x=(ow-iw)/2:y=(oh-ih)/2:color=black", graph)
        self.assertIn("overlay=x=960:y=0", graph)
        self.assertIn("overlay=x=0:y=540", graph)
        self.assertIn("[wall_raw]format=yuv420p[wall]", graph)

    def test_software_encoder_args(self) -> None:
        command = build_ffmpeg_command(make_config())

        self.assertIn("libx264", command)
        self.assertIn("zerolatency", command)
        self.assertNotIn("-rw_timeout", command)
        self.assertIn("-bufsize", command)
        self.assertEqual(command[command.index("-bufsize") + 1], "10M")
        self.assertEqual(command[-1], "rtsp://192.168.64.10:8554/camera_wall")

    def test_input_timeout_is_optional(self) -> None:
        command = build_ffmpeg_command(make_config_with_timeout(15))

        self.assertIn("-rw_timeout", command)
        self.assertEqual(command[command.index("-rw_timeout") + 1], "15000000")

    def test_vaapi_encoder_args(self) -> None:
        command = build_ffmpeg_command(make_config("vaapi"))
        graph = build_filter_graph(make_config("vaapi"))

        self.assertIn("-vaapi_device", command)
        self.assertIn("h264_vaapi", command)
        self.assertIn("-rc_mode", command)
        self.assertEqual(command[command.index("-rc_mode") + 1], "CQP")
        self.assertIn("-qp", command)
        self.assertEqual(command[command.index("-qp") + 1], "23")
        self.assertNotIn("-b:v", command)
        self.assertNotIn("-maxrate", command)
        self.assertNotIn("-bufsize", command)
        self.assertIn("[wall_raw]format=nv12,hwupload[wall]", graph)

    def test_vaapi_cbr_encoder_args_keep_bitrate(self) -> None:
        command = build_ffmpeg_command(make_config("vaapi", {"vaapi_rc_mode": "cbr"}))

        self.assertIn("-rc_mode", command)
        self.assertEqual(command[command.index("-rc_mode") + 1], "CBR")
        self.assertIn("-b:v", command)
        self.assertIn("-maxrate", command)
        self.assertIn("-bufsize", command)
        self.assertNotIn("-qp", command)

    def test_vaapi_input_decode_args(self) -> None:
        config = make_config(
            "vaapi",
            ffmpeg_overrides={
                "input_hwaccel": "vaapi",
                "hwaccel_device": "/dev/dri/renderD128",
            },
        )
        command = build_ffmpeg_command(config)
        graph = build_filter_graph(config)

        self.assertEqual(command.count("-hwaccel"), 3)
        self.assertIn("-hwaccel_device", command)
        self.assertEqual(command[command.index("-hwaccel_device") + 1], "/dev/dri/renderD128")
        self.assertIn("-hwaccel_output_format", command)
        self.assertIn("[0:v]hwdownload,format=nv12,fps=15", graph)
        self.assertIn("[wall_raw]format=nv12,hwupload[wall]", graph)

    def test_qsv_encoder_args(self) -> None:
        command = build_ffmpeg_command(make_config("qsv"))
        graph = build_filter_graph(make_config("qsv"))

        self.assertIn("-qsv_device", command)
        self.assertIn("h264_qsv", command)
        self.assertIn("[wall_raw]format=nv12[wall]", graph)

    def test_masked_command_hides_credentials(self) -> None:
        rendered = masked_command(build_ffmpeg_command(make_config()))

        self.assertNotIn("user:pass", rendered)
        self.assertIn("rtsp://***:***@192.168.64.21/stream1", rendered)


if __name__ == "__main__":
    unittest.main()

import time
import unittest

from camera_wall.slot_worker import (
    LatestFrame,
    SlotSettings,
    black_yuv420p_frame,
    build_decoder_command,
    build_encoder_command,
    build_offline_frame_command,
    frame_size,
)


def make_settings(**overrides):
    values = {
        "ffmpeg_binary": "ffmpeg",
        "log_level": "warning",
        "name": "camera-1",
        "input_url": "rtsp://user:pass@192.168.64.21/stream1",
        "output_url": "udp://127.0.0.1:15000?pkt_size=1316",
        "width": 960,
        "height": 540,
        "fps": 15,
        "preserve_aspect": True,
        "pad_color": "black",
        "offline_text": "Camera 1 offline",
        "input_rtsp_transport": "tcp",
        "input_hwaccel": "software",
        "hwaccel_device": "/dev/dri/renderD128",
        "input_timeout_seconds": 0,
        "http_reconnect_delay_max_seconds": 5,
        "slot_transport": "udp_mpegts",
        "worker_rtsp_transport": "tcp",
        "restart_delay_seconds": 5,
        "stall_timeout_seconds": 3,
        "freeze_timeout_seconds": 20,
        "bitrate": "1200k",
    }
    values.update(overrides)
    return SlotSettings(**values)


class SlotWorkerTests(unittest.TestCase):
    def test_decoder_outputs_fixed_rawvideo_tile(self) -> None:
        command = build_decoder_command(make_settings())

        self.assertIn("-rtsp_transport", command)
        self.assertIn("tcp", command)
        self.assertIn("+genpts+nobuffer+discardcorrupt", command)
        self.assertIn("-avioflags", command)
        self.assertEqual(command[command.index("-avioflags") + 1], "direct")
        self.assertIn("low_delay", command)
        self.assertIn("-max_delay", command)
        self.assertEqual(command[command.index("-max_delay") + 1], "0")
        self.assertIn("-reorder_queue_size", command)
        self.assertEqual(command[command.index("-reorder_queue_size") + 1], "0")
        self.assertIn("-use_wallclock_as_timestamps", command)
        self.assertEqual(command[command.index("-probesize") + 1], "262144")
        self.assertEqual(command[command.index("-analyzeduration") + 1], "2000000")
        self.assertIn("-vf", command)
        filter_graph = command[command.index("-vf") + 1]
        self.assertIn("scale=w=960:h=540:force_original_aspect_ratio=decrease", filter_graph)
        self.assertIn("pad=w=960:h=540", filter_graph)
        self.assertEqual(command[-2:], ["rawvideo", "pipe:1"])

    def test_encoder_publishes_constant_h264_udp_slot(self) -> None:
        command = build_encoder_command(make_settings())

        self.assertIn("libx264", command)
        self.assertIn("repeat-headers=1:scenecut=0", command)
        self.assertIn("-bf", command)
        self.assertEqual(command[command.index("-bf") + 1], "0")
        self.assertEqual(command[command.index("-g") + 1], "15")
        self.assertIn("-flush_packets", command)
        self.assertIn("-f", command)
        self.assertEqual(command[command.index("-f", command.index("-x264-params")) + 1], "mpegts")
        self.assertEqual(command[-1], "udp://127.0.0.1:15000?pkt_size=1316")

    def test_offline_frame_command_uses_drawtext(self) -> None:
        command = build_offline_frame_command(make_settings())

        self.assertIn("lavfi", command)
        self.assertTrue(any("drawtext=" in item for item in command))
        self.assertEqual(command[-2:], ["rawvideo", "pipe:1"])

    def test_yuv420p_frame_helpers(self) -> None:
        settings = make_settings(width=4, height=2)

        self.assertEqual(frame_size(settings), 12)
        self.assertEqual(len(black_yuv420p_frame(4, 2)), 12)

    def test_latest_frame_holds_reconnect_frames_until_live_rate_returns(self) -> None:
        latest = LatestFrame()
        frame = b"abc"

        latest.reset(catchup_seconds=20)
        latest.update(frame, fps=15, catchup_timeout_seconds=20)
        self.assertEqual(latest.snapshot()[0], None)
        self.assertTrue(latest.snapshot()[2])

        latest._catching_up_until = 0  # Exercise the release path without sleeping.
        latest._last_read_at = time.monotonic() - 1
        latest._normal_frame_streak = 14
        latest.update(frame, fps=15, catchup_timeout_seconds=20)

        snapshot = latest.snapshot()
        self.assertEqual(snapshot[0], frame)
        self.assertFalse(snapshot[2])


if __name__ == "__main__":
    unittest.main()

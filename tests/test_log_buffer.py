import logging
import unittest

from camera_wall.log_buffer import clear_logs, get_logs, install_log_buffer


class LogBufferTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_logs()
        install_log_buffer()

    def tearDown(self) -> None:
        clear_logs()

    def test_collects_and_masks_log_messages(self) -> None:
        logging.warning("failed input %s", "rtsp://user:pass@192.168.64.21/stream1")

        logs = get_logs()

        self.assertTrue(logs)
        self.assertNotIn("user:pass", logs[-1]["message"])
        self.assertIn("rtsp://***:***@192.168.64.21/stream1", logs[-1]["message"])


if __name__ == "__main__":
    unittest.main()

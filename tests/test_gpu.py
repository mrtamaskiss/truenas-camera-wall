import unittest

from camera_wall.gpu import parse_intel_gpu_top_json


class GpuTests(unittest.TestCase):
    def test_parse_intel_gpu_top_json(self) -> None:
        payload = """
        [
          {
            "frequency": {"actual": 450.0, "unit": "MHz"},
            "rc6": {"value": 12.5, "unit": "%"},
            "engines": {
              "Render/3D": {"busy": 4.2, "unit": "%"},
              "Video": {"busy": 31.7, "unit": "%"},
              "Blitter": {"busy": 1.0, "unit": "%"}
            }
          }
        ]
        """

        stats = parse_intel_gpu_top_json(payload, "/dev/dri/renderD128")

        self.assertTrue(stats["available"])
        self.assertEqual(stats["source"], "intel_gpu_top")
        self.assertEqual(stats["load_percent"], 36.9)
        self.assertEqual(stats["render_percent"], 4.2)
        self.assertEqual(stats["video_percent"], 31.7)
        self.assertEqual(stats["blitter_percent"], 1.0)
        self.assertEqual(stats["frequency_mhz"], 450.0)
        self.assertEqual(stats["rc6_percent"], 12.5)

    def test_parse_partial_json_stream(self) -> None:
        payload = """
        [
          {"engines": {"Video/0": {"busy": "12.25", "unit": "%"}}},
        """

        stats = parse_intel_gpu_top_json(payload)

        self.assertEqual(stats["load_percent"], 12.2)
        self.assertEqual(stats["video_percent"], 12.2)


if __name__ == "__main__":
    unittest.main()

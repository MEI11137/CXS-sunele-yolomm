import os
import sys
import tempfile
import unittest

import cv2
import numpy as np


WEB_FLASK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WEB_FLASK_ROOT not in sys.path:
    sys.path.insert(0, WEB_FLASK_ROOT)


class BrowserCompatibleVideoWriterTest(unittest.TestCase):
    def test_generated_video_uses_a_browser_supported_codec(self):
        try:
            from utils.video_writer import create_browser_video_writer
        except ModuleNotFoundError:
            self.fail("Browser-compatible video writer is not implemented")

        test_temp_root = os.path.join(WEB_FLASK_ROOT, "temp")
        os.makedirs(test_temp_root, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_temp_root, ignore_cleanup_errors=True) as output_dir:
            writer, output_path, selected_codec = create_browser_video_writer(
                output_dir=output_dir,
                filename_stem="result",
                fps=2.0,
                frame_size=(1500, 1100),
            )
            self.assertTrue(writer.isOpened())

            frame = np.zeros((1100, 1500, 3), dtype=np.uint8)
            for _ in range(3):
                writer.write(frame)
            writer.release()

            capture = cv2.VideoCapture(output_path)
            self.assertTrue(capture.isOpened())
            fourcc_value = int(capture.get(cv2.CAP_PROP_FOURCC))
            fourcc = "".join(chr((fourcc_value >> (8 * index)) & 0xFF) for index in range(4))
            readable, _ = capture.read()
            capture.release()

            self.assertTrue(readable)
            self.assertIn(fourcc.lower(), {"h264", "avc1", "vp80", "vp90"})
            self.assertNotIn(fourcc.lower(), {"fmp4", "mp4v"})
            self.assertIn(os.path.splitext(output_path)[1].lower(), {".mp4", ".webm"})
            self.assertIn(selected_codec.lower(), {"avc1", "h264", "vp80", "vp90"})


if __name__ == "__main__":
    unittest.main()

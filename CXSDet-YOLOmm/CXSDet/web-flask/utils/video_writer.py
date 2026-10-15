import os
import uuid

import cv2
import numpy as np


_BROWSER_VIDEO_CANDIDATES = (
    ("avc1", ".mp4", {"h264", "avc1"}),
    ("H264", ".mp4", {"h264", "avc1"}),
    ("VP80", ".webm", {"vp80"}),
    ("VP90", ".webm", {"vp90"}),
)


def _video_fourcc(path: str) -> tuple[bool, str]:
    capture = cv2.VideoCapture(path)
    if not capture.isOpened():
        capture.release()
        return False, ""

    fourcc_value = int(capture.get(cv2.CAP_PROP_FOURCC))
    fourcc = "".join(chr((fourcc_value >> (8 * index)) & 0xFF) for index in range(4))
    readable, _ = capture.read()
    capture.release()
    return readable, fourcc.lower()


def create_browser_video_writer(
    output_dir: str,
    filename_stem: str,
    fps: float,
    frame_size: tuple[int, int],
):
    """Create an OpenCV writer whose output is playable by modern browsers."""
    os.makedirs(output_dir, exist_ok=True)
    width, height = frame_size
    probe_frame = np.zeros((height, width, 3), dtype=np.uint8)
    failures = []

    for codec_name, extension, accepted_fourccs in _BROWSER_VIDEO_CANDIDATES:
        probe_path = os.path.join(output_dir, f".codec_probe_{uuid.uuid4().hex}{extension}")
        probe_writer = cv2.VideoWriter(
            probe_path,
            cv2.VideoWriter_fourcc(*codec_name),
            fps,
            frame_size,
        )

        if not probe_writer.isOpened():
            probe_writer.release()
            failures.append(f"{codec_name}: writer unavailable")
            continue

        probe_writer.write(probe_frame)
        probe_writer.release()
        readable, actual_fourcc = _video_fourcc(probe_path)
        try:
            if os.path.exists(probe_path):
                os.remove(probe_path)
        except OSError:
            pass

        if not readable or actual_fourcc not in accepted_fourccs:
            failures.append(f"{codec_name}: produced {actual_fourcc or 'unreadable'}")
            continue

        output_path = os.path.join(output_dir, f"{filename_stem}{extension}")
        if os.path.exists(output_path):
            os.remove(output_path)
        writer = cv2.VideoWriter(
            output_path,
            cv2.VideoWriter_fourcc(*codec_name),
            fps,
            frame_size,
        )
        if writer.isOpened():
            return writer, output_path, codec_name
        writer.release()
        failures.append(f"{codec_name}: final writer unavailable")

    raise RuntimeError(
        "No browser-compatible video encoder is available: " + "; ".join(failures)
    )

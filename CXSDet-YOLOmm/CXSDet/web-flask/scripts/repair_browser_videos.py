import json
import os
import sqlite3
import sys
import uuid

import cv2


WEB_FLASK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WEB_FLASK_ROOT not in sys.path:
    sys.path.insert(0, WEB_FLASK_ROOT)

from config import DB_PATH, FILE_STORE_CONFIG
from utils.video_writer import create_browser_video_writer


BROWSER_FOURCCS = {"h264", "avc1", "vp80", "vp90"}


def read_video_info(path):
    capture = cv2.VideoCapture(path)
    if not capture.isOpened():
        capture.release()
        raise RuntimeError(f"Cannot open video: {path}")

    fourcc_value = int(capture.get(cv2.CAP_PROP_FOURCC))
    fourcc = "".join(chr((fourcc_value >> (8 * index)) & 0xFF) for index in range(4))
    info = {
        "fourcc": fourcc.lower(),
        "fps": capture.get(cv2.CAP_PROP_FPS) or 25.0,
        "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        "frames": int(capture.get(cv2.CAP_PROP_FRAME_COUNT)),
    }
    capture.release()
    return info


def repair_record(connection, row):
    bucket = row["result_video_bucket"]
    old_key = row["result_video_object_key"]
    source_path = os.path.join(FILE_STORE_CONFIG["base_path"], bucket, old_key)
    info = read_video_info(source_path)
    if info["fourcc"] in BROWSER_FOURCCS:
        return {"id": row["id"], "status": "already-compatible", "key": old_key, **info}

    capture = cv2.VideoCapture(source_path)
    output_dir = os.path.dirname(source_path)
    writer, output_path, selected_codec = create_browser_video_writer(
        output_dir=output_dir,
        filename_stem=str(uuid.uuid4()),
        fps=info["fps"],
        frame_size=(info["width"], info["height"]),
    )

    written_frames = 0
    while True:
        readable, frame = capture.read()
        if not readable:
            break
        writer.write(frame)
        written_frames += 1
    capture.release()
    writer.release()

    repaired_info = read_video_info(output_path)
    if repaired_info["fourcc"] not in BROWSER_FOURCCS or written_frames == 0:
        raise RuntimeError(f"Repaired video is not browser compatible: {output_path}")

    new_key = os.path.basename(output_path)
    connection.execute(
        "UPDATE video_detection_records SET result_video_object_key = ? WHERE id = ?",
        (new_key, row["id"]),
    )
    connection.commit()
    return {
        "id": row["id"],
        "status": "repaired",
        "old_key": old_key,
        "new_key": new_key,
        "selected_codec": selected_codec,
        "written_frames": written_frames,
        **repaired_info,
    }


def main():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT id, result_video_bucket, result_video_object_key
        FROM video_detection_records
        WHERE result_video_bucket IS NOT NULL
          AND result_video_object_key IS NOT NULL
        """
    ).fetchall()

    results = []
    try:
        for row in rows:
            results.append(repair_record(connection, row))
    finally:
        connection.close()
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

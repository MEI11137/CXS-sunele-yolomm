"""YOLO 标签与框坐标换算。

库里一律存**原图像素** xyxy（与检测记录同口径），只有写 ``.txt`` 时才转成
归一化 ``cx cy w h``。这样页面缩放/拖拽/滚动都不会改变真实坐标，
导出时也不会因为前端视口大小产生偏移。
"""

from __future__ import annotations

import io
from pathlib import Path

from backend.errors import ApiError
from backend.services import storage


def to_yolo_lines(boxes: list[dict], width: int, height: int) -> list[str]:
    if width <= 0 or height <= 0:
        raise ApiError(409, "IMAGE_SIZE_UNKNOWN", "缺少原图尺寸，不能生成 YOLO 标签")
    lines: list[str] = []
    for box in boxes:
        class_id = int(box["class_id"])
        x1, y1, x2, y2 = (float(value) for value in box["bbox"])
        cx = (x1 + x2) / 2 / width
        cy = (y1 + y2) / 2 / height
        w = (x2 - x1) / width
        h = (y2 - y1) / height
        if not (0 < w <= 1 and 0 < h <= 1):
            raise ApiError(400, "BAD_BOX", "标注框换算后不在 0-1 范围内")
        lines.append(f"{class_id} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
    return lines


def label_bytes(boxes: list[dict], width: int, height: int) -> bytes:
    lines = to_yolo_lines(boxes, width, height)
    text = "\n".join(lines)
    if text:
        text += "\n"
    return text.encode("utf-8")


def write_label(root: Path, directory: Path, stem: str, boxes: list[dict], width: int, height: int) -> Path:
    """在项目内写 ``<stem>.txt``；空标注也写空文件（“确认无缺陷”是有效结论）。"""
    return storage.write_bytes(root, directory / f"{stem}.txt", label_bytes(boxes, width, height))


def from_yolo_lines(lines: list[str], width: int, height: int, names: list[str]) -> list[dict]:
    """把 label 文件读回像素框（用于导入时展示与冲突检查）。"""
    boxes: list[dict] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 5:
            continue
        class_id = int(float(parts[0]))
        cx, cy, w, h = (float(value) for value in parts[1:5])
        boxes.append(
            {
                "class_id": class_id,
                "class_name": names[class_id] if 0 <= class_id < len(names) else str(class_id),
                "confidence": 1.0,
                "bbox": [
                    round((cx - w / 2) * width, 3),
                    round((cy - h / 2) * height, 3),
                    round((cx + w / 2) * width, 3),
                    round((cy + h / 2) * height, 3),
                ],
                "severity": "medium",
                "source": "imported",
            }
        )
    return boxes


def preview_boxes(boxes: list[dict], width: int, height: int) -> list[dict]:
    """前端预览用的归一化框（0-1），只用于画布定位，不参与落库。"""
    if width <= 0 or height <= 0:
        return []
    out = []
    for box in boxes:
        x1, y1, x2, y2 = (float(value) for value in box["bbox"])
        out.append(
            {
                **box,
                "norm": [x1 / width, y1 / height, x2 / width, y2 / height],
            }
        )
    return out


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


__all__ = [
    "from_yolo_lines",
    "label_bytes",
    "preview_boxes",
    "read_bytes",
    "to_yolo_lines",
    "write_label",
]

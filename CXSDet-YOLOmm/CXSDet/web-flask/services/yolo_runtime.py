"""Unified single-modal and RGB+X model runtime."""

from __future__ import annotations

import sys
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = BACKEND_ROOT / "vendor"
ULTRALYTICS_CONFIG_DIR = BACKEND_ROOT / ".ultralytics"
MATPLOTLIB_CONFIG_DIR = BACKEND_ROOT / ".matplotlib"


class ModelRuntimeError(RuntimeError):
    """Base error for model runtime failures."""


class MissingModalityError(ModelRuntimeError):
    """Raised when a required X modality cannot be derived from RGB."""


@dataclass(frozen=True)
class ResolvedInputs:
    rgb_path: str
    x_path: str | None
    x_origin: str | None
    model_mode: str
    secondary_modality: str | None


@dataclass
class DetectionOutput:
    detections: list[dict[str, Any]]
    raw_results: Any


def activate_vendored_ultralytics() -> str:
    """Make the project-owned Ultralytics fork authoritative for this process."""
    ULTRALYTICS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    MATPLOTLIB_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("YOLO_CONFIG_DIR", str(ULTRALYTICS_CONFIG_DIR))
    os.environ.setdefault("MPLCONFIGDIR", str(MATPLOTLIB_CONFIG_DIR))
    vendor_text = str(VENDOR_ROOT)
    if vendor_text in sys.path:
        sys.path.remove(vendor_text)
    sys.path.insert(0, vendor_text)

    loaded = sys.modules.get("ultralytics")
    loaded_path = str(getattr(loaded, "__file__", "")) if loaded else ""
    expected_root = str(VENDOR_ROOT / "ultralytics").lower()
    if loaded and not loaded_path.lower().startswith(expected_root):
        for name in tuple(sys.modules):
            if name == "ultralytics" or name.startswith("ultralytics."):
                sys.modules.pop(name, None)
    return vendor_text


def generate_edge_image(rgb_path: str, output_path: str) -> str:
    """Generate a 3-channel Canny edge image compatible with RGB+X models."""
    image = read_image(rgb_path)
    if image is None:
        raise ModelRuntimeError(f"无法读取RGB图片: {rgb_path}")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edge = cv2.Canny(gray, 80, 160)
    edge_three_channel = cv2.cvtColor(edge, cv2.COLOR_GRAY2BGR)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not write_image(destination, edge_three_channel):
        raise ModelRuntimeError(f"无法写入edge图片: {destination}")
    return str(destination)


def read_image(path):
    """Windows OpenCV imread 不支持部分中文路径；先由 Python 读取文件字节。"""
    try:
        return cv2.imdecode(np.frombuffer(Path(path).read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    except (OSError, cv2.error):
        return None


def write_image(path, image):
    ok, encoded = cv2.imencode(Path(path).suffix or '.png', image)
    if ok:
        Path(path).write_bytes(encoded.tobytes())
    return bool(ok)


def resolve_inference_inputs(
    model_meta: dict,
    rgb_path: str,
    x_path: str | None,
    work_dir: str,
) -> ResolvedInputs:
    """Resolve required model inputs without silently fabricating sensor data."""
    if not Path(rgb_path).is_file():
        raise ModelRuntimeError(f"RGB图片不存在: {rgb_path}")

    mode = str(model_meta.get("model_mode") or "single").lower()
    if mode != "multimodal":
        return ResolvedInputs(rgb_path, None, None, "single", None)

    secondary = str(model_meta.get("secondary_modality") or "").strip().lower()
    if not secondary:
        raise ModelRuntimeError("多模态模型缺少secondary_modality配置")
    if x_path:
        if not Path(x_path).is_file():
            raise MissingModalityError(f"{secondary}模态图片不存在: {x_path}")
        return ResolvedInputs(rgb_path, x_path, "uploaded", mode, secondary)
    if secondary == "edge":
        generated = Path(work_dir) / f"{Path(rgb_path).stem}_edge.png"
        return ResolvedInputs(
            rgb_path,
            generate_edge_image(rgb_path, str(generated)),
            "generated",
            mode,
            secondary,
        )
    raise MissingModalityError(f"{secondary}模态必须上传对应X图片，系统不会使用零填充")


def load_detector(weight_path: str, model_mode: str, model_yaml: str | None = None):
    """Load a standard YOLO or the project-owned YOLOMM implementation."""
    activate_vendored_ultralytics()
    import ultralytics

    target = weight_path or model_yaml
    if not target:
        raise ModelRuntimeError("缺少模型权重或模型YAML")
    if str(model_mode).lower() == "multimodal":
        if getattr(ultralytics, "YOLOMM", None) is None:
            raise ModelRuntimeError("当前vendored Ultralytics不包含YOLOMM")
        return ultralytics.YOLOMM(target)
    return ultralytics.YOLO(target)


def _serialize_results(results: Any) -> list[dict[str, Any]]:
    detections: list[dict[str, Any]] = []
    for result in results or []:
        names = getattr(result, "names", {}) or {}
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            continue
        if hasattr(boxes, "xyxy"):
            xyxy = boxes.xyxy.detach().cpu().tolist()
            confidences = boxes.conf.detach().cpu().tolist()
            classes = boxes.cls.detach().cpu().tolist()
        else:
            # YOLOMM's MultiModalResults uses an Nx6 ndarray:
            # x1, y1, x2, y2, confidence, class_id.
            rows = boxes.tolist() if hasattr(boxes, "tolist") else list(boxes)
            valid_rows = [row for row in rows if len(row) >= 6]
            xyxy = [row[:4] for row in valid_rows]
            confidences = [row[4] for row in valid_rows]
            classes = [row[5] for row in valid_rows]
        for coords, confidence, class_id in zip(xyxy, confidences, classes):
            numeric_class = int(class_id)
            detections.append(
                {
                    "class_id": numeric_class,
                    "class_name": names.get(numeric_class, str(numeric_class)),
                    "confidence": float(confidence),
                    "bbox": [float(value) for value in coords],
                }
            )
    return detections


def predict_image(detector, inputs: ResolvedInputs, confidence: float) -> DetectionOutput:
    """Run inference and normalize results for storage/API responses."""
    if inputs.model_mode == "multimodal":
        results = detector.predict(
            rgb_source=inputs.rgb_path,
            x_source=inputs.x_path,
            conf=confidence,
            save=False,
            verbose=False,
        )
    else:
        results = detector.predict(
            source=inputs.rgb_path,
            conf=confidence,
            save=False,
            verbose=False,
        )
    return DetectionOutput(_serialize_results(results), results)

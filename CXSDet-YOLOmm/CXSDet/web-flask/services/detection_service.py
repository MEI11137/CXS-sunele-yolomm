"""Atomic image detection workflow with automatic record persistence."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import cv2

from services.yolo_runtime import (
    DetectionOutput,
    load_detector,
    predict_image,
    resolve_inference_inputs,
)


@dataclass(frozen=True)
class DetectionRecordResult:
    record_id: str
    original_image_url: str
    result_image_url: str
    secondary_image_url: str | None
    secondary_image_origin: str | None
    detections: list[dict]
    detection_count: int
    model_id: str
    model_name: str
    confidence_threshold: float
    storage: dict


def _draw_detections(rgb_path: str, detections: list[dict], output_path: str) -> str:
    image = cv2.imread(rgb_path, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"无法读取待绘制图片: {rgb_path}")
    for detection in detections:
        x1, y1, x2, y2 = [int(round(value)) for value in detection["bbox"]]
        class_name = str(detection.get("class_name", "object"))
        confidence = float(detection.get("confidence", 0.0))
        color = (0, 0, 255)
        cv2.rectangle(image, (x1, y1), (x2, y2), color, 2)
        cv2.putText(
            image,
            f"{class_name} {confidence:.2f}",
            (x1, max(18, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            color,
            2,
        )
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(destination), image):
        raise RuntimeError(f"无法写入检测结果图: {destination}")
    return str(destination)


def execute_detection(
    user_id: int,
    model_meta: dict,
    rgb_path: str,
    x_path: str | None,
    confidence: float,
    work_dir: str,
    storage_client,
    database,
    detector_loader: Callable = load_detector,
    predictor: Callable = predict_image,
    request_key: str | None = None,
) -> DetectionRecordResult:
    """Persist inputs, inference output and a record as one compensating workflow."""
    workspace = Path(work_dir)
    workspace.mkdir(parents=True, exist_ok=True)
    resolved = resolve_inference_inputs(model_meta, rgb_path, x_path, str(workspace))
    uploaded: list[dict] = []
    try:
        original = storage_client.upload("detection_original", resolved.rgb_path)
        uploaded.append(original)

        secondary = None
        if resolved.x_path:
            secondary = storage_client.upload("detection_secondary", resolved.x_path)
            uploaded.append(secondary)

        if not model_meta.get("model_bucket") or not model_meta.get("model_object_key"):
            raise RuntimeError("模型文件不存在")
        model_bytes = storage_client.get(
            model_meta["model_bucket"], model_meta["model_object_key"]
        )
        model_path = workspace / "model.pt"
        model_path.write_bytes(model_bytes)
        detector = detector_loader(
            str(model_path),
            model_meta.get("model_mode") or "single",
            model_meta.get("model_yaml"),
        )
        output: DetectionOutput = predictor(detector, resolved, confidence)
        detections = []
        for item in output.detections:
            normalized = dict(item)
            class_id = int(normalized.get("class_id", normalized.get("class", 0)))
            normalized["class_id"] = class_id
            normalized["class"] = class_id
            normalized["confidence"] = float(normalized.get("confidence", 0.0))
            normalized["bbox"] = [float(value) for value in normalized.get("bbox", [])]
            detections.append(normalized)

        result_path = _draw_detections(
            resolved.rgb_path, detections, str(workspace / "result.jpg")
        )
        result_upload = storage_client.upload("detection_results", result_path)
        uploaded.append(result_upload)

        record_id = str(uuid.uuid4())
        created_at = datetime.now().isoformat()
        affected = database.execute(
            """
            INSERT INTO detection_records (
                id, user_id, model_id, model_name,
                original_image_bucket, original_image_object_key,
                secondary_image_bucket, secondary_image_object_key,
                secondary_image_origin,
                result_image_bucket, result_image_object_key,
                detection_result, confidence_threshold, create_time,
                title, description, record_status, request_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                user_id,
                model_meta["id"],
                model_meta["name"],
                original["bucket"],
                original["objectKey"],
                secondary["bucket"] if secondary else None,
                secondary["objectKey"] if secondary else None,
                resolved.x_origin,
                result_upload["bucket"],
                result_upload["objectKey"],
                json.dumps(detections, ensure_ascii=False),
                confidence,
                created_at,
                f"自动检测 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                "检测成功后由系统自动保存",
                "completed",
                request_key,
            ),
        )
        if affected <= 0:
            raise RuntimeError("识别记录写入失败")

        secondary_url = (
            storage_client.get_file_url(secondary["bucket"], secondary["objectKey"])
            if secondary
            else None
        )
        return DetectionRecordResult(
            record_id=record_id,
            original_image_url=storage_client.get_file_url(
                original["bucket"], original["objectKey"]
            ),
            result_image_url=storage_client.get_file_url(
                result_upload["bucket"], result_upload["objectKey"]
            ),
            secondary_image_url=secondary_url,
            secondary_image_origin=resolved.x_origin,
            detections=detections,
            detection_count=len(detections),
            model_id=model_meta["id"],
            model_name=model_meta["name"],
            confidence_threshold=confidence,
            storage={
                "original": original,
                "secondary": secondary,
                "result": result_upload,
            },
        )
    except Exception:
        for item in reversed(uploaded):
            try:
                storage_client.delete(item["bucket"], item["objectKey"])
            except Exception:
                pass
        raise

"""Extract, pair, auto-label, and persist video samples into a draft dataset."""

from __future__ import annotations

import math
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2


def _as_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class VideoDatasetConfig:
    enabled: bool = False
    name: str = ""
    description: str = ""
    mode: str = "single"
    frame_interval: int = 1
    annotation_model_id: str = ""
    confidence: float = 0.5
    secondary_modality: str | None = None
    secondary_time_offset: float = 0.0

    @classmethod
    def from_form(cls, form) -> "VideoDatasetConfig":
        enabled = _as_bool(form.get("generate_dataset"))
        if not enabled:
            return cls()
        name = str(form.get("dataset_name") or "").strip()
        mode = str(form.get("dataset_mode") or "single").strip().lower()
        modality = str(form.get("secondary_modality") or "").strip().lower() or None
        try:
            interval = int(form.get("frame_interval", 1))
            confidence = float(form.get("annotation_confidence", 0.5))
            offset = float(form.get("secondary_time_offset", 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("抽帧间隔、置信度或时间偏移格式无效") from exc
        if not name:
            raise ValueError("请输入待划分数据集名称")
        if mode not in {"single", "multimodal"}:
            raise ValueError("数据集模式无效")
        if interval < 1:
            raise ValueError("抽帧间隔必须至少为1")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("标注置信度必须在0到1之间")
        if not math.isfinite(offset):
            raise ValueError("时间偏移必须是有效数字")
        if mode == "multimodal" and not modality:
            raise ValueError("多模态数据集必须选择第二模态")
        if mode == "single":
            modality = None
            offset = 0.0
        return cls(
            enabled=True,
            name=name,
            description=str(form.get("dataset_description") or "").strip(),
            mode=mode,
            frame_interval=interval,
            annotation_model_id=str(form.get("annotation_model_id") or "").strip(),
            confidence=confidence,
            secondary_modality=modality,
            secondary_time_offset=offset,
        )


def selected_frame_indices(total_frames: int, interval: int) -> range:
    try:
        count, step = int(total_frames), int(interval)
    except (TypeError, ValueError) as exc:
        raise ValueError("总帧数和抽帧间隔必须是整数") from exc
    if count < 0 or step < 1:
        raise ValueError("总帧数不能为负，抽帧间隔必须至少为1")
    return range(0, count, step)


def nearest_secondary_frame(
    primary_index: int,
    primary_fps: float,
    secondary_fps: float,
    offset_seconds: float,
) -> int:
    try:
        index, p_fps, s_fps, offset = (
            int(primary_index), float(primary_fps), float(secondary_fps), float(offset_seconds)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("帧号、帧率或时间偏移无效") from exc
    if index < 0 or not math.isfinite(p_fps) or not math.isfinite(s_fps) or p_fps <= 0 or s_fps <= 0:
        raise ValueError("帧号不能为负，视频帧率必须为正数")
    if not math.isfinite(offset):
        raise ValueError("时间偏移必须是有效数字")
    return max(0, int(round((index / p_fps + offset) * s_fps)))


def read_nearby_frame(capture, frame_index: int, total_frames: int, radius: int = 2):
    """Try the timestamp-matched frame, then nearby frames, without fabricating data."""
    candidates = [frame_index]
    for distance in range(1, radius + 1):
        candidates.extend((frame_index - distance, frame_index + distance))
    seen = set()
    for candidate in candidates:
        if candidate in seen or candidate < 0 or candidate >= total_frames:
            continue
        seen.add(candidate)
        capture.set(cv2.CAP_PROP_POS_FRAMES, candidate)
        ok, frame = capture.read()
        if ok and frame is not None:
            return frame, candidate
    return None, None


def _class_names(detector) -> list[str]:
    names = getattr(detector, "names", {}) or {}
    if isinstance(names, dict):
        return [str(names[key]) for key in sorted(names, key=lambda item: int(item))]
    return [str(name) for name in names]


def _normalize_annotations(detections, width: int, height: int) -> list[dict]:
    normalized = []
    for detection in detections:
        x1, y1, x2, y2 = [float(value) for value in detection["bbox"]]
        normalized.append(
            {
                "class_id": int(detection.get("class_id", detection.get("class", 0))),
                "class_name": str(detection.get("class_name", "")),
                "confidence": float(detection.get("confidence", 0.0)),
                "bbox": [
                    max(0.0, min(1.0, (x1 + x2) / (2 * width))),
                    max(0.0, min(1.0, (y1 + y2) / (2 * height))),
                    max(0.0, min(1.0, (x2 - x1) / width)),
                    max(0.0, min(1.0, (y2 - y1) / height)),
                ],
            }
        )
    return normalized


def generate_video_dataset(draft_id, config, paths, database, storage_client) -> dict:
    """Write all extracted frames and labels to storage, preserving unpaired samples."""
    from services.dataset_draft_service import add_draft_sample, mark_draft_ready
    from services.yolo_runtime import load_detector, predict_image, resolve_inference_inputs

    config = config if isinstance(config, VideoDatasetConfig) else VideoDatasetConfig(**config)
    primary_path = str(paths["primary_video"])
    primary = cv2.VideoCapture(primary_path)
    if not primary.isOpened():
        raise RuntimeError("无法打开用于数据集抽帧的主视频")
    total_frames = int(primary.get(cv2.CAP_PROP_FRAME_COUNT))
    primary_fps = float(primary.get(cv2.CAP_PROP_FPS))
    if total_frames <= 0 or not math.isfinite(primary_fps) or primary_fps <= 0:
        primary.release()
        raise RuntimeError("主视频帧数或帧率无效")

    secondary_path = paths.get("secondary_video")
    secondary = cv2.VideoCapture(str(secondary_path)) if secondary_path else None
    secondary_total = int(secondary.get(cv2.CAP_PROP_FRAME_COUNT)) if secondary and secondary.isOpened() else 0
    secondary_fps = float(secondary.get(cv2.CAP_PROP_FPS)) if secondary_total else 0.0
    if config.mode == "multimodal" and (secondary is None or secondary_total <= 0 or secondary_fps <= 0):
        primary.release()
        if secondary:
            secondary.release()
        raise RuntimeError("多模态数据集需要可读取的第二路视频")

    workspace = Path(paths["work_dir"])
    workspace.mkdir(parents=True, exist_ok=True)
    model_meta = dict(paths["model_meta"])
    if model_meta.get("model_mode", "single") != config.mode:
        primary.release()
        if secondary:
            secondary.release()
        raise RuntimeError("标注模型与数据集模式不兼容")
    if config.mode == "multimodal" and str(model_meta.get("secondary_modality") or "").lower() != config.secondary_modality:
        primary.release()
        if secondary:
            secondary.release()
        raise RuntimeError("标注模型与第二模态不兼容")
    model_bytes = storage_client.get(model_meta["model_bucket"], model_meta["model_object_key"])
    model_path = workspace / "annotation_model.pt"
    model_path.write_bytes(model_bytes)
    detector = load_detector(str(model_path), config.mode, model_meta.get("model_yaml"))
    classes = _class_names(detector)
    if not classes:
        classes = list(model_meta.get("class_names") or [])
    conn = database.get_connection()
    try:
        row = conn.execute(
            "SELECT metadata_json FROM dataset_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        metadata = json.loads((row[0] if row else "{}") or "{}")
        metadata["class_names"] = classes
        conn.execute(
            "UPDATE dataset_drafts SET metadata_json = ?, update_time = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False), datetime.now().isoformat(), draft_id),
        )
        conn.commit()
    finally:
        conn.close()

    extracted = paired = missing = 0
    pairing_errors_ms: list[float] = []
    try:
        for frame_index in selected_frame_indices(total_frames, config.frame_interval):
            frame, actual_index = read_nearby_frame(primary, frame_index, total_frames, radius=0)
            if frame is None:
                raise RuntimeError(f"无法读取主视频第{frame_index}帧")
            height, width = frame.shape[:2]
            stem = f"frame_{frame_index:08d}"
            rgb_path = workspace / f"{stem}.jpg"
            if not cv2.imwrite(str(rgb_path), frame):
                raise RuntimeError(f"无法保存抽取帧 {frame_index}")

            secondary_upload = None
            secondary_index = secondary_timestamp = pairing_error_ms = None
            pairing_state = "not_required"
            x_path = None
            if config.mode == "multimodal":
                expected = nearest_secondary_frame(
                    frame_index, primary_fps, secondary_fps, config.secondary_time_offset
                )
                x_frame, secondary_index = read_nearby_frame(secondary, expected, secondary_total)
                pairing_state = "missing"
                if x_frame is not None:
                    x_local = workspace / f"{stem}_{config.secondary_modality}.jpg"
                    if cv2.imwrite(str(x_local), x_frame):
                        x_path = str(x_local)
                        secondary_upload = storage_client.upload("dataset_images", x_path)
                        secondary_timestamp = secondary_index / secondary_fps
                        pairing_error_ms = abs(
                            (secondary_timestamp - config.secondary_time_offset - actual_index / primary_fps)
                            * 1000
                        )
                        pairing_state = "paired"
                        paired += 1
                        pairing_errors_ms.append(pairing_error_ms)
                        secondary_index = int(secondary_index)
                if pairing_state == "missing":
                    missing += 1

            if config.mode == "multimodal" and x_path is None:
                # Keep the RGB frame and an empty label for manual repair/exclusion;
                # never infer by inventing a second-modality image.
                annotations = []
            else:
                resolved = resolve_inference_inputs(model_meta, str(rgb_path), x_path, str(workspace))
                output = predict_image(detector, resolved, config.confidence)
                annotations = _normalize_annotations(output.detections, width, height)
            rgb_upload = storage_client.upload("dataset_images", str(rgb_path))
            label_path = workspace / f"{stem}.txt"
            with label_path.open("w", encoding="utf-8", newline="\n") as label_file:
                for annotation in annotations:
                    x, y, box_w, box_h = annotation["bbox"]
                    label_file.write(f"{annotation['class_id']} {x:.8f} {y:.8f} {box_w:.8f} {box_h:.8f}\n")
            label_upload = storage_client.upload("dataset_labels", str(label_path))
            add_draft_sample(
                draft_id,
                {
                    "sample_key": stem,
                    "rgb_bucket": rgb_upload["bucket"],
                    "rgb_object_key": rgb_upload["objectKey"],
                    "secondary_image_bucket": secondary_upload["bucket"] if secondary_upload else None,
                    "secondary_image_object_key": secondary_upload["objectKey"] if secondary_upload else None,
                    "label_bucket": label_upload["bucket"],
                    "label_object_key": label_upload["objectKey"],
                    "annotations": annotations,
                    "frame_index": actual_index,
                    "primary_timestamp": actual_index / primary_fps,
                    "secondary_frame_index": secondary_index,
                    "secondary_timestamp": secondary_timestamp,
                    "pairing_error_ms": pairing_error_ms,
                    "pairing_state": pairing_state,
                },
                database,
            )
            extracted += 1
    finally:
        primary.release()
        if secondary:
            secondary.release()

    mark_draft_ready(draft_id, database)
    return {
        "extracted": extracted,
        "paired": paired,
        "missing_secondary": missing,
        "max_pairing_error_ms": max(pairing_errors_ms) if pairing_errors_ms else None,
        "class_names": classes,
    }

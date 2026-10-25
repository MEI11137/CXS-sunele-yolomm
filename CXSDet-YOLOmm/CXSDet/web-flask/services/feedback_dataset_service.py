"""Create draft datasets from human-confirmed image detection feedback."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.dataset_classes import dataset_class_names
from services.review_service import ReviewValidationError, _validate_annotations
from services.dataset_draft_service import (
    DraftDatasetResult,
    add_draft_sample,
    create_draft,
    mark_draft_failed,
    mark_draft_ready,
)


class FeedbackValidationError(ValueError):
    pass


@dataclass(frozen=True)
class FeedbackSampleResult:
    sample_id: str
    record_id: str
    dataset_id: str
    annotation_hash: str
    created: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _canonical_annotations(value: str | None) -> tuple[str, list[dict[str, Any]]]:
    try:
        annotations = json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise FeedbackValidationError("人工标注无法解析") from exc
    if not isinstance(annotations, list):
        raise FeedbackValidationError("人工标注格式错误")
    canonical = json.dumps(annotations, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return canonical, annotations


def add_confirmed_record(record_id: str, target_dataset_id: str, database) -> FeedbackSampleResult:
    conn = database.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        dataset = conn.execute("SELECT id, structure_info FROM datasets WHERE id = ?", (target_dataset_id,)).fetchone()
        if dataset is None:
            raise FeedbackValidationError("目标反馈数据集不存在")
        record = conn.execute(
            """
            SELECT id, human_review_status, corrected_annotations
            FROM detection_records WHERE id = ?
            """,
            (record_id,),
        ).fetchone()
        if record is None:
            raise FeedbackValidationError("识别记录不存在")
        if record["human_review_status"] != "confirmed":
            raise FeedbackValidationError("只有人工确认的记录才能加入反馈数据集")
        canonical, annotations = _canonical_annotations(record["corrected_annotations"])
        try:
            _validate_annotations(annotations, dataset_class_names(dataset["structure_info"]))
        except ReviewValidationError as exc:
            raise FeedbackValidationError(f"人工标注与目标数据集类别不一致，请重新人工复核：{exc}") from exc
        annotation_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        existing = conn.execute(
            "SELECT id FROM feedback_samples WHERE record_id = ? AND annotation_hash = ?",
            (record_id, annotation_hash),
        ).fetchone()
        if existing:
            conn.commit()
            return FeedbackSampleResult(
                existing["id"], record_id, target_dataset_id, annotation_hash, False
            )
        sample_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO feedback_samples
                (id, record_id, dataset_id, annotation_hash, export_status, create_time)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (
                sample_id,
                record_id,
                target_dataset_id,
                annotation_hash,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.execute(
            "UPDATE detection_records SET feedback_dataset_id = ? WHERE id = ?",
            (target_dataset_id, record_id),
        )
        conn.commit()
        return FeedbackSampleResult(sample_id, record_id, target_dataset_id, annotation_hash, True)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _yolo_label(annotations: list[dict[str, Any]]) -> str:
    lines = []
    for item in annotations:
        x1, y1, x2, y2 = [float(value) for value in item["bbox"]]
        lines.append(
            f"{int(item['class_id'])} {(x1 + x2) / 2:.6f} {(y1 + y2) / 2:.6f} "
            f"{x2 - x1:.6f} {y2 - y1:.6f}"
        )
    return "\n".join(lines) + ("\n" if lines else "")


def create_feedback_dataset_draft(
    dataset_id: str,
    work_dir,
    creator_id: int,
    database,
    storage_client,
) -> DraftDatasetResult:
    """Reuse confirmed image objects and store their reviewed YOLO labels in a draft."""
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    conn = database.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        source = conn.execute(
            """
            SELECT id, name, dataset_mode, primary_modality, secondary_modality, structure_info
            FROM datasets WHERE id = ?
            """,
            (dataset_id,),
        ).fetchone()
        if source is None:
            raise FeedbackValidationError("源反馈数据集不存在")
        rows = conn.execute(
            """
            SELECT fs.id AS feedback_sample_id, dr.id AS record_id,
                   dr.human_review_status, dr.corrected_annotations,
                   dr.original_image_bucket, dr.original_image_object_key,
                   dr.secondary_image_bucket, dr.secondary_image_object_key,
                   dr.secondary_image_origin
            FROM feedback_samples fs
            JOIN detection_records dr ON dr.id = fs.record_id
            WHERE fs.dataset_id = ? AND dr.human_review_status = 'confirmed'
            ORDER BY dr.id, fs.id
            """,
            (dataset_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        raise FeedbackValidationError("反馈数据集中还没有已确认样本")
    class_names = dataset_class_names(source["structure_info"])
    for row in rows:
        _, annotations = _canonical_annotations(row["corrected_annotations"])
        try:
            _validate_annotations(annotations, class_names)
        except ReviewValidationError as exc:
            raise FeedbackValidationError(f"记录 {row['record_id']} 的标注与数据集类别不一致，请重新人工复核：{exc}") from exc

    mode = source["dataset_mode"] or "single"
    if mode not in {"single", "multimodal"}:
        raise FeedbackValidationError("源数据集类型无效")
    secondary = source["secondary_modality"] if mode == "multimodal" else None
    draft_id = str(uuid.uuid4())
    draft = create_draft(
        {
            "id": draft_id,
            "name": f"{source['name']}-feedback-{draft_id[:8]}",
            "description": f"由反馈数据集 {source['name']} 的人工确认样本生成，等待人工划分",
            "creator_id": creator_id,
            "source_type": "feedback",
            "source_dataset_id": dataset_id,
            "dataset_mode": mode,
            "primary_modality": source["primary_modality"] or "rgb",
            "secondary_modality": secondary,
            "metadata": {
                "class_names": class_names,
                "secondary_image_origins": {
                    row["record_id"]: row["secondary_image_origin"] for row in rows
                },
            },
        },
        database,
    )
    uploaded_labels = []
    try:
        for row in rows:
            if row["human_review_status"] != "confirmed":
                continue
            if not row["original_image_bucket"] or not row["original_image_object_key"]:
                raise FeedbackValidationError(f"记录 {row['record_id']} 缺少 RGB 图片")
            _, annotations = _canonical_annotations(row["corrected_annotations"])
            label_path = work_dir / f"{row['feedback_sample_id']}.txt"
            label_path.write_text(_yolo_label(annotations), encoding="utf-8")
            uploaded = storage_client.upload("feedback-labels", str(label_path))
            uploaded_labels.append(uploaded)
            secondary_present = bool(
                secondary
                and row["secondary_image_bucket"]
                and row["secondary_image_object_key"]
            )
            add_draft_sample(
                draft_id,
                {
                    "id": str(uuid.uuid4()),
                    "sample_key": row["record_id"],
                    "split": "unassigned",
                    "rgb_bucket": row["original_image_bucket"],
                    "rgb_object_key": row["original_image_object_key"],
                    "secondary_image_bucket": row["secondary_image_bucket"] if secondary_present else None,
                    "secondary_image_object_key": row["secondary_image_object_key"] if secondary_present else None,
                    "label_bucket": uploaded["bucket"],
                    "label_object_key": uploaded["objectKey"],
                    "annotations": annotations,
                    "pairing_state": "paired" if secondary_present else ("missing" if secondary else "not_required"),
                },
                database,
            )

        mark_draft_ready(draft_id, database)
        conn = database.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE feedback_samples SET export_status = 'draft' WHERE dataset_id = ?",
                (dataset_id,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return DraftDatasetResult(
            **{**draft.to_dict(), "workflow_status": "draft"}
        )
    except Exception:
        try:
            mark_draft_failed(draft_id, "反馈数据集草稿生成失败，请稍后重试", database)
        except Exception:
            pass
        for item in uploaded_labels:
            try:
                storage_client.delete(item["bucket"], item["objectKey"])
            except Exception:
                pass
        raise

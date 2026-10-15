"""Build traceable YOLO feedback datasets from human-confirmed records."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from config import YOLO_CLASSES_CONFIG


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


@dataclass(frozen=True)
class ExportResult:
    dataset_id: str
    destination: str
    sample_count: int
    dataset_mode: str
    secondary_modality: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FeedbackDatasetVersion:
    dataset_id: str
    parent_dataset_id: str
    name: str
    sample_count: int
    zip_bucket: str
    zip_object_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _canonical_annotations(value: str | None) -> tuple[str, list[dict[str, Any]]]:
    try:
        annotations = json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise FeedbackValidationError("人工标注无法解析") from exc
    canonical = json.dumps(annotations, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return canonical, annotations


def add_confirmed_record(record_id: str, target_dataset_id: str, database) -> FeedbackSampleResult:
    conn = database.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        dataset = conn.execute("SELECT id FROM datasets WHERE id = ?", (target_dataset_id,)).fetchone()
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
        canonical, _ = _canonical_annotations(record["corrected_annotations"])
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
        return FeedbackSampleResult(
            sample_id, record_id, target_dataset_id, annotation_hash, True
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _extension(object_key: str) -> str:
    suffix = Path(object_key).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"} else ".jpg"


def _yolo_label(annotations: list[dict[str, Any]]) -> str:
    lines = []
    for item in annotations:
        x1, y1, x2, y2 = [float(value) for value in item["bbox"]]
        lines.append(
            f"{int(item['class_id'])} {(x1 + x2) / 2:.6f} {(y1 + y2) / 2:.6f} "
            f"{x2 - x1:.6f} {y2 - y1:.6f}"
        )
    return "\n".join(lines) + ("\n" if lines else "")


def export_feedback_dataset(dataset_id: str, destination, database, storage_client) -> ExportResult:
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    conn = database.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        dataset = conn.execute(
            """
            SELECT id, dataset_mode, primary_modality, secondary_modality
            FROM datasets WHERE id = ?
            """,
            (dataset_id,),
        ).fetchone()
        if dataset is None:
            raise FeedbackValidationError("目标反馈数据集不存在")
        rows = conn.execute(
            """
            SELECT fs.id AS sample_id, dr.*
            FROM feedback_samples fs
            JOIN detection_records dr ON dr.id = fs.record_id
            WHERE fs.dataset_id = ? AND dr.human_review_status = 'confirmed'
            ORDER BY dr.id
            """,
            (dataset_id,),
        ).fetchall()
        mode = dataset["dataset_mode"] or "single"
        secondary = dataset["secondary_modality"] if mode == "multimodal" else None
        (destination / "images/train").mkdir(parents=True, exist_ok=True)
        (destination / "images/val").mkdir(parents=True, exist_ok=True)
        (destination / "labels/train").mkdir(parents=True, exist_ok=True)
        (destination / "labels/val").mkdir(parents=True, exist_ok=True)
        if secondary:
            (destination / f"images_{secondary}/train").mkdir(parents=True, exist_ok=True)
            (destination / f"images_{secondary}/val").mkdir(parents=True, exist_ok=True)
        manifest_samples = []
        exported_ids = []
        for row in rows:
            _, annotations = _canonical_annotations(row["corrected_annotations"])
            rgb_ext = _extension(row["original_image_object_key"])
            rgb_name = f"{row['id']}{rgb_ext}"
            (destination / "images/train" / rgb_name).write_bytes(
                storage_client.get(row["original_image_bucket"], row["original_image_object_key"])
            )
            (destination / "labels/train" / f"{row['id']}.txt").write_text(
                _yolo_label(annotations), encoding="utf-8"
            )
            sample = {"record_id": row["id"], "rgb": f"images/train/{rgb_name}"}
            if secondary:
                if not row["secondary_image_bucket"] or not row["secondary_image_object_key"]:
                    raise FeedbackValidationError(f"记录 {row['id']} 缺少 {secondary} 模态图片")
                x_ext = _extension(row["secondary_image_object_key"])
                x_name = f"{row['id']}{x_ext}"
                (destination / f"images_{secondary}/train" / x_name).write_bytes(
                    storage_client.get(row["secondary_image_bucket"], row["secondary_image_object_key"])
                )
                sample.update(
                    {
                        "secondary": f"images_{secondary}/train/{x_name}",
                        "secondary_modality": secondary,
                        "secondary_origin": row["secondary_image_origin"],
                    }
                )
            manifest_samples.append(sample)
            exported_ids.append(row["sample_id"])
        if rows:
            validation_row = rows[0]
            validation_rgb = f"{validation_row['id']}{_extension(validation_row['original_image_object_key'])}"
            (destination / "images/val" / validation_rgb).write_bytes(
                (destination / "images/train" / validation_rgb).read_bytes()
            )
            (destination / "labels/val" / f"{validation_row['id']}.txt").write_text(
                (destination / "labels/train" / f"{validation_row['id']}.txt").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            if secondary:
                validation_x = f"{validation_row['id']}{_extension(validation_row['secondary_image_object_key'])}"
                (destination / f"images_{secondary}/val" / validation_x).write_bytes(
                    (destination / f"images_{secondary}/train" / validation_x).read_bytes()
                )
        data = {
            "path": str(destination),
            "train": "./images/train",
            "val": "./images/val",
            "names": {index: name for index, name in enumerate(YOLO_CLASSES_CONFIG)},
        }
        if secondary:
            data.update(
                {
                    "modalities": {
                        "rgb": str(destination / "images"),
                        secondary: str(destination / f"images_{secondary}"),
                    },
                    "modality_used": ["rgb", secondary],
                }
            )
        (destination / "data.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        (destination / "manifest.json").write_text(
            json.dumps(
                {
                    "dataset_id": dataset_id,
                    "dataset_mode": mode,
                    "secondary_modality": secondary,
                    "samples": manifest_samples,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if exported_ids:
            conn.executemany(
                "UPDATE feedback_samples SET export_status = 'exported' WHERE id = ?",
                [(sample_id,) for sample_id in exported_ids],
            )
        conn.commit()
        return ExportResult(dataset_id, str(destination), len(rows), mode, secondary)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_feedback_dataset_version(
    dataset_id: str,
    work_dir,
    database,
    storage_client,
) -> FeedbackDatasetVersion:
    """Export, validate, zip, upload, and register a new immutable dataset version."""
    work_dir = Path(work_dir).resolve()
    export_dir = work_dir / "dataset"
    result = export_feedback_dataset(dataset_id, export_dir, database, storage_client)
    if result.sample_count == 0:
        raise FeedbackValidationError("反馈数据集中还没有已确认样本")

    conn = database.get_connection()
    conn.row_factory = sqlite3.Row
    source = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    conn.close()
    if source is None:
        raise FeedbackValidationError("源反馈数据集不存在")

    archive_path = work_dir / "feedback_dataset.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(export_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(export_dir).as_posix())

    from services.dataset_contract import validate_dataset_archive

    validation = validate_dataset_archive(str(archive_path), result.dataset_mode)
    uploaded = storage_client.upload("datasets", str(archive_path))
    new_id = str(uuid.uuid4())
    suffix = new_id[:8]
    name = f"{source['name']}-feedback-{suffix}"
    structure = validation.to_dict()
    conn = database.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO datasets
                (id, name, description, zip_bucket, zip_object_key,
                 structure_info, upload_time, file_size, status, dataset_mode,
                 primary_modality, secondary_modality, runtime_mapping,
                 parent_dataset_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                new_id,
                name,
                f"由反馈数据集 {source['name']} 人工确认样本生成",
                uploaded["bucket"],
                uploaded["objectKey"],
                json.dumps(structure, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
                archive_path.stat().st_size,
                result.dataset_mode,
                "rgb",
                result.secondary_modality,
                json.dumps(validation.runtime_mapping, ensure_ascii=False),
                dataset_id,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        try:
            storage_client.delete(uploaded["bucket"], uploaded["objectKey"])
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return FeedbackDatasetVersion(
        new_id,
        dataset_id,
        name,
        result.sample_count,
        uploaded["bucket"],
        uploaded["objectKey"],
    )

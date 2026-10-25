"""Build and register a trainable archive from a manually split draft."""

from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from services.dataset_contract import validate_dataset_archive
from services.dataset_draft_service import (
    DraftValidationError,
    validate_draft_for_finalization,
)
from utils.models import DATASET_STATUS


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FinalizedDatasetResult:
    dataset_id: str
    name: str
    description: str | None
    zip_bucket: str
    zip_object_key: str
    file_size: int
    sample_count: int
    dataset_mode: str
    secondary_modality: str | None
    structure_info: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _image_extension(object_key: str | None) -> str:
    extension = Path(str(object_key or "")).suffix.lower()
    return extension if extension in IMAGE_EXTENSIONS else ".jpg"


def _sample_basename(sample_key: str) -> str:
    basename = PurePosixPath(str(sample_key).replace("\\", "/")).name
    if not basename or basename in {".", ".."}:
        raise DraftValidationError("样本键无效，无法生成训练文件名")
    return basename


def _snapshot(rows) -> tuple[tuple[Any, ...], ...]:
    fields = (
        "id",
        "sample_key",
        "split",
        "rgb_bucket",
        "rgb_object_key",
        "secondary_image_bucket",
        "secondary_image_object_key",
        "label_bucket",
        "label_object_key",
        "pairing_state",
    )
    return tuple(tuple(row[field] for field in fields) for row in rows)


def _read_samples(database, draft_id: str):
    conn = database.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM dataset_draft_samples WHERE draft_id = ? ORDER BY id",
            (draft_id,),
        ).fetchall()
    finally:
        conn.close()


def _write_archive(
    draft: dict[str, Any], samples, class_names: list[str], destination: Path, storage_client
) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    draft_id = draft["id"]
    mode = draft["dataset_mode"]
    secondary = draft["secondary_modality"] if mode == "multimodal" else None
    metadata = draft["metadata"]
    seen = set()
    manifest_samples = []

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for sample in samples:
            split = sample["split"]
            if split == "excluded":
                continue
            basename = _sample_basename(sample["sample_key"])
            if basename in seen:
                raise DraftValidationError("样本键生成了重复的训练文件名")
            seen.add(basename)
            rgb_ext = _image_extension(sample["rgb_object_key"])
            rgb_name = f"{basename}{rgb_ext}"
            label_name = f"{basename}.txt"
            archive.writestr(
                f"images/{split}/{rgb_name}",
                storage_client.get(sample["rgb_bucket"], sample["rgb_object_key"]),
            )
            label_bytes = storage_client.get(sample["label_bucket"], sample["label_object_key"])
            archive.writestr(f"labels/{split}/{label_name}", label_bytes)
            sample_manifest = {
                "sample_id": sample["id"],
                "sample_key": sample["sample_key"],
                "split": split,
                "rgb": f"images/{split}/{rgb_name}",
                "label": f"labels/{split}/{label_name}",
                "frame_index": sample["frame_index"],
                "primary_timestamp": sample["primary_timestamp"],
                "pairing_state": sample["pairing_state"],
                "pairing_error_ms": sample["pairing_error_ms"],
            }
            if secondary:
                x_ext = _image_extension(sample["secondary_image_object_key"])
                x_name = f"{basename}{x_ext}"
                x_path = f"images_{secondary}/{split}/{x_name}"
                archive.writestr(
                    x_path,
                    storage_client.get(
                        sample["secondary_image_bucket"],
                        sample["secondary_image_object_key"],
                    ),
                )
                sample_manifest.update(
                    {
                        "secondary": x_path,
                        "secondary_frame_index": sample["secondary_frame_index"],
                        "secondary_timestamp": sample["secondary_timestamp"],
                    }
                )
            manifest_samples.append(sample_manifest)

        data = {
            "path": ".",
            "train": "./images/train",
            "val": "./images/val",
            "nc": len(class_names),
            "names": class_names,
        }
        if any(sample["split"] == "test" for sample in samples):
            data["test"] = "./images/test"
        if secondary:
            data["modalities"] = {
                "rgb": "./images",
                secondary: f"./images_{secondary}",
            }
            data["modality_used"] = ["rgb", secondary]
        archive.writestr(
            "data.yaml",
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        )
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "draft_id": draft_id,
                    "name": draft["name"],
                    "source_type": draft["source_type"],
                    "source_dataset_id": draft["source_dataset_id"],
                    "video_detection_record_id": draft["video_detection_record_id"],
                    "dataset_mode": mode,
                    "primary_modality": draft["primary_modality"],
                    "secondary_modality": secondary,
                    "metadata": metadata,
                    "samples": manifest_samples,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return destination.stat().st_size


def finalize_draft(draft_id, work_dir, database, storage_client) -> FinalizedDatasetResult:
    validation = validate_draft_for_finalization(draft_id, database)
    draft = validation["draft"]
    samples = _read_samples(database, draft_id)
    expected_snapshot = _snapshot(samples)
    validation = validate_draft_for_finalization(draft_id, database)
    if _snapshot(_read_samples(database, draft_id)) != expected_snapshot:
        raise DraftValidationError("样本划分或文件信息已变化，请重新执行定稿")
    draft = validation["draft"]
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f"dataset-{draft_id}-", suffix=".zip", dir=work_dir, delete=False
    ) as temporary_archive:
        archive_path = Path(temporary_archive.name)

    uploaded = None
    try:
        file_size = _write_archive(
            draft, samples, validation["class_names"], archive_path, storage_client
        )
        contract = validate_dataset_archive(
            str(archive_path),
            draft["dataset_mode"],
            secondary_modality=draft["secondary_modality"],
            class_names=validation["class_names"],
        )
        uploaded = storage_client.upload("datasets", str(archive_path))
        conn = database.get_connection()
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT id FROM datasets WHERE name = ? LIMIT 1", (draft["name"],)
            ).fetchone()
            other_draft = conn.execute(
                "SELECT id FROM dataset_drafts WHERE name = ? AND id != ? LIMIT 1",
                (draft["name"], draft_id),
            ).fetchone()
            if current or other_draft:
                raise DraftValidationError("数据集名称已存在")
            latest_samples = conn.execute(
                "SELECT * FROM dataset_draft_samples WHERE draft_id = ? ORDER BY id",
                (draft_id,),
            ).fetchall()
            if _snapshot(latest_samples) != expected_snapshot:
                raise DraftValidationError("样本划分或文件信息已变化，请重新执行定稿")
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO datasets (
                    id, name, description, zip_bucket, zip_object_key, structure_info,
                    upload_time, file_size, status, dataset_mode, primary_modality,
                    secondary_modality, runtime_mapping, parent_dataset_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    draft["name"],
                    draft["description"],
                    uploaded["bucket"],
                    uploaded["objectKey"],
                    json.dumps(contract.to_dict(), ensure_ascii=False),
                    now,
                    file_size,
                    DATASET_STATUS["APPROVED"],
                    draft["dataset_mode"],
                    contract.primary_modality,
                    contract.secondary_modality,
                    json.dumps(contract.runtime_mapping, ensure_ascii=False),
                    draft["source_dataset_id"],
                ),
            )
            cursor = conn.execute(
                """
                UPDATE dataset_drafts
                SET workflow_status = 'finalized', error_message = NULL,
                    update_time = ?, finalized_at = ?
                WHERE id = ? AND workflow_status = 'draft'
                """,
                (now, now, draft_id),
            )
            if cursor.rowcount != 1:
                raise DraftValidationError("数据集状态已变化，请刷新后重试")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return FinalizedDatasetResult(
            dataset_id=draft_id,
            name=draft["name"],
            description=draft["description"],
            zip_bucket=uploaded["bucket"],
            zip_object_key=uploaded["objectKey"],
            file_size=file_size,
            sample_count=validation["sample_count"],
            dataset_mode=draft["dataset_mode"],
            secondary_modality=contract.secondary_modality,
            structure_info=contract.to_dict(),
        )
    except Exception:
        if uploaded:
            try:
                storage_client.delete(uploaded["bucket"], uploaded["objectKey"])
            except Exception:
                pass
        raise
    finally:
        try:
            archive_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove temporary dataset archive %s", archive_path)

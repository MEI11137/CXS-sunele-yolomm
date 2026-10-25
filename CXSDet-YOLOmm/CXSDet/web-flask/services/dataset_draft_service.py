"""Domain objects and operations for datasets awaiting manual splitting."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


DRAFT_STATES = {"generating", "draft", "failed", "finalized"}
SAMPLE_SPLITS = {"unassigned", "train", "val", "test", "excluded"}
PAIRING_STATES = {"not_required", "paired", "missing"}


class DraftValidationError(ValueError):
    """A draft cannot be created, changed, or finalized in its current state."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_value(row, name: str, default=None):
    if isinstance(row, sqlite3.Row):
        return row[name] if name in row.keys() else default
    if isinstance(row, dict):
        return row.get(name, default)
    return default


def ensure_dataset_name_available(name, database, exclude_draft_id=None) -> None:
    normalized = str(name or "").strip()
    if not normalized:
        raise DraftValidationError("数据集名称不能为空")
    conn = database.get_connection()
    try:
        formal = conn.execute(
            "SELECT id FROM datasets WHERE TRIM(name) = ? LIMIT 1", (normalized,)
        ).fetchone()
        if formal:
            raise DraftValidationError("数据集名称已存在")
        if exclude_draft_id:
            draft = conn.execute(
                "SELECT id FROM dataset_drafts WHERE TRIM(name) = ? AND id != ? LIMIT 1",
                (normalized, exclude_draft_id),
            ).fetchone()
        else:
            draft = conn.execute(
                "SELECT id FROM dataset_drafts WHERE TRIM(name) = ? LIMIT 1",
                (normalized,),
            ).fetchone()
        if draft:
            raise DraftValidationError("数据集名称已存在")
    finally:
        conn.close()


def _draft_result(row) -> DraftDatasetResult:
    try:
        metadata = json.loads(_row_value(row, "metadata_json", "{}") or "{}")
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    return DraftDatasetResult(
        id=_row_value(row, "id"),
        name=_row_value(row, "name"),
        description=_row_value(row, "description"),
        creator_id=_row_value(row, "creator_id"),
        source_type=_row_value(row, "source_type", "video"),
        source_dataset_id=_row_value(row, "source_dataset_id"),
        video_detection_record_id=_row_value(row, "video_detection_record_id"),
        dataset_mode=_row_value(row, "dataset_mode", "single"),
        primary_modality=_row_value(row, "primary_modality", "rgb"),
        secondary_modality=_row_value(row, "secondary_modality"),
        workflow_status=_row_value(row, "workflow_status", "generating"),
        metadata=metadata,
        error_message=_row_value(row, "error_message"),
        create_time=_row_value(row, "create_time"),
        update_time=_row_value(row, "update_time"),
    )


def _sample_result(row) -> DraftSampleResult:
    try:
        annotations = json.loads(_row_value(row, "annotations_json", "[]") or "[]")
    except (TypeError, json.JSONDecodeError):
        annotations = []
    return DraftSampleResult(
        id=_row_value(row, "id"),
        draft_id=_row_value(row, "draft_id"),
        sample_key=_row_value(row, "sample_key"),
        split=_row_value(row, "split", "unassigned"),
        pairing_state=_row_value(row, "pairing_state", "not_required"),
        frame_index=_row_value(row, "frame_index"),
        primary_timestamp=_row_value(row, "primary_timestamp"),
        secondary_frame_index=_row_value(row, "secondary_frame_index"),
        secondary_timestamp=_row_value(row, "secondary_timestamp"),
        pairing_error_ms=_row_value(row, "pairing_error_ms"),
        annotations=annotations,
        rgb_bucket=_row_value(row, "rgb_bucket"),
        rgb_object_key=_row_value(row, "rgb_object_key"),
        secondary_image_bucket=_row_value(row, "secondary_image_bucket"),
        secondary_image_object_key=_row_value(row, "secondary_image_object_key"),
        label_bucket=_row_value(row, "label_bucket"),
        label_object_key=_row_value(row, "label_object_key"),
        create_time=_row_value(row, "create_time"),
        update_time=_row_value(row, "update_time"),
    )


def create_draft(config, database) -> DraftDatasetResult:
    name = str(config.get("name") or "").strip()
    if not name:
        raise DraftValidationError("数据集名称不能为空")
    draft_id = str(config.get("id") or uuid.uuid4())
    now = _now()
    metadata = config.get("metadata") or {}
    try:
        metadata_json = json.dumps(metadata, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise DraftValidationError("数据集元信息格式错误") from exc

    conn = database.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        formal = conn.execute(
            "SELECT id FROM datasets WHERE TRIM(name) = ? LIMIT 1", (name,)
        ).fetchone()
        draft = conn.execute(
            "SELECT id FROM dataset_drafts WHERE TRIM(name) = ? LIMIT 1", (name,)
        ).fetchone()
        if formal or draft:
            raise DraftValidationError("数据集名称已存在")
        conn.execute(
            """
            INSERT INTO dataset_drafts (
                id, name, description, creator_id, source_type, source_dataset_id,
                video_detection_record_id, dataset_mode, primary_modality,
                secondary_modality, workflow_status, metadata_json, error_message,
                create_time, update_time, finalized_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'generating', ?, NULL, ?, ?, NULL)
            """,
            (
                draft_id,
                name,
                config.get("description"),
                config.get("creator_id"),
                config.get("source_type", "video"),
                config.get("source_dataset_id"),
                config.get("video_detection_record_id"),
                config.get("dataset_mode", "single"),
                config.get("primary_modality", "rgb"),
                config.get("secondary_modality"),
                metadata_json,
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM dataset_drafts WHERE id = ?", (draft_id,)).fetchone()
        conn.commit()
        return _draft_result(row)
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        if "uq_dataset_draft_name" in str(exc) or "dataset_drafts.name" in str(exc):
            raise DraftValidationError("数据集名称已存在") from exc
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_draft_sample(draft_id: str, sample, database) -> DraftSampleResult:
    conn = database.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        draft = conn.execute(
            "SELECT workflow_status FROM dataset_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if draft is None:
            raise DraftValidationError("待划分数据集不存在")
        if draft["workflow_status"] != "generating":
            raise DraftValidationError("当前数据集状态不允许添加样本")
        sample_id = str(sample.get("id") or uuid.uuid4())
        sample_key = str(sample.get("sample_key") or sample_id)
        annotations = sample.get("annotations") or []
        now = _now()
        conn.execute(
            """
            INSERT INTO dataset_draft_samples (
                id, draft_id, sample_key, split, rgb_bucket, rgb_object_key,
                secondary_image_bucket, secondary_image_object_key, label_bucket,
                label_object_key, annotations_json, frame_index, primary_timestamp,
                secondary_frame_index, secondary_timestamp, pairing_error_ms,
                pairing_state, create_time, update_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sample_id,
                draft_id,
                sample_key,
                sample.get("split", "unassigned"),
                sample.get("rgb_bucket"),
                sample.get("rgb_object_key"),
                sample.get("secondary_image_bucket"),
                sample.get("secondary_image_object_key"),
                sample.get("label_bucket"),
                sample.get("label_object_key"),
                json.dumps(annotations, ensure_ascii=False),
                sample.get("frame_index"),
                sample.get("primary_timestamp"),
                sample.get("secondary_frame_index"),
                sample.get("secondary_timestamp"),
                sample.get("pairing_error_ms"),
                sample.get("pairing_state", "not_required"),
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM dataset_draft_samples WHERE id = ?", (sample_id,)
        ).fetchone()
        conn.commit()
        return _sample_result(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_draft_ready(draft_id, database) -> None:
    """Finish generation so administrators may assign samples."""
    conn = database.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE dataset_drafts
            SET workflow_status = 'draft', error_message = NULL, update_time = ?
            WHERE id = ? AND workflow_status = 'generating'
            """,
            (_now(), draft_id),
        )
        if cursor.rowcount != 1:
            exists = conn.execute("SELECT id FROM dataset_drafts WHERE id = ?", (draft_id,)).fetchone()
            if not exists:
                raise DraftValidationError("待划分数据集不存在")
            raise DraftValidationError("数据集当前状态不能完成生成")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_draft(draft_id, database) -> dict[str, Any] | None:
    conn = database.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM dataset_drafts WHERE id = ?", (draft_id,)).fetchone()
        return _draft_result(row).to_dict() if row else None
    finally:
        conn.close()


def get_draft_sample(draft_id, sample_id, database) -> DraftSampleResult | None:
    conn = database.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM dataset_draft_samples WHERE draft_id = ? AND id = ?",
            (draft_id, sample_id),
        ).fetchone()
        return _sample_result(row) if row else None
    finally:
        conn.close()


def list_draft_samples(
    draft_id, database, *, page=1, page_size=50, split=None, pairing_state=None
) -> dict[str, Any]:
    page, page_size = max(1, int(page)), min(200, max(1, int(page_size)))
    conditions = ["draft_id = ?"]
    params: list[Any] = [draft_id]
    if split:
        if split not in SAMPLE_SPLITS:
            raise DraftValidationError("样本划分状态无效")
        conditions.append("split = ?")
        params.append(split)
    if pairing_state:
        if pairing_state not in PAIRING_STATES:
            raise DraftValidationError("模态配对状态无效")
        conditions.append("pairing_state = ?")
        params.append(pairing_state)
    where = " AND ".join(conditions)
    conn = database.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        total = conn.execute(
            f"SELECT COUNT(*) FROM dataset_draft_samples WHERE {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM dataset_draft_samples WHERE {where} ORDER BY frame_index, id LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        return {
            "samples": [_sample_result(row).to_dict() for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    finally:
        conn.close()


def repair_draft_sample_secondary(
    draft_id, sample_id, image_ref, database, *, frame_index=None, timestamp=None
) -> dict[str, Any]:
    conn = database.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        draft = conn.execute(
            "SELECT workflow_status, dataset_mode FROM dataset_drafts WHERE id = ?",
            (draft_id,),
        ).fetchone()
        if not draft:
            raise DraftValidationError("待划分数据集不存在")
        if draft["workflow_status"] != "draft" or draft["dataset_mode"] != "multimodal":
            raise DraftValidationError("当前数据集状态不支持修复第二模态")
        sample = conn.execute(
            "SELECT id FROM dataset_draft_samples WHERE draft_id = ? AND id = ?",
            (draft_id, sample_id),
        ).fetchone()
        if not sample:
            raise DraftValidationError("样本不存在或不属于该数据集")
        conn.execute(
            """UPDATE dataset_draft_samples
               SET secondary_image_bucket = ?, secondary_image_object_key = ?,
                   secondary_frame_index = COALESCE(?, secondary_frame_index),
                   secondary_timestamp = COALESCE(?, secondary_timestamp),
                   pairing_state = 'paired', pairing_error_ms = NULL, update_time = ?
               WHERE draft_id = ? AND id = ?""",
            (
                image_ref["bucket"], image_ref["objectKey"], frame_index, timestamp,
                _now(), draft_id, sample_id,
            ),
        )
        conn.commit()
        return {"updated": 1}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reset_failed_draft_for_retry(draft_id, database, storage_client) -> dict[str, Any]:
    """Reset only a failed draft and remove its persisted sample objects before retry."""
    conn = database.get_connection()
    objects = set()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        draft = conn.execute(
            "SELECT * FROM dataset_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if not draft:
            raise DraftValidationError("待划分数据集不存在")
        if draft["workflow_status"] != "failed":
            raise DraftValidationError("只有生成失败的数据集可以重试")
        if draft["source_type"] != "video" or not draft["video_detection_record_id"]:
            raise DraftValidationError("该数据集没有可重试的视频来源")
        rows = conn.execute(
            "SELECT * FROM dataset_draft_samples WHERE draft_id = ?", (draft_id,)
        ).fetchall()
        for row in rows:
            for bucket_field, key_field in (
                ("rgb_bucket", "rgb_object_key"),
                ("secondary_image_bucket", "secondary_image_object_key"),
                ("label_bucket", "label_object_key"),
            ):
                if row[bucket_field] and row[key_field]:
                    objects.add((row[bucket_field], row[key_field]))
        conn.execute("DELETE FROM dataset_draft_samples WHERE draft_id = ?", (draft_id,))
        conn.execute(
            """UPDATE dataset_drafts
               SET workflow_status = 'generating', error_message = NULL, update_time = ?
               WHERE id = ?""",
            (_now(), draft_id),
        )
        conn.commit()
        result = _draft_result(draft).to_dict()
        result["workflow_status"] = "generating"
        result["metadata"] = json.loads(draft["metadata_json"] or "{}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    for bucket, key in objects:
        try:
            storage_client.delete(bucket, key)
        except Exception:
            # A storage cleanup issue should not prevent regeneration; objects are
            # no longer referenced by the draft and can be garbage-collected later.
            pass
    return result


def assign_draft_samples(draft_id, sample_ids, split, database) -> dict[str, int]:
    ids = list(dict.fromkeys(str(sample_id) for sample_id in sample_ids or []))
    if split not in SAMPLE_SPLITS:
        raise DraftValidationError("样本划分状态无效")
    if not ids:
        raise DraftValidationError("请选择至少一个样本")
    conn = database.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        draft = conn.execute(
            "SELECT workflow_status FROM dataset_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if draft is None:
            raise DraftValidationError("待划分数据集不存在")
        if draft["workflow_status"] != "draft":
            raise DraftValidationError("数据集尚未生成完成或已不可修改")
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT id FROM dataset_draft_samples WHERE draft_id = ? AND id IN ({placeholders})",
            [draft_id, *ids],
        ).fetchall()
        if len(rows) != len(ids):
            raise DraftValidationError("所选样本包含其他数据集或不存在的样本")
        now = _now()
        conn.execute(
            f"UPDATE dataset_draft_samples SET split = ?, update_time = ? "
            f"WHERE draft_id = ? AND id IN ({placeholders})",
            [split, now, draft_id, *ids],
        )
        conn.commit()
        return {"updated": len(ids)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def validate_draft_for_finalization(draft_id, database) -> dict[str, Any]:
    conn = database.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        draft = conn.execute("SELECT * FROM dataset_drafts WHERE id = ?", (draft_id,)).fetchone()
        if draft is None:
            raise DraftValidationError("待划分数据集不存在")
        if draft["workflow_status"] != "draft":
            raise DraftValidationError("数据集尚未生成完成，不能标记为可训练")
        try:
            metadata = json.loads(draft["metadata_json"] or "{}")
        except json.JSONDecodeError as exc:
            raise DraftValidationError("数据集类别信息无法解析") from exc
        class_names = metadata.get("class_names") or []
        if isinstance(class_names, dict):
            class_names = [class_names[key] for key in sorted(class_names, key=lambda key: int(key))]
        rows = conn.execute(
            "SELECT * FROM dataset_draft_samples WHERE draft_id = ? ORDER BY id", (draft_id,)
        ).fetchall()
        counts = {split: 0 for split in SAMPLE_SPLITS}
        for row in rows:
            counts[row["split"]] = counts.get(row["split"], 0) + 1
        if counts["unassigned"]:
            raise DraftValidationError("仍有未分配样本")
        if not counts["train"]:
            raise DraftValidationError("训练集至少需要一个样本")
        if not counts["val"]:
            raise DraftValidationError("验证集至少需要一个样本")
        multimodal = draft["dataset_mode"] == "multimodal"
        for row in rows:
            if row["split"] == "excluded":
                continue
            if not row["rgb_bucket"] or not row["rgb_object_key"]:
                raise DraftValidationError(f"样本 {row['sample_key']} 缺少 RGB 图片")
            if not row["label_bucket"] or not row["label_object_key"]:
                raise DraftValidationError(f"样本 {row['sample_key']} 缺少 YOLO 标签文件")
            if multimodal and (
                not row["secondary_image_bucket"]
                or not row["secondary_image_object_key"]
                or row["pairing_state"] != "paired"
            ):
                modality = draft["secondary_modality"] or "第二模态"
                raise DraftValidationError(f"样本 {row['sample_key']} 缺少完整的 {modality} 配对")
            try:
                annotations = json.loads(row["annotations_json"] or "[]")
            except json.JSONDecodeError as exc:
                raise DraftValidationError(f"样本 {row['sample_key']} 的标注无法解析") from exc
            for annotation in annotations:
                class_id = annotation.get("class_id")
                if not isinstance(class_id, int) or class_id < 0 or class_id >= len(class_names):
                    raise DraftValidationError(f"样本 {row['sample_key']} 的类别 ID 超出类别映射范围")
        return {
            "draft": _draft_result(draft).to_dict(),
            "counts": counts,
            "class_names": class_names,
            "sample_count": sum(counts[split] for split in ("train", "val", "test")),
        }
    finally:
        conn.close()


def mark_draft_failed(draft_id, message, database) -> None:
    public_message = str(message or "数据集生成失败")[:500]
    conn = database.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE dataset_drafts
            SET workflow_status = 'failed', error_message = ?, update_time = ?
            WHERE id = ? AND workflow_status != 'finalized'
            """,
            (public_message, _now(), draft_id),
        )
        if cursor.rowcount == 0:
            exists = conn.execute("SELECT id FROM dataset_drafts WHERE id = ?", (draft_id,)).fetchone()
            if not exists:
                raise DraftValidationError("待划分数据集不存在")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@dataclass(frozen=True)
class DraftDatasetResult:
    id: str
    name: str
    description: str | None = None
    creator_id: int | None = None
    source_type: str = "video"
    source_dataset_id: str | None = None
    video_detection_record_id: str | None = None
    dataset_mode: str = "single"
    primary_modality: str = "rgb"
    secondary_modality: str | None = None
    workflow_status: str = "generating"
    metadata: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None
    create_time: str | None = None
    update_time: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DraftSampleResult:
    id: str
    draft_id: str
    sample_key: str
    split: str = "unassigned"
    pairing_state: str = "not_required"
    frame_index: int | None = None
    primary_timestamp: float | None = None
    secondary_frame_index: int | None = None
    secondary_timestamp: float | None = None
    pairing_error_ms: float | None = None
    annotations: list[dict[str, Any]] = field(default_factory=list)
    rgb_bucket: str | None = None
    rgb_object_key: str | None = None
    secondary_image_bucket: str | None = None
    secondary_image_object_key: str | None = None
    label_bucket: str | None = None
    label_object_key: str | None = None
    create_time: str | None = None
    update_time: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

"""Transactional human correction of detection results."""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from utils.dataset_classes import dataset_class_names


class ReviewValidationError(ValueError):
    pass


class ReviewConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReviewResult:
    record_id: str
    status: str
    decision: str
    previous_version: int
    new_version: int
    annotations: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validate_annotations(
    annotations: Any, class_names: list[str] | None
) -> list[dict[str, Any]]:
    """校验人工标注。``class_names=None`` 表示**没有真实类别表**（老库/极简库），
    此时只校验几何与结构，不拿一份默认类别表去猜。"""
    if not isinstance(annotations, list):
        raise ReviewValidationError("annotations 必须是数组")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(annotations):
        if not isinstance(item, dict):
            raise ReviewValidationError(f"第 {index + 1} 个标注格式错误")
        class_name = str(item.get("class_name") or "")
        class_id = item.get("class_id", item.get("class"))
        if class_names is not None:
            if class_name not in class_names:
                raise ReviewValidationError(f"未知类别: {class_name}")
            if not isinstance(class_id, int) or class_id != class_names.index(class_name):
                raise ReviewValidationError(f"类别编号与类别名称不匹配: {class_name}")
        bbox = item.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ReviewValidationError("bbox 必须是 [x1, y1, x2, y2]")
        try:
            coords = [float(value) for value in bbox]
        except (TypeError, ValueError) as exc:
            raise ReviewValidationError("bbox 坐标必须是数字") from exc
        if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in coords):
            raise ReviewValidationError("bbox 必须使用 0-1 归一化坐标且不能越界")
        x1, y1, x2, y2 = coords
        if x2 <= x1 or y2 <= y1:
            raise ReviewValidationError("bbox 右下角必须位于左上角右下方")
        normalized.append(
            {
                "class_name": class_name,
                "class_id": class_id,
                "bbox": coords,
            }
        )
    return normalized


def submit_human_review(
    record_id: str,
    reviewer_id: int,
    expected_version: int,
    annotations: list[dict[str, Any]],
    decision: str,
    database,
) -> ReviewResult:
    if decision not in {"confirmed", "no_defect", "rejected"}:
        raise ReviewValidationError("decision 必须是 confirmed、no_defect 或 rejected")
    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ReviewValidationError("expected_version 必须是整数") from exc

    conn = database.get_connection()
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()
    status = "confirmed" if decision in {"confirmed", "no_defect"} else "rejected"
    try:
        conn.execute("BEGIN IMMEDIATE")
        record = conn.execute(
            "SELECT * FROM detection_records WHERE id = ?", (record_id,)
        ).fetchone()
        if record is None:
            raise ReviewValidationError("识别记录不存在")
        source = None
        if "model_id" in record.keys() and record["model_id"]:
            source = conn.execute(
                "SELECT d.structure_info FROM models m "
                "LEFT JOIN datasets d ON d.id = m.dataset_id WHERE m.id = ?",
                (record["model_id"],),
            ).fetchone()
        clean = _validate_annotations(
            annotations, dataset_class_names(source["structure_info"] if source else None)
        )
        if decision == "confirmed" and not clean:
            raise ReviewValidationError("空标注请明确选择 no_defect")
        if decision == "no_defect" and clean:
            raise ReviewValidationError("no_defect 不能同时包含标注框")
        annotations_json = json.dumps(clean, ensure_ascii=False, sort_keys=True)
        current_version = int(record["review_version"] or 0)
        if current_version != expected_version:
            raise ReviewConflictError(
                f"记录已被其他人更新，当前版本为 {current_version}"
            )
        new_version = current_version + 1
        updated = conn.execute(
            """
            UPDATE detection_records
            SET human_review_status = ?, corrected_annotations = ?, reviewed_by = ?,
                reviewed_at = ?, review_version = ?
            WHERE id = ? AND review_version = ?
            """,
            (
                status,
                annotations_json,
                reviewer_id,
                now,
                new_version,
                record_id,
                current_version,
            ),
        )
        if updated.rowcount != 1:
            raise ReviewConflictError("记录版本冲突，请刷新后重试")
        conn.execute(
            """
            INSERT INTO detection_record_reviews
                (id, record_id, reviewer_id, decision, annotations,
                 previous_version, new_version, create_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                record_id,
                reviewer_id,
                decision,
                annotations_json,
                current_version,
                new_version,
                now,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return ReviewResult(
        record_id=record_id,
        status=status,
        decision=decision,
        previous_version=current_version,
        new_version=new_version,
        annotations=clean,
    )

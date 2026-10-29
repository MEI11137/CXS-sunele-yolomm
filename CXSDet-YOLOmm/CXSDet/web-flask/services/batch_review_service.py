"""Batch orchestration for AI review and reviewed-sample feedback."""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from typing import Any, Callable

from services.ai_review_service import AIReviewError
from services.feedback_dataset_service import FeedbackValidationError, add_confirmed_record


MAX_BATCH_SIZE = 50


class BatchValidationError(ValueError):
    """Raised when a batch request itself is invalid."""


def _normalize_record_ids(record_ids: Any) -> tuple[int, list[str]]:
    if not isinstance(record_ids, list) or not record_ids:
        raise BatchValidationError("record_ids 必须是非空数组")
    if len(record_ids) > MAX_BATCH_SIZE:
        raise BatchValidationError(f"最多批量处理 {MAX_BATCH_SIZE} 条记录")

    unique: list[str] = []
    seen: set[str] = set()
    for value in record_ids:
        record_id = str(value or "").strip()
        if not record_id:
            raise BatchValidationError("record_id 不能为空")
        if record_id not in seen:
            seen.add(record_id)
            unique.append(record_id)
    return len(record_ids), unique


def _summary(requested: int, record_ids: list[str], items: list[dict[str, Any]]) -> dict[str, Any]:
    succeeded = sum(item["status"] == "success" for item in items)
    return {
        "requested": requested,
        "unique": len(record_ids),
        "succeeded": succeeded,
        "failed": len(items) - succeeded,
        "items": items,
    }


def _load_accessible_record(record_id: str, user_id: int, is_admin_user: bool, database):
    conn = database.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        sql = 'SELECT * FROM detection_records WHERE id = ?'
        params = (record_id,)
        if not is_admin_user:
            sql += ' AND user_id = ?'; params += (user_id,)
        row = conn.execute(sql, params).fetchone()
        return None if row and 'record_status' in row.keys() and row['record_status'] == 'deleted' else row
    finally:
        conn.close()


def run_batch_ai_reviews(
    record_ids: Any,
    user_id: int,
    is_admin_user: bool,
    database,
    storage_client,
    review_runner: Callable,
    client_factory: Callable,
) -> dict[str, Any]:
    """Run paid/remote AI review once per unique accessible record."""

    requested, unique_ids = _normalize_record_ids(record_ids)
    items: list[dict[str, Any]] = []
    client = None
    for record_id in unique_ids:
        record = _load_accessible_record(record_id, user_id, is_admin_user, database)
        if record is None:
            items.append(
                {
                    "record_id": record_id,
                    "status": "failed",
                    "error": "识别记录不存在或无权访问",
                }
            )
            continue
        try:
            if client is None:
                client = client_factory()
            review = review_runner(record_id, client, database, storage_client)
            items.append(
                {"record_id": record_id, "status": "success", "result": review.to_dict()}
            )
        except AIReviewError as exc:
            items.append(
                {
                    "record_id": record_id,
                    "status": "failed",
                    "error": exc.public_message,
                }
            )
        except Exception as exc:
            items.append(
                {
                    "record_id": record_id,
                    "status": "failed",
                    "error": f"AI辅助复核失败: {type(exc).__name__}",
                }
            )
    return _summary(requested, unique_ids, items)


def _load_feedback_target(record_id: str, user_id: int, is_admin_user: bool, database):
    conn = database.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        access_sql = "" if is_admin_user else " AND dr.user_id = ?"
        params = (record_id,) if is_admin_user else (record_id, user_id)
        return conn.execute(
            f"""
            SELECT dr.id, m.dataset_id, d.name AS dataset_name
            FROM detection_records dr
            LEFT JOIN models m ON m.id = dr.model_id
            LEFT JOIN datasets d ON d.id = m.dataset_id
            WHERE dr.id = ?{access_sql}
            """,
            params,
        ).fetchone()
    finally:
        conn.close()


def add_batch_confirmed_records(
    record_ids: Any,
    user_id: int,
    is_admin_user: bool,
    database,
) -> dict[str, Any]:
    """Add confirmed records to their model's source dataset feedback pool."""

    requested, unique_ids = _normalize_record_ids(record_ids)
    items: list[dict[str, Any]] = []
    groups: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for record_id in unique_ids:
        target = _load_feedback_target(record_id, user_id, is_admin_user, database)
        if target is None:
            items.append(
                {
                    "record_id": record_id,
                    "status": "failed",
                    "error": "识别记录不存在或无权访问",
                }
            )
            continue
        if not target["dataset_id"] or not target["dataset_name"]:
            items.append(
                {
                    "record_id": record_id,
                    "status": "failed",
                    "error": "检测模型没有关联原训练数据集",
                }
            )
            continue
        try:
            result = add_confirmed_record(record_id, target["dataset_id"], database)
            item = {
                "record_id": record_id,
                "status": "success",
                "dataset_id": target["dataset_id"],
                "dataset_name": target["dataset_name"],
                "sample_id": result.sample_id,
                "created": result.created,
            }
            items.append(item)
            group = groups.setdefault(
                target["dataset_id"],
                {
                    "dataset_id": target["dataset_id"],
                    "dataset_name": target["dataset_name"],
                    "record_ids": [],
                },
            )
            group["record_ids"].append(record_id)
        except FeedbackValidationError as exc:
            items.append(
                {"record_id": record_id, "status": "failed", "error": str(exc)}
            )
        except Exception as exc:
            items.append(
                {
                    "record_id": record_id,
                    "status": "failed",
                    "error": f"加入反馈数据集失败: {type(exc).__name__}",
                }
            )

    result = _summary(requested, unique_ids, items)
    result["dataset_groups"] = list(groups.values())
    return result


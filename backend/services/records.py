"""识别记录的批量操作：软删除、恢复、加入反馈。

规则：

* 删除只写 ``deleted_at``，**文件与数据集一概不动**；不级联删源图、不删训练资源。
* 批量只作用于调用方显式给出的 ID；逐项返回结果，单项失败不影响其它项。
* 反馈只接受“本人已确认、且该版本尚未回流”的记录；版本变了就是另一条反馈。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from backend import security
from backend.errors import ApiError
from backend.services import detection as detection_service
from backend.services.review import effective_review


def _record_or_error(conn, user_id: int, project_id: str, record_id: str) -> sqlite3.Row | None:
    try:
        return detection_service.get_owned(conn, user_id, project_id, record_id)
    except ApiError:
        return None


def soft_delete(conn, user_id: int, project_id: str, record_ids: list[str]) -> dict:
    items = []
    for record_id in _unique(record_ids):
        row = _record_or_error(conn, user_id, project_id, record_id)
        if row is None:
            items.append({"record_id": record_id, "status": "missing", "error": "记录不存在或不属于当前项目"})
            continue
        if row["deleted_at"]:
            items.append({"record_id": record_id, "status": "unchanged", "deleted_at": row["deleted_at"]})
            continue
        now = security.now_iso()
        conn.execute(
            "UPDATE detections SET deleted_at = ?, deleted_by = ? WHERE id = ? AND project_id = ? AND owner_id = ?",
            (now, int(user_id), record_id, str(project_id), int(user_id)),
        )
        items.append({"record_id": record_id, "status": "deleted", "deleted_at": now})
    conn.commit()
    return {
        "items": items,
        "deleted": sum(1 for item in items if item["status"] == "deleted"),
        "note": "软删除：只标记记录不再出现在默认列表，原图、结果图与数据集文件都保留。",
    }


def restore(conn, user_id: int, project_id: str, record_ids: list[str]) -> dict:
    items = []
    for record_id in _unique(record_ids):
        row = _record_or_error(conn, user_id, project_id, record_id)
        if row is None:
            items.append({"record_id": record_id, "status": "missing", "error": "记录不存在或不属于当前项目"})
            continue
        if not row["deleted_at"]:
            items.append({"record_id": record_id, "status": "unchanged"})
            continue
        conn.execute(
            "UPDATE detections SET deleted_at = NULL, deleted_by = NULL WHERE id = ? AND project_id = ? AND owner_id = ?",
            (record_id, str(project_id), int(user_id)),
        )
        items.append({"record_id": record_id, "status": "restored"})
    conn.commit()
    return {"items": items, "restored": sum(1 for item in items if item["status"] == "restored")}


def add_feedback(
    conn, user_id: int, project_id: str, record_id: str, *, note: str = "", request_key: str = ""
) -> tuple[dict, bool]:
    key = str(request_key or "").strip()
    if key:
        existing = conn.execute(
            "SELECT * FROM record_feedback WHERE project_id = ? AND id = ?",
            (str(project_id), key),
        ).fetchone()
        if existing is not None:
            return _public_feedback(conn, existing), False
    row = _record_or_error(conn, user_id, project_id, record_id)
    if row is None:
        raise ApiError(404, "RECORD_NOT_FOUND", "记录不存在或不属于当前项目")
    if row["deleted_at"]:
        raise ApiError(409, "RECORD_DELETED", "已删除的记录不能加入反馈，请先恢复")
    version = effective_review(conn, str(record_id), int(user_id))
    if version is None:
        raise ApiError(409, "REVIEW_REQUIRED", "只有本人已确认的复核结论才能加入反馈")
    if version["status"] != "confirmed":
        raise ApiError(
            409,
            "REVIEW_NOT_CONFIRMED",
            "这条记录当前生效的人工结论不是“已确认”（可能已驳回或还在草稿），不能加入反馈；请先重新确认。",
            details={
                "latest_version": int(version["version"]),
                "latest_status": version["status"],
            },
        )
    existing = conn.execute(
        "SELECT * FROM record_feedback WHERE detection_id = ? AND review_version_id = ?",
        (str(record_id), str(version["id"])),
    ).fetchone()
    if existing is not None:
        if existing["status"] == "reflowed":
            raise ApiError(
                409,
                "ALREADY_REFLOVED",
                "这个复核版本的样本已经回流过，不能重复加入反馈",
                details={"feedback_id": existing["id"], "dataset_id": existing["dataset_id"]},
            )
        return _public_feedback(conn, existing), False
    feedback_id = key or security.new_id("fbk")
    try:
        conn.execute(
            "INSERT INTO record_feedback (id, project_id, owner_id, detection_id, review_version_id,"
            " status, note, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                feedback_id,
                str(project_id),
                int(user_id),
                str(record_id),
                str(version["id"]),
                str(note or "")[:500],
                security.now_iso(),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        existing = conn.execute(
            "SELECT * FROM record_feedback WHERE detection_id = ? AND review_version_id = ?",
            (str(record_id), str(version["id"])),
        ).fetchone()
        if existing is None:
            raise
        return _public_feedback(conn, existing), False
    row = conn.execute("SELECT * FROM record_feedback WHERE id = ?", (feedback_id,)).fetchone()
    return _public_feedback(conn, row), True


def list_feedback(conn, user_id: int, project_id: str, *, status: str = "pending", limit: int = 50) -> dict:
    clauses = ["project_id = ?", "owner_id = ?"]
    params: list[Any] = [str(project_id), int(user_id)]
    if status:
        clauses.append("status = ?")
        params.append(str(status))
    rows = conn.execute(
        f"SELECT * FROM record_feedback WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at DESC LIMIT ?",
        (*params, max(1, min(int(limit or 50), 200))),
    ).fetchall()
    return {"feedback": [_public_feedback(conn, row) for row in rows]}


def _public_feedback(conn, row) -> dict:
    data = dict(row)
    version = conn.execute(
        "SELECT version, status, outcome, boxes, image_width, image_height FROM review_versions "
        "WHERE id = ? AND owner_id = ?",
        (str(data["review_version_id"]), int(data["owner_id"])),
    ).fetchone()
    review = None
    if version is not None:
        boxes = json.loads(version["boxes"] or "[]")
        review = {
            "version": int(version["version"]),
            "outcome": version["outcome"],
            "box_count": len(boxes),
            "image": {"width": int(version["image_width"]), "height": int(version["image_height"])},
        }
    return {
        "id": data["id"],
        "detection_id": data["detection_id"],
        "review_version_id": data["review_version_id"],
        "dataset_id": data.get("dataset_id"),
        "status": data["status"],
        "note": data.get("note") or "",
        "created_at": data["created_at"],
        "reflowed_at": data.get("reflowed_at"),
        "review": review,
    }


def _unique(ids: list[str]) -> list[str]:
    seen: list[str] = []
    for item in ids or []:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


__all__ = ["add_feedback", "list_feedback", "restore", "soft_delete"]

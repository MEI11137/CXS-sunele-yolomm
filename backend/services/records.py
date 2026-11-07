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
    """反馈池查询。网页与 Agent 共用这一份口径。

    ``pending``（待整理）不是“表里 status='pending' 的行”，而是**当前仍然有效**的
    待办：记录没有软删，且这条反馈绑定的复核版本就是该记录最近一次已提交版本、
    状态为 confirmed。旧版本、后来被驳回的、记录已删的 pending 行仍留在表里做历史
    （``status=all`` 可查），但不再进待整理队列。

    过滤条件全部写在 SQL 的 LIMIT 之前：先截一页再在 Python 里过滤会漏项。
    只改读取口径，不删行、不改 status，所以记录恢复后原反馈照样能取回。
    """
    page = max(1, min(int(limit or 50), 200))
    params: list[Any] = [str(project_id), int(user_id)]
    scope = "f.project_id = ? AND f.owner_id = ?"
    wanted = str(status or "").strip()
    # ``all`` = 全量历史（含已过期/已删记录的行），不当作真实 status 值过滤。
    if wanted and wanted != "all":
        scope += " AND f.status = ?"
        params.append(wanted)
    if wanted == "pending":
        # 该记录当前生效（最近已提交）的复核版本 ID；与 review.effective_review 同序。
        effective = (
            "SELECT v.id FROM review_versions v"
            " WHERE v.detection_id = f.detection_id AND v.owner_id = f.owner_id"
            " AND v.status != 'draft' ORDER BY v.version DESC LIMIT 1"
        )
        rows = conn.execute(
            "SELECT f.* FROM record_feedback f"
            " JOIN detections d ON d.id = f.detection_id"
            " AND d.owner_id = f.owner_id AND d.project_id = f.project_id"
            " JOIN review_versions v ON v.id = f.review_version_id AND v.owner_id = f.owner_id"
            f" WHERE {scope} AND d.deleted_at IS NULL AND v.status = 'confirmed'"
            f" AND v.id = ({effective})"
            " ORDER BY f.created_at DESC LIMIT ?",
            (*params, page),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT f.* FROM record_feedback f WHERE {scope} "
            "ORDER BY f.created_at DESC LIMIT ?",
            (*params, page),
        ).fetchall()
    return {"feedback": [_public_feedback(conn, row) for row in rows]}


def _public_feedback(conn, row) -> dict:
    data = dict(row)
    version = conn.execute(
        "SELECT version, status, outcome, boxes, x_boxes, image_width, image_height,"
        " x_image_width, x_image_height, reference_modality, secondary_modality"
        " FROM review_versions WHERE id = ? AND owner_id = ?",
        (str(data["review_version_id"]), int(data["owner_id"])),
    ).fetchone()
    review = None
    if version is not None:
        boxes = json.loads(version["boxes"] or "[]")
        x_boxes = json.loads(version["x_boxes"] or "[]")
        review = {
            "version": int(version["version"]),
            "outcome": version["outcome"],
            # box_count 保持“参考模态框数”旧语义；X 侧单独给 x_box_count。
            "box_count": len(boxes),
            "x_box_count": len(x_boxes),
            "x_modality": version["secondary_modality"],
            "reference_modality": version["reference_modality"],
            "secondary_modality": version["secondary_modality"],
            "image": {"width": int(version["image_width"]), "height": int(version["image_height"])},
            "x_image": (
                {"width": int(version["x_image_width"]), "height": int(version["x_image_height"])}
                if version["x_image_width"] and version["x_image_height"]
                else None
            ),
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

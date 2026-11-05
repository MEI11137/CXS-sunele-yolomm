"""人工复核：草稿、不可变版本、驳回与重开。

三条底线：

1. **只有本人能在页面上提交**。Agent 可以读状态、解释 AI 建议、按用户口述改草稿，
   但没有“替用户确认”的工具；``submit`` 只允许从已登录用户自己的 HTTP 会话进入。
2. **版本是追加的**，草稿可以反复保存，提交/驳回各产生一条不可变版本；
   同一记录不会因为“再点一次”反复涨版本（request_key 幂等 + 已终态直接返回）。
3. **坐标是原图像素**，与检测记录同口径；转成 YOLO 归一化只在写标签文件时发生。
   页面缩放、滚动、拖拽都不改变真实坐标。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from backend import security
from backend.errors import ApiError
from backend.services import detection as detection_service
from backend.services import jobs as job_service
from backend.services import labels as label_service
from backend.services import models as model_service

STATUSES = {"draft", "confirmed", "rejected"}
OUTCOMES = {"defect", "no_defect"}
MAX_BOXES = 200
SEVERITIES = {"low", "medium", "high"}
DEFAULT_SEVERITY = "medium"


def public_version(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "detection_id": data["detection_id"],
        "version": int(data["version"]),
        "status": data["status"],
        "outcome": data.get("outcome"),
        "boxes": _loads(data.get("boxes"), []),
        "class_table": _loads(data.get("class_table"), []),
        "image_width": int(data.get("image_width") or 0),
        "image_height": int(data.get("image_height") or 0),
        "model_id": data.get("model_id"),
        "dataset_id": data.get("dataset_id"),
        "note": data.get("note") or "",
        "reject_reason": data.get("reject_reason"),
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "submitted_at": data.get("submitted_at"),
    }


def _loads(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (ValueError, TypeError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def class_options(conn, user_id: int, project_id: str, record) -> list[dict]:
    """复核下拉用的类别：**目标数据集口径**，不是权重自带名称。

    没有绑定数据集时才回落到模型自带类别，并保留 native 名称便于追溯。
    """
    model = conn.execute(
        "SELECT * FROM models WHERE id = ? AND project_id = ? AND owner_id = ?",
        (str(record["model_id"]), str(project_id), int(user_id)),
    ).fetchone()
    if model is None:
        raise ApiError(409, "MODEL_MISSING", "这条记录引用的模型已不存在")
    labels = model_service.labels_for(conn, model)
    native = {int(item["index"]): str(item["name"]) for item in _loads(model["classes"], [])}
    mapping = _loads(model["class_map"], {})
    reverse = {int(target): int(source) for source, target in mapping.items()}
    dataset_classes: list[str] = []
    if model["dataset_id"]:
        found = conn.execute(
            "SELECT classes FROM datasets WHERE id = ? AND project_id = ?",
            (str(model["dataset_id"]), str(project_id)),
        ).fetchone()
        if found is not None:
            dataset_classes = [str(item) for item in _loads(found["classes"], [])]
    options = []
    for index in sorted(labels):
        native_id = reverse.get(index)
        options.append(
            {
                "index": int(index),
                "name": labels[index],
                "native_class_id": native_id,
                "native_class_name": native.get(native_id) if native_id is not None else None,
                "source": "dataset" if 0 <= index < len(dataset_classes) else "model",
            }
        )
    return options


def _versions(conn, detection_id: str, *, owner_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM review_versions WHERE detection_id = ? AND owner_id = ? "
        "ORDER BY version ASC",
        (str(detection_id), int(owner_id)),
    ).fetchall()


def _latest_submitted(conn, detection_id: str, owner_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM review_versions WHERE detection_id = ? AND owner_id = ? "
        "AND status != 'draft' ORDER BY version DESC LIMIT 1",
        (str(detection_id), int(owner_id)),
    ).fetchone()


def _draft(conn, detection_id: str, owner_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM review_versions WHERE detection_id = ? AND owner_id = ? "
        "AND status = 'draft' LIMIT 1",
        (str(detection_id), int(owner_id)),
    ).fetchone()


def _latest_any(conn, detection_id: str, owner_id: int) -> sqlite3.Row | None:
    """最近一条版本，**草稿也算**。草稿的编辑基线要用它，不能用已提交版本。"""
    return conn.execute(
        "SELECT * FROM review_versions WHERE detection_id = ? AND owner_id = ? "
        "ORDER BY version DESC LIMIT 1",
        (str(detection_id), int(owner_id)),
    ).fetchone()


def effective_review(conn, detection_id: str, owner_id: int) -> sqlite3.Row | None:
    """当前**生效**的人工结论：最近一次已提交版本（草稿不算）。

    反馈、回流都必须以它为准。历史里可能还躺着更早的 confirmed，但用户后来
    驳回/重开了，那条旧确认已经失效，不能拿来当“已确认”。
    """
    return _latest_submitted(conn, detection_id, owner_id)


def state(conn, user_id: int, project_id: str, record_id: str) -> dict:
    record = detection_service.get_owned(conn, user_id, project_id, record_id)
    options = class_options(conn, user_id, project_id, record)
    versions = _versions(conn, record_id, owner_id=user_id)
    latest = versions[-1] if versions else None
    latest_submitted = _latest_submitted(conn, record_id, user_id)
    draft = next((row for row in versions if row["status"] == "draft"), None)
    confirmed = next(
        (row for row in reversed(versions) if row["status"] == "confirmed"), None
    )
    rejected = next((row for row in reversed(versions) if row["status"] == "rejected"), None)
    detection = detection_service.public_with_labels(conn, user_id, project_id, record)
    from backend.services import aireview

    ai_review = aireview.latest_for_records(conn, user_id, project_id, [str(record_id)]).get(str(record_id))
    return {
        "record": detection,
        "ai_review": ai_review,
        "human_status": (
            "confirmed" if latest_submitted and latest_submitted["status"] == "confirmed"
            else "rejected" if latest_submitted and latest_submitted["status"] == "rejected"
            else "draft" if draft else "pending"
        ),
        "class_options": options,
        "versions": [public_version(row) for row in versions],
        "draft": public_version(draft) if draft else None,
        "latest": public_version(latest) if latest else None,
        "latest_submitted": public_version(latest_submitted) if latest_submitted else None,
        "latest_confirmed": public_version(confirmed) if confirmed else None,
        "latest_rejected": public_version(rejected) if rejected else None,
        "pending": latest_submitted is None,
        "queue": _queue_name(latest_submitted),
        "image": {
            "width": _asset_size(conn, user_id, project_id, record)[0],
            "height": _asset_size(conn, user_id, project_id, record)[1],
            "rgb_url": detection["image_url"],
            "x_url": (
                f"/api/projects/{project_id}/assets/{record['x_asset_id']}/content"
                if record["x_asset_id"]
                else None
            ),
            "x_modality": _x_modality(conn, user_id, project_id, record),
            "result_url": detection["result_url"],
        },
    }


def _x_modality(conn, user_id: int, project_id: str, record) -> str | None:
    if not record["x_asset_id"]:
        return None
    row = conn.execute(
        "SELECT role, origin FROM assets WHERE id = ? AND project_id = ? AND owner_id = ?",
        (str(record["x_asset_id"]), str(project_id), int(user_id)),
    ).fetchone()
    if row is None:
        return None
    from backend.services import aireview

    return aireview._modality_label(row)


def _asset_size(conn, user_id: int, project_id: str, record) -> tuple[int, int]:
    row = conn.execute(
        "SELECT width, height FROM assets WHERE id = ? AND project_id = ? AND owner_id = ?",
        (str(record["rgb_asset_id"]), str(project_id), int(user_id)),
    ).fetchone()
    if row is None:
        return 0, 0
    return int(row["width"] or 0), int(row["height"] or 0)


def _queue_name(latest: sqlite3.Row | None) -> str:
    if latest is None:
        return "pending"
    if latest["status"] == "draft":
        return "draft"
    if latest["status"] == "rejected":
        return "rejected"
    return "confirmed"


def _validate_boxes(raw: Any, options: list[dict], width: int, height: int) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ApiError(400, "BAD_BOXES", "boxes 必须是数组")
    if len(raw) > MAX_BOXES:
        raise ApiError(400, "TOO_MANY_BOXES", f"一张图最多 {MAX_BOXES} 个框")
    allowed = {int(item["index"]): str(item["name"]) for item in options}
    clean: list[dict] = []
    for number, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ApiError(400, "BAD_BOXES", f"第 {number} 个框不是对象")
        try:
            class_id = int(item.get("class_id"))
        except (TypeError, ValueError):
            raise ApiError(400, "BAD_CLASS", f"第 {number} 个框缺少类别") from None
        if class_id not in allowed:
            raise ApiError(
                400,
                "BAD_CLASS",
                f"第 {number} 个框的类别 {class_id} 不在当前类别表里",
                details={"allowed": options},
            )
        bbox = item.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            raise ApiError(400, "BAD_BOX", f"第 {number} 个框需要 [x1,y1,x2,y2]")
        try:
            x1, y1, x2, y2 = (float(value) for value in bbox)
        except (TypeError, ValueError):
            raise ApiError(400, "BAD_BOX", f"第 {number} 个框坐标不是数字") from None
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            raise ApiError(
                400,
                "BAD_BOX",
                f"第 {number} 个框超出原图范围（{width}x{height}）",
                details={"bbox": [x1, y1, x2, y2], "image": {"width": width, "height": height}},
            )
        severity = str(item.get("severity") or DEFAULT_SEVERITY).lower()
        if severity not in SEVERITIES:
            raise ApiError(400, "BAD_SEVERITY", f"第 {number} 个框的严重程度不合法")
        clean.append(
            {
                "class_id": class_id,
                "class_name": allowed[class_id],
                "confidence": round(float(item.get("confidence") or 1.0), 4),
                "bbox": [round(x1, 3), round(y1, 3), round(x2, 3), round(y2, 3)],
                "severity": severity,
                "source": str(item.get("source") or "manual")[:16],
            }
        )
    return clean


def _request_replay(conn, project_id: str, user_id: int, record_id: str, key: str):
    """幂等键只能重放**同一用户、同一记录**的提交；不能拿来读别人的版本。"""
    if not key:
        return None
    row = conn.execute(
        "SELECT * FROM review_versions WHERE project_id = ? AND request_key = ?",
        (str(project_id), key),
    ).fetchone()
    if row is None:
        return None
    if int(row["owner_id"]) != int(user_id) or str(row["detection_id"]) != str(record_id):
        raise ApiError(
            409,
            "REQUEST_KEY_CONFLICT",
            "这个请求键已用于另一条记录或另一位用户，请换一个再提交",
        )
    return row


def _frozen_class_table(options: list[dict]) -> list[dict]:
    return [{"index": item["index"], "name": item["name"]} for item in options]


def save_draft(
    conn,
    user_id: int,
    project_id: str,
    record_id: str,
    *,
    boxes: Any,
    note: str = "",
    base_version: int = 0,
) -> dict:
    data = state(conn, user_id, project_id, record_id)
    width, height = data["image"]["width"], data["image"]["height"]
    if not width or not height:
        raise ApiError(409, "IMAGE_SIZE_UNKNOWN", "这张原图没有登记尺寸，不能编辑标注")
    clean = _validate_boxes(boxes, data["class_options"], width, height)
    # 草稿的并发基线是“最近一条版本”，**草稿本身也算**：每保存一次就前进一版，
    # 这样两个页面拿同一个旧基线保存时，后到的那个必须 409，不能最后写入者覆盖。
    # 已提交版本仍然只由 submit/reject 推进，编号不受草稿编辑影响太大（见下方 version 计算）。
    base_row = _latest_any(conn, record_id, user_id)
    current = int(base_row["version"]) if base_row else 0
    if int(base_version) != current:
        raise ApiError(
            409,
            "REVIEW_CONFLICT",
            "这条记录已经有更新的版本，请刷新后再改；你的草稿没有丢，可以对照后重新保存。",
            details={
                "current_version": current,
                "latest": public_version(base_row) if base_row else None,
            },
        )
    now = security.now_iso()
    draft = _draft(conn, record_id, user_id)
    version = int(current) + 1
    try:
        if draft is None:
            conn.execute(
                "INSERT INTO review_versions (id, project_id, owner_id, detection_id, version,"
                " status, boxes, class_table, image_width, image_height, model_id, dataset_id,"
                " note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    security.new_id("rvw"),
                    str(project_id),
                    int(user_id),
                    str(record_id),
                    version,
                    json.dumps(clean, ensure_ascii=False),
                    json.dumps(_frozen_class_table(data["class_options"]), ensure_ascii=False),
                    width,
                    height,
                    str(data["record"]["model_id"]),
                    data["record"]["dataset_id"],
                    str(note or "")[:1000],
                    now,
                    now,
                ),
            )
        else:
            changed = conn.execute(
                "UPDATE review_versions SET boxes = ?, class_table = ?, image_width = ?,"
                " image_height = ?, note = ?, version = ?, updated_at = ?"
                " WHERE id = ? AND status = 'draft' AND version = ?",
                (
                    json.dumps(clean, ensure_ascii=False),
                    json.dumps(_frozen_class_table(data["class_options"]), ensure_ascii=False),
                    width,
                    height,
                    str(note or "")[:1000],
                    version,
                    now,
                    str(draft["id"]),
                    int(draft["version"]),
                ),
            ).rowcount
            if not changed:
                conn.rollback()
                raise ApiError(
                    409,
                    "REVIEW_CONFLICT",
                    "另一个页面刚刚改过这份草稿，本次保存没有写入；请刷新后对照再保存。",
                    details={"current_version": current},
                )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ApiError(
            409,
            "REVIEW_CONFLICT",
            "保存草稿时发现版本冲突，请刷新后重试。",
            details={"current_version": int(current)},
        ) from None
    return state(conn, user_id, project_id, record_id)


def submit(
    conn,
    user_id: int,
    project_id: str,
    record_id: str,
    *,
    boxes: Any,
    outcome: str,
    note: str = "",
    base_version: int = 0,
    request_key: str = "",
    session_id: str | None = None,
) -> tuple[dict, bool]:
    """提交人工结论。只有本人页面能调用；返回 ``(version, created)``。"""
    key = str(request_key or "").strip()
    existing = _request_replay(conn, project_id, user_id, record_id, key)
    if existing is not None:
        return public_version(existing), False
    value = str(outcome or "").strip().lower()
    if value not in OUTCOMES:
        raise ApiError(400, "BAD_OUTCOME", "结论只能是 defect（有缺陷）或 no_defect（无缺陷）")

    data = state(conn, user_id, project_id, record_id)
    width, height = data["image"]["width"], data["image"]["height"]
    clean = _validate_boxes(boxes, data["class_options"], width, height)
    if value == "no_defect" and clean:
        raise ApiError(400, "BOXES_NOT_EMPTY", "确认无缺陷时不能保留标注框")
    if value == "defect" and not clean:
        raise ApiError(400, "BOXES_REQUIRED", "确认有缺陷时至少需要一个标注框")

    latest = data["latest_submitted"]["version"] if data["latest_submitted"] else 0
    if int(base_version) != int(latest):
        raise ApiError(
            409,
            "REVIEW_CONFLICT",
            "这条记录已经有更新的版本，本次提交没有写入；草稿仍在，可刷新后重新提交。",
            details={"current_version": int(latest), "latest": data["latest_submitted"]},
        )
    # 人工结论的版本号只数**提交**：草稿的编辑号是乐观锁基线，不是结论版本。
    # 用户看到的“第 1 版”必须是第一次真实确认，不能因为存过草稿变成第 5 版。
    version = int(latest) + 1
    now = security.now_iso()
    review_id = security.new_id("rvw")
    draft = _draft(conn, record_id, user_id)
    try:
        if draft is not None:
            conn.execute("DELETE FROM review_versions WHERE id = ? AND status = 'draft'", (str(draft["id"]),))
        conn.execute(
            "INSERT INTO review_versions (id, project_id, owner_id, detection_id, version, status,"
            " outcome, boxes, class_table, image_width, image_height, model_id, dataset_id, note,"
            " request_key, created_at, updated_at, submitted_at)"
            " VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                review_id,
                str(project_id),
                int(user_id),
                str(record_id),
                version,
                value,
                json.dumps(clean, ensure_ascii=False),
                json.dumps(_frozen_class_table(data["class_options"]), ensure_ascii=False),
                width,
                height,
                str(data["record"]["model_id"]),
                data["record"]["dataset_id"],
                str(note or "")[:1000],
                key or None,
                now,
                now,
                now,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ApiError(
            409,
            "REVIEW_CONFLICT",
            "提交时发现版本冲突（可能另一个页面刚刚提交过），请刷新后再确认。",
            details={"current_version": int(latest)},
        ) from None

    row = conn.execute("SELECT * FROM review_versions WHERE id = ?", (review_id,)).fetchone()
    _notice(
        conn,
        project_id=project_id,
        owner_id=user_id,
        session_id=session_id,
        record_id=record_id,
        version=version,
        action="confirmed",
        outcome=value,
        boxes=len(clean),
    )
    return public_version(row), True


def reject(
    conn,
    user_id: int,
    project_id: str,
    record_id: str,
    *,
    reason: str = "",
    base_version: int = 0,
    request_key: str = "",
    session_id: str | None = None,
) -> tuple[dict, bool]:
    key = str(request_key or "").strip()
    existing = _request_replay(conn, project_id, user_id, record_id, key)
    if existing is not None:
        return public_version(existing), False
    data = state(conn, user_id, project_id, record_id)
    latest_row = data["latest_submitted"]
    # 已经在驳回状态：重复点不涨版本，也不需要“无限驳回”。
    if latest_row and latest_row["status"] == "rejected":
        return latest_row, False
    latest = latest_row["version"] if latest_row else 0
    if int(base_version) != int(latest):
        raise ApiError(
            409,
            "REVIEW_CONFLICT",
            "这条记录已经有更新的版本，本次驳回没有写入。",
            details={"current_version": int(latest), "latest": latest_row},
        )
    # 人工结论的版本号只数**提交**：草稿的编辑号是乐观锁基线，不是结论版本。
    # 用户看到的“第 1 版”必须是第一次真实确认，不能因为存过草稿变成第 5 版。
    version = int(latest) + 1
    now = security.now_iso()
    review_id = security.new_id("rvw")
    draft = _draft(conn, record_id, user_id)
    try:
        if draft is not None:
            conn.execute("DELETE FROM review_versions WHERE id = ? AND status = 'draft'", (str(draft["id"]),))
        conn.execute(
            "INSERT INTO review_versions (id, project_id, owner_id, detection_id, version, status,"
            " boxes, class_table, image_width, image_height, model_id, dataset_id, note,"
            " reject_reason, request_key, created_at, updated_at, submitted_at)"
            " VALUES (?, ?, ?, ?, ?, 'rejected', '[]', '[]', ?, ?, ?, ?, '', ?, ?, ?, ?, ?)",
            (
                review_id,
                str(project_id),
                int(user_id),
                str(record_id),
                version,
                int(data["image"]["width"]),
                int(data["image"]["height"]),
                str(data["record"]["model_id"]),
                data["record"]["dataset_id"],
                str(reason or "")[:500],
                key or None,
                now,
                now,
                now,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ApiError(
            409,
            "REVIEW_CONFLICT",
            "驳回时发现版本冲突，请刷新后再试。",
            details={"current_version": int(latest)},
        ) from None
    row = conn.execute("SELECT * FROM review_versions WHERE id = ?", (review_id,)).fetchone()
    _notice(
        conn,
        project_id=project_id,
        owner_id=user_id,
        session_id=session_id,
        record_id=record_id,
        version=version,
        action="rejected",
        outcome=None,
        boxes=0,
    )
    return public_version(row), True


def reopen(
    conn,
    user_id: int,
    project_id: str,
    record_id: str,
    *,
    request_key: str = "",
) -> dict:
    """显式把已驳回的记录放回待复核：生成新草稿，历史不改。"""
    key = str(request_key or "").strip()
    if _request_replay(conn, project_id, user_id, record_id, key) is not None:
        return state(conn, user_id, project_id, record_id)
    data = state(conn, user_id, project_id, record_id)
    latest_row = data["latest_submitted"]
    if latest_row is None or latest_row["status"] != "rejected":
        raise ApiError(409, "NOT_REJECTED", "只有已驳回的记录需要重开")
    if data["draft"]:
        return data
    now = security.now_iso()
    version = int(latest_row["version"]) + 1
    source = data["latest_confirmed"]
    boxes = source["boxes"] if source else data["record"]["boxes"]
    conn.execute(
        "INSERT INTO review_versions (id, project_id, owner_id, detection_id, version, status,"
        " boxes, class_table, image_width, image_height, model_id, dataset_id, note, request_key,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, '', ?, ?, ?)",
        (
            security.new_id("rvw"),
            str(project_id),
            int(user_id),
            str(record_id),
            version,
            json.dumps(boxes, ensure_ascii=False),
            json.dumps(_frozen_class_table(data["class_options"]), ensure_ascii=False),
            int(data["image"]["width"]),
            int(data["image"]["height"]),
            str(data["record"]["model_id"]),
            data["record"]["dataset_id"],
            key or None,
            now,
            now,
        ),
    )
    conn.commit()
    return state(conn, user_id, project_id, record_id)


def queue(
    conn,
    user_id: int,
    project_id: str,
    *,
    view: str = "pending",
    record_ids: list[str] | None = None,
    limit: int = 50,
) -> dict:
    """待复核 / 已驳回 / 已确认 三个视图。驳回后默认不再出现在待复核里。"""
    clauses = [
        "d.project_id = ?",
        "d.owner_id = ?",
        "d.status = 'done'",
        "d.deleted_at IS NULL",
    ]
    params: list[Any] = [str(project_id), int(user_id)]
    if record_ids:
        placeholders = ", ".join("?" for _ in record_ids)
        clauses.append(f"d.id IN ({placeholders})")
        params.extend(str(item) for item in record_ids)
    rows = conn.execute(
        f"SELECT d.* FROM detections d WHERE {' AND '.join(clauses)} "
        "ORDER BY d.created_at DESC LIMIT 500",
        tuple(params),
    ).fetchall()
    buckets = {"pending": [], "draft": [], "rejected": [], "confirmed": []}
    for row in rows:
        latest = _latest_submitted(conn, row["id"], user_id)
        draft = _draft(conn, row["id"], user_id)
        bucket = _queue_name(latest) if latest is not None else ("draft" if draft else "pending")
        item = detection_service.public_with_labels(conn, user_id, project_id, row)
        item["review"] = public_version(latest) if latest else None
        item["draft"] = public_version(draft) if draft else None
        item["bucket"] = bucket
        buckets[bucket].append(item)
    wanted = view if view in buckets else "pending"
    items = buckets[wanted][: max(1, min(int(limit or 50), 200))]
    return {
        "view": wanted,
        "items": items,
        "counts": {name: len(value) for name, value in buckets.items()},
        "flow_ids": sorted({str(row["flow_id"]) for row in rows if row["flow_id"]}),
    }


def _notice(conn, *, project_id: str, owner_id: int, session_id: str | None,
            record_id: str, version: int, action: str, outcome: str | None, boxes: int) -> None:
    """把“人工已提交”变成会话里可见的真实事件。

    用一条 done 状态的轻量作业承载（幂等键按版本），这样会话级轮询能直接捞到；
    它不是执行任务，不排队、不调用模型。
    """
    try:
        job, created = job_service.create(
            conn,
            project_id=project_id,
            owner_id=owner_id,
            kind="review_notice",
            request_key=f"review-notice:{record_id}:{version}",
            payload={"record_id": record_id, "version": version, "action": action},
            session_id=session_id,
        )
        if not created:
            return
        job_service.update(
            conn,
            str(job["id"]),
            status="done",
            result=json.dumps(
                {"record_id": record_id, "version": version, "action": action,
                 "outcome": outcome, "boxes": boxes},
                ensure_ascii=False,
            ),
            finished_at=security.now_iso(),
        )
        job_service.emit(
            conn,
            job_service.get(conn, str(job["id"])),
            "review.submitted",
            {
                "record_id": record_id,
                "version": version,
                "action": action,
                "outcome": outcome,
                "boxes": boxes,
                "summary": (
                    f"人工复核已提交：记录 {record_id[-6:]} 第 {version} 版，"
                    + ("确认有缺陷" if outcome == "defect" else "确认无缺陷" if outcome == "no_defect" else "已驳回")
                ),
            },
        )
    except Exception:  # noqa: BLE001 - 通知失败不能影响已提交的复核
        import logging

        logging.getLogger(__name__).exception("复核通知事件写入失败 record=%s", record_id)


__all__ = [
    "MAX_BOXES",
    "OUTCOMES",
    "SEVERITIES",
    "class_options",
    "public_version",
    "queue",
    "reject",
    "reopen",
    "save_draft",
    "state",
    "submit",
]

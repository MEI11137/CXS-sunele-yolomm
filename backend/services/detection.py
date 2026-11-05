"""检测记录：一次推理 = 一条可追溯记录。

记录里必须能回答四个问题：**谁**（owner/project）、**用什么**（模型、权重 SHA、
数据集版本）、**看的是什么**（RGB 与 X 的原资产 ID/SHA/尺寸、X 是上传还是派生）、
**算出了什么**（置信度、bbox、类别表、命中数、结果图）。

原始图片字节不会被推理过程的格式转换覆盖：结果图画在 ``artifacts/<记录ID>/`` 下，
原资产仍然按上传时的 SHA 可复核。

幂等：``(project_id, request_key)`` 唯一。重试同一个 key 返回同一条记录，
既不会多一条产物，也不会重复计费/重复推理。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from backend import security, timeutil
from backend.errors import ApiError
from backend.services import assets as asset_service
from backend.services import models as model_service
from backend.services import storage
from backend.vision import runtime as vision_runtime


def public(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    project_id = data["project_id"]
    record_id = data["id"]
    return {
        "id": record_id,
        "project_id": project_id,
        "session_id": data.get("session_id"),
        "job_id": data.get("job_id"),
        "model_id": data["model_id"],
        "dataset_id": data.get("dataset_id"),
        "status": data["status"],
        "confidence": float(data["confidence"] or 0),
        "boxes": _loads(data.get("boxes"), []),
        "class_summary": _loads(data.get("class_summary"), {}),
        "hit_count": int(data.get("hit_count") or 0),
        "rgb_asset_id": data["rgb_asset_id"],
        "x_asset_id": data.get("x_asset_id"),
        "x_origin": data["x_origin"],
        "created_at": data["created_at"],
        "created_at_local": timeutil.to_local(data["created_at"]),
        "finished_at": data.get("finished_at"),
        "error": (
            {"code": data.get("error_code"), "message": data.get("error_message")}
            if data.get("error_code")
            else None
        ),
        "image_url": f"/api/projects/{project_id}/assets/{data['rgb_asset_id']}/content",
        "result_url": (
            f"/api/projects/{project_id}/detections/{record_id}/result"
            if data.get("result_rel")
            else None
        ),
        "request_key": data["request_key"],
    }


def _loads(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (ValueError, TypeError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def get_owned(conn, user_id: int, project_id: str, record_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM detections WHERE id = ? AND project_id = ? AND owner_id = ?",
        (str(record_id), str(project_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise ApiError(404, "RECORD_NOT_FOUND", "识别记录不存在或不属于当前项目")
    return row


def by_request_key(conn, user_id: int, project_id: str, request_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM detections WHERE project_id = ? AND owner_id = ? AND request_key = ?",
        (str(project_id), int(user_id), str(request_key)),
    ).fetchone()


def list_for_project(
    conn,
    user_id: int,
    project_id: str,
    *,
    status: str | None = None,
    model_id: str | None = None,
    session_id: str | None = None,
    has_hits: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
    before: str | None = None,
) -> dict:
    """分页 + 筛选。日期按**本地日期**解释，并在返回里给出换算后的 UTC 边界。"""
    bounds = timeutil.day_bounds(date_from, date_to)
    clauses = ["project_id = ?", "owner_id = ?", "created_at >= ?", "created_at < ?"]
    params: list[Any] = [str(project_id), int(user_id), bounds["utc_from"], bounds["utc_to_exclusive"]]
    if status:
        clauses.append("status = ?")
        params.append(str(status))
    if model_id:
        clauses.append("model_id = ?")
        params.append(str(model_id))
    if session_id:
        clauses.append("session_id = ?")
        params.append(str(session_id))
    if has_hits is True:
        clauses.append("hit_count > 0")
    elif has_hits is False:
        clauses.append("hit_count = 0")
    if before:
        cursor = conn.execute(
            "SELECT created_at, id FROM detections WHERE id = ? AND project_id = ? AND owner_id = ?",
            (str(before), str(project_id), int(user_id)),
        ).fetchone()
        if cursor is not None:
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([cursor["created_at"], cursor["created_at"], cursor["id"]])
    limit = max(1, min(int(limit or 20), 200))
    rows = conn.execute(
        f"SELECT * FROM detections WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    page = rows[:limit]
    total = conn.execute(
        f"SELECT COUNT(*) FROM detections WHERE {' AND '.join(clauses[:4])}",
        tuple(params[:4]),
    ).fetchone()[0]
    return {
        "records": [public(row) for row in page],
        "has_more": len(rows) > limit,
        "next_before": page[-1]["id"] if len(rows) > limit and page else None,
        "total_in_range": int(total),
        "filters": {
            "status": status,
            "model_id": model_id,
            "session_id": session_id,
            "has_hits": has_hits,
            "applied": bounds,
        },
    }


def run_detection(
    conn,
    user_id: int,
    project_id: str,
    *,
    model_id: str,
    rgb_asset_id: str,
    x_asset_id: str | None = None,
    confidence: float = 0.25,
    request_key: str,
    session_id: str | None = None,
    turn_id: str | None = None,
    job_id: str | None = None,
) -> dict:
    key = str(request_key or "").strip()
    if not key:
        raise ApiError(400, "REQUEST_KEY_REQUIRED", "缺少 request_key（用于幂等）")
    existing = by_request_key(conn, user_id, project_id, key)
    if existing is not None:
        return public(existing)

    model = model_service.get_owned(conn, user_id, project_id, model_id)
    if model["status"] != "published":
        raise ApiError(409, "MODEL_NOT_PUBLISHED", "模型还没有发布，不能用于检测")
    rgb = asset_service.get_owned(conn, user_id, project_id, rgb_asset_id)
    if rgb["kind"] != "image":
        raise ApiError(409, "NOT_AN_IMAGE", "RGB 输入必须是图片资产")
    # 权重缺失要在**建记录之前**就报错：否则会留下一条永远 running 的孤儿记录，
    # 页面上看起来像“检测卡住了”，而不是“文件没了”。
    try:
        storage.resolve_rel(
            storage.project_root(conn, user_id, project_id), model["weight_rel"], must_exist=True
        )
    except ApiError:
        raise ApiError(
            409, "MODEL_WEIGHT_MISSING", "模型权重文件已不存在，请重新导入或重新发布该模型"
        ) from None
    confidence = max(0.01, min(float(confidence or 0.25), 0.99))

    x_row = None
    x_origin = "none"
    if str(model["model_mode"]) == "multimodal":
        secondary = str(model["secondary_modality"] or "")
        if x_asset_id:
            x_row = asset_service.get_owned(conn, user_id, project_id, x_asset_id)
            if x_row["kind"] != "image":
                raise ApiError(409, "NOT_AN_IMAGE", "X 模态输入必须是图片资产")
            x_origin = "uploaded"
        elif secondary == "edge":
            derived = asset_service.derive_edge(conn, user_id, project_id, rgb)
            x_row = asset_service.get_owned(conn, user_id, project_id, derived["id"])
            x_origin = "derived"
        else:
            raise ApiError(
                409,
                "VISION_X_REQUIRED",
                f"这个模型需要 {secondary} 模态图片；系统不会用 RGB 或零图顶替",
            )
    elif x_asset_id:
        raise ApiError(400, "MODEL_SINGLE_MODAL", "单模态模型不接受 X 输入")

    record_id = security.new_id("det")
    now = security.now_iso()
    try:
        conn.execute(
            """
            INSERT INTO detections
                (id, project_id, owner_id, session_id, turn_id, job_id, request_key, model_id,
                 dataset_id, rgb_asset_id, x_asset_id, x_origin, confidence, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                record_id,
                str(project_id),
                int(user_id),
                session_id,
                turn_id,
                job_id,
                key,
                str(model_id),
                model["dataset_id"],
                str(rgb_asset_id),
                str(x_row["id"]) if x_row is not None else None,
                x_origin,
                confidence,
                now,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # 并发下另一个线程刚插入了同一个 key：返回它的记录，不重复推理。
        existing = by_request_key(conn, user_id, project_id, key)
        if existing is not None:
            return public(existing)
        raise

    try:
        result = _infer(conn, user_id, project_id, model, rgb, x_row, confidence, record_id)
    except vision_runtime.VisionError as exc:
        _mark_failed(conn, record_id, exc.code, exc.message)
        return public(get_owned(conn, user_id, project_id, record_id))

    summary: dict[str, int] = {}
    for box in result["boxes"]:
        summary[box["class_name"]] = summary.get(box["class_name"], 0) + 1
    conn.execute(
        """
        UPDATE detections SET status = 'done', boxes = ?, class_summary = ?, hit_count = ?,
               image_rel = ?, result_rel = ?, finished_at = ?
         WHERE id = ? AND status = 'running'
        """,
        (
            json.dumps(result["boxes"], ensure_ascii=False),
            json.dumps(summary, ensure_ascii=False),
            len(result["boxes"]),
            result["image_rel"],
            result["result_rel"],
            security.now_iso(),
            record_id,
        ),
    )
    conn.commit()
    return public(get_owned(conn, user_id, project_id, record_id))


def _infer(conn, user_id: int, project_id: str, model, rgb, x_row, confidence: float, record_id: str) -> dict:
    root = storage.project_root(conn, user_id, project_id)
    rgb_path = asset_service.path_of(conn, user_id, project_id, rgb["id"])
    x_path = (
        asset_service.path_of(conn, user_id, project_id, x_row["id"]) if x_row is not None else None
    )
    weight_path = storage.resolve_rel(root, model["weight_rel"], must_exist=True)
    inputs = vision_runtime.resolve_inputs(
        model_mode=str(model["model_mode"]),
        secondary_modality=model["secondary_modality"],
        rgb_path=rgb_path,
        x_path=x_path,
    )
    output = vision_runtime.predict(
        inputs,
        weight_path=weight_path,
        model_mode=str(model["model_mode"]),
        confidence=confidence,
    )
    mapping = _loads(model["class_map"], {})
    labels = model_service.class_labels(model)
    boxes: list[dict] = []
    for item in output.boxes:
        native_id = int(item["class_id"])
        dataset_id = int(mapping.get(str(native_id), native_id)) if mapping else native_id
        boxes.append(
            {
                "class_id": dataset_id,
                "native_class_id": native_id,
                "class_name": labels.get(dataset_id, item.get("class_name") or str(dataset_id)),
                "confidence": item["confidence"],
                "bbox": item["bbox"],
            }
        )
    artifacts = storage.project_dir(root, storage.ARTIFACTS, record_id)
    result_path = artifacts / "result.jpg"
    _render(root, rgb_path, boxes, result_path)
    return {
        "boxes": boxes,
        "image_rel": storage.relative(root, rgb_path),
        "result_rel": storage.relative(root, result_path) if result_path.is_file() else None,
    }


def _render(root: Path, rgb_path: Path, boxes: list[dict], target: Path) -> None:
    """把 bbox 画在**副本**上；原图字节一个都没动。"""
    import cv2
    import numpy as np

    data = rgb_path.read_bytes()
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise vision_runtime.VisionError("VISION_IMAGE_UNREADABLE", "结果图渲染失败：原图无法解码")
    for box in boxes:
        x1, y1, x2, y2 = (int(round(value)) for value in box["bbox"])
        cv2.rectangle(image, (x1, y1), (x2, y2), (0, 0, 255), 2)
        name = str(box["class_name"])
        text = f"{name if name.isascii() else box['class_id']} {box['confidence']:.2f}"
        cv2.putText(
            image,
            text,
            (max(0, x1), max(12, y1 - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 0, 255),
            1,
            cv2.LINE_AA,
        )
    ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    if not ok:
        raise vision_runtime.VisionError("VISION_RENDER_FAILED", "结果图编码失败")
    storage.write_bytes(root, target, encoded.tobytes())


def _mark_failed(conn, record_id: str, code: str, message: str) -> None:
    conn.execute(
        "UPDATE detections SET status = 'failed', error_code = ?, error_message = ?, finished_at = ? "
        "WHERE id = ? AND status = 'running'",
        (code, message[:500], security.now_iso(), str(record_id)),
    )
    conn.commit()


def mark_paused(conn, record_id: str, code: str, message: str) -> None:
    conn.execute(
        "UPDATE detections SET status = 'paused', error_code = ?, error_message = ?, finished_at = ? "
        "WHERE id = ? AND status IN ('queued', 'running')",
        (code, message[:500], security.now_iso(), str(record_id)),
    )
    conn.commit()


def result_path(conn, user_id: int, project_id: str, record_id: str) -> Path:
    row = get_owned(conn, user_id, project_id, record_id)
    if not row["result_rel"]:
        raise ApiError(404, "NO_RESULT_IMAGE", "这条记录没有结果图")
    root = storage.project_root(conn, user_id, project_id)
    return storage.resolve_rel(root, row["result_rel"], must_exist=True)


__all__ = [
    "by_request_key",
    "get_owned",
    "list_for_project",
    "mark_paused",
    "public",
    "result_path",
    "run_detection",
]

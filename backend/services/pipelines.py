"""业务流水线：批量检测与巡检。

这里是“服务端的确定性流程”，不是第二个 LLM 规划器：

* 模型只负责**发起**（选目录、给置信度与数量上限）与**查询**；
* 真正的扫描、逐张检测、核查、生成人工队列都在这几个函数里按固定顺序跑；
* 每一步都复用上一阶段的任务/事件/幂等（``jobs`` + ``job_events`` + ``detections.request_key``）。

失败语义（对应验收要求）：

* 单张图片解码失败 → 记一条 failed 记录，**继续**处理其余图片（成功记录保留）；
* 依赖项失败（模型未发布、权重缺失、缺 X 模态、目录撤权）→ 立即**暂停**剩余条目，
  不再继续制造必然失败的记录，并在结果里说清楚原因。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from backend import security
from backend.errors import ApiError
from backend.services import assets as asset_service
from backend.services import detection as detection_service
from backend.services import jobs as job_service
from backend.services import models as model_service
from backend.services import storage

MAX_BATCH = 50
DEPENDENCY_CODES = {
    "MODEL_NOT_FOUND",
    "MODEL_NOT_PUBLISHED",
    "ASSET_NOT_FOUND",
    "ROOT_MISSING",
    "GRANT_REVOKED",
    "VISION_X_REQUIRED",
    "VISION_WEIGHT_MISSING",
    "VISION_VENDOR_MISSING",
    "VISION_VENDOR_SHADOWED",
    "VISION_DEPENDENCY_MISSING",
}


def _error_code(exc: BaseException) -> str:
    return str(getattr(exc, "code", "") or type(exc).__name__)


# --------------------------------------------------------------------------- 批量检测


def start_detection_batch(
    conn,
    user_id: int,
    project_id: str,
    *,
    model_id: str,
    items: list[dict],
    request_key: str,
    confidence: float = 0.25,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> tuple[dict, bool]:
    if not items:
        raise ApiError(400, "NO_ITEMS", "没有要检测的图片")
    if len(items) > MAX_BATCH:
        raise ApiError(400, "TOO_MANY_ITEMS", f"一次最多 {MAX_BATCH} 张")
    model = model_service.get_owned(conn, user_id, project_id, model_id)
    if model["status"] != "published":
        raise ApiError(409, "MODEL_NOT_PUBLISHED", "模型还没有发布，不能用于检测")
    clean: list[dict] = []
    for item in items:
        rgb_id = str(item.get("rgb_asset_id") or "").strip()
        if not rgb_id:
            raise ApiError(400, "BAD_ITEM", "每一项都需要 rgb_asset_id")
        asset_service.get_owned(conn, user_id, project_id, rgb_id)
        x_id = str(item.get("x_asset_id") or "").strip() or None
        if x_id:
            asset_service.get_owned(conn, user_id, project_id, x_id)
        clean.append(
            {
                "rgb_asset_id": rgb_id,
                "x_asset_id": x_id,
                "confidence": float(item.get("confidence") or confidence),
            }
        )
    job, created = job_service.create(
        conn,
        project_id=project_id,
        owner_id=int(user_id),
        kind="detection_batch",
        request_key=request_key,
        payload={"model_id": str(model_id), "items": clean, "confidence": float(confidence)},
        session_id=session_id,
        turn_id=turn_id,
    )
    if created:
        job_service.submit(str(job["id"]), run_detection_batch)
    return job_service.public(job), created


def run_detection_batch(conn, job, payload_override: dict | None = None) -> dict:
    # sqlite3.Row 是只读的：巡检流程要用自己组装的那份 items，不能改写 job 行。
    payload = payload_override if payload_override is not None else _loads(job["payload"], {})
    items = payload.get("items") or []
    model_id = str(payload.get("model_id") or "")
    owner_id = int(job["owner_id"])
    project_id = str(job["project_id"])
    results: list[dict] = []
    paused = False
    pause_reason: dict | None = None
    done = failed = 0
    for index, item in enumerate(items):
        if paused:
            results.append(
                {
                    "index": index,
                    "rgb_asset_id": item.get("rgb_asset_id"),
                    "status": "paused",
                    "reason": pause_reason,
                }
            )
            continue
        key = f"{job['id']}:{index}:{item.get('rgb_asset_id')}"
        try:
            record = detection_service.run_detection(
                conn,
                owner_id,
                project_id,
                model_id=model_id,
                rgb_asset_id=str(item.get("rgb_asset_id")),
                x_asset_id=item.get("x_asset_id"),
                confidence=float(item.get("confidence") or 0.25),
                request_key=key,
                session_id=job["session_id"],
                turn_id=job["turn_id"],
                job_id=str(job["id"]),
            )
        except ApiError as exc:
            code = _error_code(exc)
            results.append(
                {
                    "index": index,
                    "rgb_asset_id": item.get("rgb_asset_id"),
                    "status": "failed",
                    "error": {"code": code, "message": exc.msg},
                }
            )
            failed += 1
            if code in DEPENDENCY_CODES:
                paused = True
                pause_reason = {"code": code, "message": exc.msg}
            job_service.emit(
                conn, job, "business.detection", {"index": index, "total": len(items), "item": results[-1]}
            )
            continue

        if record["status"] == "done":
            done += 1
        else:
            failed += 1
            error = record.get("error") or {}
            if str(error.get("code") or "") in DEPENDENCY_CODES:
                paused = True
                pause_reason = error
        results.append(
            {
                "index": index,
                "rgb_asset_id": item.get("rgb_asset_id"),
                "status": record["status"],
                "record_id": record["id"],
                "hit_count": record["hit_count"],
                "error": record.get("error"),
            }
        )
        job_service.emit(
            conn,
            job,
            "business.detection",
            {
                "index": index,
                "total": len(items),
                "item": results[-1],
                "record": {
                    "id": record["id"],
                    "hit_count": record["hit_count"],
                    "class_summary": record["class_summary"],
                    "result_url": record["result_url"],
                    "image_url": record["image_url"],
                },
            },
        )
        job_service.update(
            conn,
            str(job["id"]),
            progress=json.dumps({"done": done, "failed": failed, "total": len(items)}, ensure_ascii=False),
        )

    status = "done" if failed == 0 else ("failed" if done == 0 else "partial")
    return {
        "status": status,
        "items": results,
        "done": done,
        "failed": failed,
        "paused": len(items) - done - failed,
        "total": len(items),
        "pause_reason": pause_reason,
    }


# --------------------------------------------------------------------------- 巡检（发现 → 检测 → 核查 → 队列）


def discover_images(conn, user_id: int, project_id: str, directory: str, *, x_directory: str | None = None) -> dict:
    """在项目目录里找真实图片，并按同名 stem 找配图。"""
    root = storage.project_root(conn, user_id, project_id)
    folder = storage.resolve_rel(root, directory)
    if not folder.is_dir():
        raise ApiError(404, "DIR_NOT_FOUND", "这个目录不存在")
    x_folder = storage.resolve_rel(root, x_directory) if x_directory else _guess_x_dir(folder)
    images = sorted(
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
    )
    if not images:
        raise ApiError(404, "NO_IMAGES", "这个目录里没有可识别的图片文件")
    pairs: list[dict] = []
    missing_x: list[str] = []
    for path in images:
        x_path = None
        if x_folder is not None:
            for suffix in (path.suffix, ".jpg", ".png"):
                candidate = x_folder / f"{path.stem}{suffix}"
                if candidate.is_file():
                    x_path = candidate
                    break
        if x_path is None and x_folder is not None:
            missing_x.append(path.name)
        pairs.append({"rgb": path, "x": x_path})
    return {
        "directory": storage.relative(root, folder),
        "x_directory": storage.relative(root, x_folder) if x_folder else None,
        "pairs": pairs,
        "missing_x": missing_x,
        "count": len(pairs),
    }


def _guess_x_dir(folder: Path) -> Path | None:
    name = folder.name
    candidates = []
    if name.startswith("images"):
        candidates.append(folder.parent / name.replace("images", "images_ir", 1))
    candidates.extend([folder.parent / "images_ir", folder.parent / "ir", folder.parent / f"{name}_ir"])
    for candidate in candidates:
        if candidate.is_dir() and candidate != folder:
            return candidate
    return None


def start_inspection(
    conn,
    user_id: int,
    project_id: str,
    *,
    model_id: str,
    directory: str,
    request_key: str,
    confidence: float = 0.25,
    limit: int = 20,
    review: bool = False,
    x_directory: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    review_model: str | None = None,
) -> tuple[dict, bool]:
    found = discover_images(conn, user_id, project_id, directory, x_directory=x_directory)
    if not found["pairs"]:
        raise ApiError(404, "NO_IMAGES", "没有找到可检测的图片")
    review_snapshot = {}
    if review:
        from backend.agent import identity

        selection = identity.resolve(conn, int(user_id), model=review_model)
        if selection is not None:
            # 与 AI 核查作业一致的快照：中途换连接就不再发图，避免旧模型配新地址。
            review_snapshot = {
                "review_provider": selection.provider,
                "review_fingerprint": selection.fingerprint,
                "review_model": selection.model,
            }
    if int(limit) <= 0:
        raise ApiError(400, "BAD_LIMIT", "数量上限必须大于 0")
    job, created = job_service.create(
        conn,
        project_id=project_id,
        owner_id=int(user_id),
        kind="inspection",
        request_key=request_key,
        payload={
            "model_id": str(model_id),
            "directory": found["directory"],
            "x_directory": found["x_directory"],
            "confidence": float(confidence),
            "limit": min(int(limit), MAX_BATCH),
            "review": bool(review),
            # 与聊天同一份模型快照：核查阶段不再按“当前偏好”重新解析。
            "review_model": str(review_model or ""),
            **review_snapshot,
        },
        session_id=session_id,
        turn_id=turn_id,
    )
    if created:
        job_service.submit(str(job["id"]), run_inspection)
    return job_service.public(job), created


def run_inspection(conn, job) -> dict:
    payload = _loads(job["payload"], {})
    owner_id = int(job["owner_id"])
    project_id = str(job["project_id"])
    model_id = str(payload.get("model_id") or "")
    limit = int(payload.get("limit") or 20)
    confidence = float(payload.get("confidence") or 0.25)

    job_service.update(
        conn, str(job["id"]), progress=json.dumps({"phase": "discover"}, ensure_ascii=False)
    )
    found = discover_images(
        conn, owner_id, project_id, str(payload.get("directory") or "."),
        x_directory=payload.get("x_directory"),
    )
    selected = found["pairs"][:limit]
    job_service.emit(
        conn,
        job,
        "business.inspection_discovered",
        {"count": found["count"], "selected": len(selected), "missing_x": found["missing_x"][:20]},
    )

    items = []
    for pair in selected:
        rgb_asset = asset_service.add_bytes(
            conn,
            owner_id,
            project_id,
            data=pair["rgb"].read_bytes(),
            filename=pair["rgb"].name,
            role="rgb",
            session_id=job["session_id"],
            origin="local",
            source_path=found["directory"] + "/" + pair["rgb"].name,
        )
        x_asset_id = None
        if pair["x"] is not None:
            x_asset = asset_service.add_bytes(
                conn,
                owner_id,
                project_id,
                data=pair["x"].read_bytes(),
                filename=pair["x"].name,
                role="ir",
                session_id=job["session_id"],
                origin="local",
                source_path=found["x_directory"] + "/" + pair["x"].name if found["x_directory"] else None,
            )
            x_asset_id = x_asset["id"]
        items.append({"rgb_asset_id": rgb_asset["id"], "x_asset_id": x_asset_id})

    # 直接跑批（同一个作业里，避免再套一层作业导致状态分裂）。
    detection_result = run_detection_batch(
        conn, job, {"items": items, "model_id": model_id, "confidence": confidence}
    )

    reviewed: list[dict] = []
    if payload.get("review"):
        from backend.services import aireview

        review_model = str(payload.get("review_model") or "") or None
        snapshot = {
            "connection_fingerprint": str(payload.get("review_fingerprint") or ""),
            "provider": str(payload.get("review_provider") or ""),
        }
        for item in detection_result["items"]:
            record_id = item.get("record_id")
            if not record_id or item.get("status") != "done":
                continue
            try:
                review = aireview._review_one(
                    conn,
                    owner_id,
                    project_id,
                    record_id,
                    job_id=str(job["id"]),
                    model=review_model,
                    snapshot=snapshot,
                )
                reviewed.append({"record_id": record_id, "status": review["status"], "verdict": review["verdict"]})
            except ApiError as exc:
                reviewed.append(
                    {"record_id": record_id, "status": "failed", "error": {"code": exc.code, "message": exc.msg}}
                )
            job_service.emit(
                conn,
                job,
                "business.review",
                {"record_id": record_id, "item": reviewed[-1]},
            )

    record_ids = [item["record_id"] for item in detection_result["items"] if item.get("record_id")]
    queue = build_queue(conn, owner_id, project_id, record_ids)
    job_service.emit(
        conn,
        job,
        "business.queue",
        {"record_ids": record_ids, "count": len(queue)},
    )
    return {
        "status": detection_result["status"],
        "detection": detection_result,
        "review": reviewed,
        "queue": queue,
        "record_ids": record_ids,
        "discovered": {"count": found["count"], "selected": len(selected)},
    }


def build_queue(conn, user_id: int, project_id: str, record_ids: list[str]) -> list[dict]:
    """本轮人工队列：按“命中数优先、置信度次之”排序，附页面定位参数。"""
    if not record_ids:
        return []
    placeholders = ", ".join("?" for _ in record_ids)
    rows = conn.execute(
        f"SELECT * FROM detections WHERE project_id = ? AND owner_id = ? AND id IN ({placeholders})",
        (str(project_id), int(user_id), *[str(item) for item in record_ids]),
    ).fetchall()
    items = [detection_service.public(row) for row in rows]
    items.sort(key=lambda item: (item["hit_count"], item["confidence"]), reverse=True)
    return [
        {
            "record_id": item["id"],
            "hit_count": item["hit_count"],
            "classes": item["class_summary"],
            "status": item["status"],
            "created_at_local": item["created_at_local"],
            "page": {
                "name": "records",
                "params": {"projectId": project_id},
                "query": {"record": item["id"]},
            },
        }
        for item in items
    ]


def _loads(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (ValueError, TypeError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


__all__ = [
    "build_queue",
    "discover_images",
    "run_detection_batch",
    "run_inspection",
    "start_detection_batch",
    "start_inspection",
]

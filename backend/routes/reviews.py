"""人工复核接口。

提交/驳回只从**本人已登录页面**进入；请求体带 ``base_version`` 做乐观锁，
冲突返回 409 与当前版本，草稿不会被丢弃。
"""

from __future__ import annotations

from flask import Blueprint, current_app, request

from backend import auth, db, security
from backend.errors import ApiError, ok
from backend.services import followups as followup_service
from backend.services import review as review_service

bp = Blueprint("reviews", __name__)


def _start_turn():
    runtime = current_app.config.get("HX17_AGENT")
    return runtime.runner.start if runtime is not None else None


def _continue_after(conn, project_id: str, record_id: str, version: dict, created: bool, action: str) -> None:
    """提交成功才接续；幂等重放不重复唤醒。失败只记日志，不能影响已提交的结论。"""
    if not created:
        return
    try:
        followup_service.on_submitted(
            conn,
            project_id=str(project_id),
            owner_id=int(_user()["id"]),
            record_id=str(record_id),
            version=int(version["version"]),
            action=action,
            outcome=version.get("outcome"),
            boxes=len(version.get("boxes") or []) + len(version.get("x_boxes") or []),
            start=_start_turn(),
        )
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("提交后的会话接力失败 record=%s", record_id)


def _user() -> dict:
    assert auth.current_user() is not None
    return auth.current_user()  # type: ignore[return-value]


def _key(payload: dict, prefix: str) -> str:
    raw = str(payload.get("request_key") or "").strip()
    return raw[:120] if raw else f"{prefix}:{security.new_token(12)}"


@bp.get("/projects/<project_id>/reviews")
@auth.login_required
def list_queue(project_id: str):
    raw = (request.args.get("record_ids") or "").strip()
    ids = [item.strip() for item in raw.split(",") if item.strip()] if raw else None
    conn = db.get_db()
    return ok(
        review_service.queue(
            conn,
            int(_user()["id"]),
            project_id,
            view=(request.args.get("view") or "pending").strip(),
            record_ids=ids,
            limit=int(request.args.get("limit") or 50),
        )
    )


@bp.get("/projects/<project_id>/detections/<record_id>/review")
@auth.login_required
def get_state(project_id: str, record_id: str):
    """返回 ``{"state": {...}}``。

    这个包装不是可选的：草稿/重开同样返回 ``{state}``，前端适配器只认这一种形状。
    之前这里直接返回裸 state，页面读 ``data.state.draft`` 就是 undefined，
    真实浏览器里表现为 ``Cannot read properties of undefined (reading 'draft')``、
    图片不加载、按钮全禁用。契约必须两端一致，不能靠前端加可选链掩盖。
    """
    conn = db.get_db()
    return ok({"state": review_service.state(conn, int(_user()["id"]), project_id, record_id)})


@bp.get("/projects/<project_id>/review-events")
@auth.login_required
def review_events(project_id: str):
    """返回本项目最近的复核版本变更（按 updated_at 递增）。

    只读投影，不新增表：seq 语义 = (record_id, version, updated_at)。
    调用方带 ``after``（上次收到的最大 updated_at）与可选 ``record_id``，
    重复轮询同一批不会重复触发刷新。
    """
    after = str(request.args.get("after") or "").strip()
    record_id = str(request.args.get("record_id") or "").strip()
    latest = str(request.args.get("latest") or "").strip().lower() in {"1", "true", "yes", "on"}
    raw_after_version = str(request.args.get("after_version") or "").strip()
    after_version: int | None = None
    if raw_after_version:
        try:
            after_version = int(raw_after_version)
        except ValueError:
            raise ApiError(400, "BAD_CURSOR", "after_version 必须是整数")
    try:
        limit = max(1, min(200, int(request.args.get("limit") or 100)))
    except ValueError:
        raise ApiError(400, "BAD_LIMIT", "limit 必须是整数")
    conn = db.get_db()
    if latest:
        if not record_id:
            raise ApiError(400, "BAD_REQUEST", "latest=1 需要同时带 record_id")
        # 只读“当前最新一条版本”的投影。版本号在 submit 后可能从草稿 vN 回退到已提交 vM，
        # 所以调用方比较 (id, status, version)，而不是假设 version 单调。
        row = conn.execute(
            "SELECT id, detection_id, version, status, outcome, updated_at"
            " FROM review_versions WHERE project_id = ? AND owner_id = ? AND detection_id = ?"
            " ORDER BY updated_at DESC, version DESC, rowid DESC LIMIT 1",
            (str(project_id), int(_user()["id"]), record_id),
        ).fetchone()
        projection = (
            {
                "id": row["id"],
                "record_id": row["detection_id"],
                "version": int(row["version"]),
                "status": row["status"],
                "outcome": row["outcome"],
                "updated_at": row["updated_at"],
            }
            if row
            else None
        )
        return ok(
            {
                "projection": projection,
                "events": [],
                "last": projection["updated_at"] if projection else None,
                "last_version": projection["version"] if projection else None,
            }
        )
    sql = (
        "SELECT detection_id, version, status, outcome, updated_at"
        " FROM review_versions WHERE project_id = ? AND owner_id = ?"
    )
    args: list = [str(project_id), int(_user()["id"])]
    # 单条记录内 version 单调递增，同秒多次保存也不会漏；不带 record_id 的项目级
    # 轮询仍用旧 updated_at 游标（after），保持兼容。
    version_cursor = after_version is not None and bool(record_id)
    if version_cursor:
        sql += " AND version > ?"
        args.append(int(after_version))
    elif after:
        sql += " AND updated_at > ?"
        args.append(after)
    if record_id:
        sql += " AND detection_id = ?"
        args.append(record_id)
    if version_cursor:
        sql += " ORDER BY version ASC LIMIT ?"
    else:
        sql += " ORDER BY updated_at ASC, version ASC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    events = [
        {
            "record_id": row["detection_id"],
            "version": int(row["version"]),
            "status": row["status"],
            "outcome": row["outcome"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
    last_version = events[-1]["version"] if events else (after_version if version_cursor else None)
    return ok(
        {
            "events": events,
            "last": events[-1]["updated_at"] if events else after or None,
            "last_version": last_version,
        }
    )


@bp.post("/projects/<project_id>/detections/<record_id>/review/draft")
@auth.login_required
def save_draft(project_id: str, record_id: str):
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    state = review_service.save_draft(
        conn,
        int(_user()["id"]),
        project_id,
        record_id,
        boxes=payload.get("boxes"),
        # 不传 = 沿用已保存的辅模态框；显式 [] 才清空。旧客户端不会误删 X 标注。
        x_boxes=payload.get("x_boxes"),
        note=str(payload.get("note") or ""),
        base_version=int(payload.get("base_version") or 0),
    )
    return ok({"state": state})


@bp.post("/projects/<project_id>/detections/<record_id>/review/submit")
@auth.login_required
def submit(project_id: str, record_id: str):
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    version, created = review_service.submit(
        conn,
        int(_user()["id"]),
        project_id,
        record_id,
        boxes=payload.get("boxes"),
        x_boxes=payload.get("x_boxes"),
        outcome=str(payload.get("outcome") or ""),
        note=str(payload.get("note") or ""),
        base_version=int(payload.get("base_version") or 0),
        request_key=_key(payload, "review-submit"),
        session_id=(payload.get("session_id") or None),
    )
    _continue_after(conn, project_id, record_id, version, created, "confirmed")
    return ok({"version": version, "created": created})


@bp.post("/projects/<project_id>/detections/<record_id>/review/reject")
@auth.login_required
def reject(project_id: str, record_id: str):
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    version, created = review_service.reject(
        conn,
        int(_user()["id"]),
        project_id,
        record_id,
        reason=str(payload.get("reason") or ""),
        base_version=int(payload.get("base_version") or 0),
        request_key=_key(payload, "review-reject"),
        session_id=(payload.get("session_id") or None),
    )
    _continue_after(conn, project_id, record_id, version, created, "rejected")
    return ok({"version": version, "created": created})


@bp.post("/projects/<project_id>/detections/<record_id>/review/reopen")
@auth.login_required
def reopen(project_id: str, record_id: str):
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    state = review_service.reopen(
        conn,
        int(_user()["id"]),
        project_id,
        record_id,
        request_key=_key(payload, "review-reopen"),
    )
    return ok({"state": state})


@bp.post("/projects/<project_id>/detections/<record_id>/review/note")
@auth.login_required
def update_note(project_id: str, record_id: str):
    """只改草稿备注；没有草稿时报错，避免在已提交版本上“偷偷改字”。"""
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    state = review_service.state(conn, int(_user()["id"]), project_id, record_id)
    if not state["draft"]:
        raise ApiError(409, "NO_DRAFT", "没有可修改的草稿")
    updated = review_service.save_draft(
        conn,
        int(_user()["id"]),
        project_id,
        record_id,
        boxes=state["draft"]["boxes"],
        note=str(payload.get("note") or ""),
        base_version=int(state["latest"]["version"]) if state["latest"] else 0,
    )
    return ok({"state": updated})

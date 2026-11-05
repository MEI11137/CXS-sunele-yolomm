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
            boxes=len(version.get("boxes") or []),
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

"""会话与消息接口。

本轮只落库用户消息，不生成助手回复；响应里 ``assistant`` 恒为 null 并给出说明，
前端据此显示“运行时未接入”，而不是伪造一条回答。
"""

from __future__ import annotations

from flask import Blueprint, request

from backend import auth, db
from backend.errors import ok
from backend.services import projects, sessions

bp = Blueprint("sessions", __name__)

AGENT_NOTICE = "内部 Agent 运行时尚未接入（WB17-02）：这条消息已保存，但不会产生助手回复。"


def _user() -> dict:
    user = auth.current_user()
    assert user is not None
    return user


def _owned_session(session_id: str, *, include_deleted: bool = False):
    conn = db.get_db()
    user_id = int(_user()["id"])
    row = sessions.get_owned(
        conn, session_id, user_id, include_deleted=include_deleted
    )
    # 会话所属项目也必须属于本人且未删除，避免“项目已删、会话还能聊”。
    projects.get_owned(conn, row["project_id"], user_id)
    return conn, row


@bp.get("/projects/<project_id>/sessions")
@auth.login_required
def list_sessions(project_id: str):
    conn = db.get_db()
    user_id = int(_user()["id"])
    projects.get_owned(conn, project_id, user_id, include_deleted=True)
    trash = (request.args.get("trash") or "").strip().lower() in {"1", "true", "yes"}
    rows = sessions.list_for(
        conn, project_id=project_id, owner_id=user_id, include_deleted=trash
    )
    return ok({"sessions": rows})


@bp.post("/projects/<project_id>/sessions")
@auth.login_required
def create_session(project_id: str):
    conn = db.get_db()
    user_id = int(_user()["id"])
    project = projects.get_owned(conn, project_id, user_id)
    payload = request.get_json(silent=True) or {}
    row = sessions.create(
        conn,
        project_id=project["id"],
        owner_id=user_id,
        title=payload.get("title"),
    )
    return ok({"session": sessions.public(row)})


@bp.get("/sessions/<session_id>")
@auth.login_required
def get_session(session_id: str):
    conn, row = _owned_session(session_id, include_deleted=True)
    return ok({"session": sessions.public(row)})


@bp.put("/sessions/<session_id>")
@auth.login_required
def rename_session(session_id: str):
    conn, row = _owned_session(session_id)
    payload = request.get_json(silent=True) or {}
    updated = sessions.rename(conn, row, str(payload.get("title") or ""))
    return ok({"session": sessions.public(updated)})


@bp.delete("/sessions/<session_id>")
@auth.login_required
def delete_session(session_id: str):
    conn, row = _owned_session(session_id)
    sessions.soft_delete(conn, row)
    return ok({"deleted": True, "session_id": session_id})


@bp.post("/sessions/<session_id>/restore")
@auth.login_required
def restore_session(session_id: str):
    conn, row = _owned_session(session_id, include_deleted=True)
    restored = sessions.restore(conn, row)
    return ok({"session": sessions.public(restored)})


@bp.get("/sessions/<session_id>/messages")
@auth.login_required
def list_messages(session_id: str):
    conn, row = _owned_session(session_id)
    return ok({"messages": sessions.list_messages(conn, row["id"])})


@bp.post("/sessions/<session_id>/messages")
@auth.login_required
def append_message(session_id: str):
    conn, row = _owned_session(session_id)
    payload = request.get_json(silent=True) or {}
    message = sessions.append_message(
        conn,
        session_id=row["id"],
        role="user",
        content=str(payload.get("content") or ""),
        meta={"source": "web"},
    )
    return ok({"message": message, "assistant": None, "notice": AGENT_NOTICE})

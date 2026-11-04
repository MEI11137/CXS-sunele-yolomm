"""对话与 Agent 接口：发消息、停止、事件续拉、动作确认、偏好设置。

约定：

* 所有入口都先校验「会话属于本人 + 项目未删除」；轮次与动作再校验 owner；
* ``POST /sessions/{id}/turns`` 需要 ``request_id``：刷新或重复点击拿到的是同一轮，
  不会产生第二条模型调用；
* 事件接口是**幂等增量**：``after=<seq>`` 返回之后的全部事件，重连不丢、不重；
* 页面关闭不影响后台运行：轮次在服务端线程里跑，重新打开按活动轮次续上。
"""

from __future__ import annotations

import json
import re
import time

from flask import Blueprint, Response, current_app, request, stream_with_context

from backend import auth, db
from backend.agent import identity, policy, store, tools
from backend.agent.context import ToolError
from backend.errors import ApiError, conflict, not_found, ok
from backend.services import audit, projects, sessions

bp = Blueprint("agent", __name__)

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
CONTENT_MAX = 8000


def _user() -> dict:
    user = auth.current_user()
    assert user is not None
    return user


def _owned_session(session_id: str):
    conn = db.get_db()
    user_id = int(_user()["id"])
    row = sessions.get_owned(conn, session_id, user_id)
    project = projects.get_owned(conn, row["project_id"], user_id)
    return conn, row, project, user_id


def _owned_turn(turn_id: str):
    conn = db.get_db()
    user_id = int(_user()["id"])
    try:
        row = store.get(conn, turn_id)
    except KeyError:
        raise not_found("轮次") from None
    if int(row["owner_id"]) != user_id:
        raise not_found("轮次")
    return conn, row, user_id


def _runtime():
    return current_app.config["HX17_AGENT"]


# --------------------------------------------------------------------------- 能力 / 偏好


@bp.get("/agent/capabilities")
@auth.login_required
def capabilities():
    conn = db.get_db()
    user = _user()
    user_id = int(user["id"])
    connection = identity.connection_for(conn, user_id)
    runtime = _runtime()
    return ok(
        {
            "runtime": runtime.describe(),
            "tools": [
                {
                    "name": spec.name,
                    "title": spec.title,
                    "risk": spec.risk,
                    "requires_root": spec.requires_root,
                }
                for spec in tools.TOOLS.values()
            ],
            "services": {
                name: {"available": bool(info.get("registered")), "note": info.get("note", "")}
                for name, info in tools.SERVICES.items()
            },
            "connection": {
                "configured": connection is not None,
                "provider": connection["provider"] if connection is not None else None,
                "model": connection["model"] if connection is not None else None,
                "fingerprint": identity.connection_fingerprint(connection)[:8]
                if connection is not None
                else None,
            },
            "modes": [
                {"id": mode, "label": policy.MODE_LABELS[mode], "hint": policy.MODE_HINTS[mode]}
                for mode in policy.MODES
            ],
        }
    )


def _prefs_payload(conn, user_id: int) -> dict:
    saved = store.prefs(conn, user_id)
    connection = identity.connection_for(conn, user_id)
    mode = policy.normalize_mode(saved.get("mode"), policy.DEFAULT_MODE)
    models = identity.available_models(conn, user_id)
    model = (
        identity.effective_model(conn, user_id, connection) if connection is not None else None
    )
    return {
        "mode": mode,
        "mode_label": policy.MODE_LABELS[mode],
        "mode_hint": policy.MODE_HINTS[mode],
        "model": model,
        "models": models,
        "modes": [
            {"id": item, "label": policy.MODE_LABELS[item], "hint": policy.MODE_HINTS[item]}
            for item in policy.MODES
        ],
        "updated_at": saved.get("updated_at"),
    }


@bp.get("/me/preferences")
@auth.login_required
def get_preferences():
    conn = db.get_db()
    return ok(_prefs_payload(conn, int(_user()["id"])))


@bp.put("/me/preferences")
@auth.login_required
def put_preferences():
    conn = db.get_db()
    user_id = int(_user()["id"])
    payload = request.get_json(silent=True) or {}
    raw_mode = payload.get("mode")
    raw_model = payload.get("model")
    mode = None
    if raw_mode is not None:
        if str(raw_mode) not in policy.MODES:
            raise ApiError(400, "BAD_MODE", "权限模式只能是 request / assist / full")
        mode = str(raw_mode)
    model = None
    clear_model = False
    if raw_model is not None:
        candidate = str(raw_model).strip()
        if candidate:
            allowed = identity.available_models(conn, user_id)
            if not allowed:
                raise ApiError(409, "CONNECTION_MISSING", "还没有保存个人模型连接，先到设置页配置")
            if candidate not in allowed:
                raise ApiError(
                    400, "UNKNOWN_MODEL", "只能从个人连接可用的模型里选择，请先在设置页确认模型"
                )
            model = candidate
        else:
            # 空字符串 = 清掉另选的模型，回到个人连接里保存的那个。
            model = None
            clear_model = True
    if clear_model:
        store.clear_model_pref(conn, user_id)
    store.save_prefs(conn, user_id, mode=mode, model=model)
    if mode is not None:
        audit.record(conn, user_id, "prefs.mode", mode)
    return ok(_prefs_payload(conn, user_id))


# --------------------------------------------------------------------------- 轮次


@bp.post("/sessions/<session_id>/turns")
@auth.login_required
def create_turn(session_id: str):
    conn, session, project, user_id = _owned_session(session_id)
    payload = request.get_json(silent=True) or {}
    request_id = str(payload.get("request_id") or "").strip()
    if not REQUEST_ID_RE.match(request_id):
        raise ApiError(400, "BAD_REQUEST_ID", "缺少有效的 request_id（8-64 位字母数字）")
    content = str(payload.get("content") or "").strip()
    if not content:
        raise ApiError(400, "VALIDATION", "消息内容不能为空")
    if len(content) > CONTENT_MAX:
        raise ApiError(400, "VALIDATION", f"单条消息不能超过 {CONTENT_MAX} 个字符")

    existing = store.by_request(conn, session["id"], request_id)
    if existing is not None:
        return ok(
            {
                "turn": store.turn_public(existing),
                "message": None,
                "resumed": True,
                "events": store.events_after(conn, existing["id"], 0),
                "actions": store.latest_actions(conn, existing["id"]),
            }
        )

    active = store.active_for_session(conn, session["id"])
    if active is not None:
        raise conflict(
            "该会话还有一轮没有结束；先等待或停止它", code="TURN_BUSY"
        )

    connection = identity.connection_for(conn, user_id)
    if connection is None:
        raise ApiError(409, "CONNECTION_MISSING", "还没有保存个人模型连接，先在设置页配置")
    if not project["root_path"]:
        raise ApiError(409, "NO_PROJECT_ROOT", "该项目还没有绑定服务器目录，先完成绑定")

    mode = identity.effective_mode(conn, user_id)
    model = identity.effective_model(conn, user_id, connection)
    fingerprint = identity.connection_fingerprint(connection)

    message = sessions.append_message(
        conn,
        session_id=session["id"],
        role="user",
        content=content,
        meta={"source": "web", "request_id": request_id},
    )
    try:
        turn, created = store.create(
            conn,
            session_id=session["id"],
            project_id=project["id"],
            owner_id=user_id,
            request_id=request_id,
            mode=mode,
            provider=str(connection["provider"]),
            model=model,
            fingerprint=fingerprint,
        )
    except Exception:
        conn.execute("DELETE FROM messages WHERE id = ?", (message["id"],))
        conn.commit()
        raise
    if not created:
        return ok({"turn": store.turn_public(turn), "message": None, "resumed": True})

    conn.execute(
        "UPDATE messages SET meta = ? WHERE id = ?",
        (json.dumps({"source": "web", "request_id": request_id, "turn_id": turn["id"]}), message["id"]),
    )
    conn.commit()
    _runtime().runner.start(turn["id"])
    return ok(
        {
            "turn": store.turn_public(turn),
            "message": message,
            "resumed": False,
            "events": [],
            "actions": [],
        }
    )


@bp.get("/sessions/<session_id>/turns")
@auth.login_required
def list_turns(session_id: str):
    conn, session, _project, _user_id = _owned_session(session_id)
    turns = store.recent_for_session(conn, session["id"])
    active = store.active_for_session(conn, session["id"])
    return ok(
        {
            "turns": turns,
            "active": store.turn_public(active) if active is not None else None,
            "actions": store.pending_actions(conn, session["id"]),
        }
    )


@bp.get("/turns/<turn_id>")
@auth.login_required
def get_turn(turn_id: str):
    conn, turn, _user_id = _owned_turn(turn_id)
    after = int(request.args.get("after") or 0)
    return ok(
        {
            "turn": store.turn_public(turn),
            "events": store.events_after(conn, turn["id"], after),
            "actions": store.latest_actions(conn, turn["id"]),
        }
    )


@bp.get("/turns/<turn_id>/events")
@auth.login_required
def get_events(turn_id: str):
    conn, turn, _user_id = _owned_turn(turn_id)
    after = int(request.args.get("after") or 0)
    return ok(
        {
            "turn": store.turn_public(turn),
            "events": store.events_after(conn, turn["id"], after),
            "actions": store.latest_actions(conn, turn["id"]),
        }
    )


@bp.get("/turns/<turn_id>/stream")
@auth.login_required
def stream_events(turn_id: str):
    """SSE 版增量：轮询是界面主路径，这个接口给需要低延迟的客户端用。"""
    conn, turn, _user_id = _owned_turn(turn_id)
    after = int(request.args.get("after") or 0)
    db_path = current_app.config["HX17"].db_path

    @stream_with_context
    def generate():
        cursor = after
        idle = 0
        while True:
            fresh = db.connect(db_path)
            try:
                row = fresh.execute("SELECT status FROM turns WHERE id = ?", (turn["id"],)).fetchone()
                events = store.events_after(fresh, turn["id"], cursor)
            finally:
                fresh.close()
            for event in events:
                cursor = event["seq"]
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            status = row["status"] if row is not None else "failed"
            if status in store.TERMINAL_STATUSES and not events:
                yield f"data: {json.dumps({'type': 'end', 'payload': {'status': status}}, ensure_ascii=False)}\n\n"
                return
            idle = 0 if events else idle + 1
            if idle and idle % 30 == 0:
                yield ": keep-alive\n\n"
            time.sleep(0.5)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@bp.post("/turns/<turn_id>/stop")
@auth.login_required
def stop_turn(turn_id: str):
    conn, turn, user_id = _owned_turn(turn_id)
    if turn["status"] in store.TERMINAL_STATUSES:
        return ok({"stopped": False, "already": True, "turn": store.turn_public(turn)})
    changed = store.request_stop(conn, turn["id"])
    store.append_event(
        conn, turn["id"], turn["session_id"], "notice",
        {"level": "info", "message": "已请求停止，等待引擎确认实际状态…"},
    )
    audit.record(conn, user_id, "agent.turn.stop", turn["id"])
    return ok({"stopped": True, "already": not changed, "turn": store.turn_public(store.get(conn, turn["id"]))})


# --------------------------------------------------------------------------- 动作


@bp.get("/sessions/<session_id>/actions")
@auth.login_required
def list_actions(session_id: str):
    conn, session, _project, _user_id = _owned_session(session_id)
    return ok({"actions": store.pending_actions(conn, session["id"])})


@bp.post("/actions/<action_id>/approve")
@auth.login_required
def approve_action(action_id: str):
    conn = db.get_db()
    user_id = int(_user()["id"])
    action = store.get_action(conn, action_id)
    if action is None or int(action["owner_id"]) != user_id:
        raise not_found("动作")
    if action["status"] == "done":
        return ok({"action": store.action_public(action), "executed": False, "already": True})
    if action["status"] != "proposed":
        raise conflict("该动作已处理或已过期", code="ACTION_NOT_PENDING")

    if not store.claim_action(conn, action["id"]):
        latest = store.get_action(conn, action["id"])
        return ok(
            {
                "action": store.action_public(latest) if latest is not None else None,
                "executed": False,
                "already": True,
            }
        )

    mode = identity.effective_mode(conn, user_id)
    try:
        result = _runtime().gateway.execute_action(conn, action, actor_mode=mode)
    except ToolError as error:
        store.settle_action(
            conn,
            action["id"],
            status="failed",
            error_code=error.code,
            error_message=error.message,
        )
        store.append_event(
            conn, action["turn_id"], action["session_id"], "action",
            {
                "action_id": action["id"],
                "tool": action["tool"],
                "title": action["title"],
                "status": "failed",
                "error": error.as_dict(),
            },
        )
        raise ApiError(error.status, error.code, error.message) from None
    except Exception as exc:  # 未预期异常同样要落到动作状态
        store.settle_action(
            conn, action["id"], status="failed", error_code="ACTION_CRASH",
            error_message=f"执行失败（{type(exc).__name__}）",
        )
        raise ApiError(500, "ACTION_CRASH", "动作执行时发生内部错误，未确认是否已生效") from None

    store.settle_action(conn, action["id"], status="done", result=result)
    store.append_event(
        conn, action["turn_id"], action["session_id"], "action",
        {
            "action_id": action["id"],
            "tool": action["tool"],
            "title": action["title"],
            "status": "done",
            "result": result,
        },
    )
    audit.record(conn, user_id, "agent.action.approve", action["id"], action["tool"])

    continuation = _start_continuation(conn, action, user_id)
    return ok(
        {
            "action": store.action_public(store.get_action(conn, action["id"])),
            "executed": True,
            "result": result,
            "turn": continuation,
        }
    )


def _start_continuation(conn, action, user_id: int) -> dict | None:
    """确认后让助手基于真实结果继续；已有活动轮次时不插队。"""
    session = conn.execute(
        "SELECT * FROM sessions WHERE id = ? AND owner_id = ?", (action["session_id"], user_id)
    ).fetchone()
    if session is None or session["status"] != "active":
        return None
    if store.active_for_session(conn, session["id"]) is not None:
        return None
    project = conn.execute(
        "SELECT * FROM projects WHERE id = ? AND owner_id = ?", (action["project_id"], user_id)
    ).fetchone()
    if project is None or project["status"] != "active":
        return None
    connection = identity.connection_for(conn, user_id)
    if connection is None:
        return None
    request_id = f"resume_{action['id']}"[:64]
    try:
        turn, created = store.create(
            conn,
            session_id=session["id"],
            project_id=project["id"],
            owner_id=user_id,
            request_id=request_id,
            mode=identity.effective_mode(conn, user_id),
            provider=str(connection["provider"]),
            model=identity.effective_model(conn, user_id, connection),
            fingerprint=identity.connection_fingerprint(connection),
            kind="continuation",
        )
    except Exception:
        return None
    if not created:
        return store.turn_public(turn)
    _runtime().runner.start(turn["id"])
    return store.turn_public(turn)


@bp.post("/actions/<action_id>/reject")
@auth.login_required
def reject_action(action_id: str):
    conn = db.get_db()
    user_id = int(_user()["id"])
    action = store.get_action(conn, action_id)
    if action is None or int(action["owner_id"]) != user_id:
        raise not_found("动作")
    if not store.reject_action(conn, action["id"]):
        latest = store.get_action(conn, action["id"])
        return ok({"action": store.action_public(latest) if latest is not None else None, "rejected": False})
    store.append_event(
        conn, action["turn_id"], action["session_id"], "action",
        {
            "action_id": action["id"],
            "tool": action["tool"],
            "title": action["title"],
            "status": "rejected",
        },
    )
    audit.record(conn, user_id, "agent.action.reject", action["id"], action["tool"])
    return ok({"action": store.action_public(store.get_action(conn, action["id"])), "rejected": True})


@bp.get("/tool-jobs/<job_id>")
@auth.login_required
def get_job(job_id: str):
    conn = db.get_db()
    user_id = int(_user()["id"])
    row = conn.execute(
        "SELECT * FROM tool_jobs WHERE id = ? AND owner_id = ?", (job_id, user_id)
    ).fetchone()
    if row is None:
        raise not_found("工具任务")
    return ok({"job": store.job_public(row)})

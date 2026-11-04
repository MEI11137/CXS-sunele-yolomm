"""个人模型连接接口。

响应里永远只有掩码 Key；真正的明文只在 ``services.connections.server_credentials``
里给服务端内部使用，不经过任何路由。
"""

from __future__ import annotations

from flask import Blueprint, current_app, request

from backend import auth, db
from backend.errors import ok
from backend.services import audit, connections

bp = Blueprint("connections", __name__)


def _user() -> dict:
    user = auth.current_user()
    assert user is not None
    return user


def _key() -> bytes:
    return current_app.config["HX17_CONNECTION_KEY"]


@bp.get("/connections/providers")
@auth.login_required
def list_providers():
    return ok({"providers": connections.PROVIDERS, "probe_timeout": connections.PROBE_TIMEOUT})


@bp.get("/connections/me")
@auth.login_required
def get_connection():
    user = _user()
    data = connections.view(db.get_db(), int(user["id"]))
    return ok({"connection": data})


@bp.put("/connections/me")
@auth.login_required
def save_connection():
    conn = db.get_db()
    user = _user()
    payload = request.get_json(silent=True) or {}
    data = connections.save(conn, int(user["id"]), payload, key=_key())
    audit.record(
        conn,
        int(user["id"]),
        "connection.save",
        f"{data.get('provider')}/{data.get('model')}",
        "key_changed" if payload.get("api_key") else "key_kept",
    )
    return ok({"connection": data})


@bp.delete("/connections/me")
@auth.login_required
def delete_connection():
    conn = db.get_db()
    user = _user()
    removed = connections.delete(conn, int(user["id"]))
    if removed:
        audit.record(conn, int(user["id"]), "connection.delete", str(user["username"]))
    return ok({"removed": removed})


@bp.post("/connections/me/check")
@auth.login_required
def check_connection():
    conn = db.get_db()
    user = _user()
    data = connections.check(conn, int(user["id"]), key=_key())
    # 审计只记状态与能力名，绝不记 Key、上游正文或请求头。
    audit.record(
        conn,
        int(user["id"]),
        "connection.check",
        f"{data.get('model')}",
        "auth_state={state}; applied={applied}; caps={caps}".format(
            state=data.get("auth_state", "unknown"),
            applied=data.get("applied", False),
            caps=",".join(
                key for key in ("chat", "vision", "tools") if data["capabilities"].get(key)
            )
            or "-",
        ),
    )
    return ok({"connection": data})

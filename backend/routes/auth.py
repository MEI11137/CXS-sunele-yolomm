"""登录、登出、当前用户、改密。

没有注册接口：账号只能由本机 CLI 或管理员创建。
"""

from __future__ import annotations

import threading
import time

from flask import Blueprint, current_app, request

from backend import auth, db, security
from backend.errors import ApiError, forbidden, ok, unauthorized
from backend.services import audit, users

bp = Blueprint("auth", __name__)

_MAX_FAILURES = 8
_FAILURE_WINDOW = 300.0


class LoginThrottle:
    """按 IP + 用户名计数的登录失败节流。

    实例挂在应用上（而不是模块级），多个应用/测试之间互不影响；进程重启即清空，
    只用于本机防暴力猜测，不需要持久化。
    """

    def __init__(self) -> None:
        self._items: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            stamps = [value for value in self._items.get(key, []) if now - value < _FAILURE_WINDOW]
            self._items[key] = stamps
            blocked = len(stamps) >= _MAX_FAILURES
        if blocked:
            raise ApiError(
                429, "TOO_MANY_ATTEMPTS", "登录失败次数过多，请稍后再试（5 分钟后自动解除）"
            )

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._items.setdefault(key, []).append(time.monotonic())

    def clear(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)


def _throttle() -> LoginThrottle:
    store = current_app.extensions.get("hx17_login_throttle")
    if store is None:
        store = LoginThrottle()
        current_app.extensions["hx17_login_throttle"] = store
    return store


def _throttle_key(username: str) -> str:
    return f"{request.remote_addr or '?'}|{username.lower()}"


@bp.post("/auth/login")
def login():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        raise ApiError(400, "VALIDATION", "请填写用户名和密码")

    key = _throttle_key(username)
    throttle = _throttle()
    throttle.check(key)

    conn = db.get_db()
    row = users.find_by_username(conn, username)
    # 账号不存在也跑一次哈希，响应时间不泄露账号是否存在。
    matched = security.verify_password_or_dummy(
        password, row["password_hash"] if row is not None else None
    )
    if row is None or not matched:
        throttle.record_failure(key)
        raise unauthorized("用户名或密码不正确", code="AUTH_INVALID")
    if row["status"] != "active":
        raise forbidden("该账号已被停用，请联系管理员", code="ACCOUNT_DISABLED")

    throttle.clear(key)
    token = auth.issue_session(conn, row, client_hint=(request.user_agent.string or "")[:120])
    response = ok({"user": users.public(row)})
    auth.set_session_cookies(response, token)
    audit.record(conn, row["id"], "auth.login", row["username"])
    return response


@bp.post("/auth/logout")
@auth.login_required
def logout():
    conn = db.get_db()
    sid = auth.session_id()
    auth.revoke_session(conn, sid)
    response = ok({"logged_out": True})
    auth.clear_cookies(response)
    return response


@bp.get("/auth/me")
@auth.login_required
def me():
    user = auth.current_user()
    assert user is not None
    return ok({"user": users.public(user)})


@bp.post("/auth/password")
@auth.login_required
def change_password():
    payload = request.get_json(silent=True) or {}
    current = str(payload.get("current_password") or "")
    new_password = str(payload.get("new_password") or "")
    if not current or not new_password:
        raise ApiError(400, "VALIDATION", "请填写当前密码与新密码")
    conn = db.get_db()
    user = auth.current_user()
    assert user is not None
    sid = auth.session_id()
    # 只保留当前会话，其它设备的令牌全部失效。
    users.change_password(
        conn,
        int(user["id"]),
        current_password=current,
        new_password=new_password,
        keep_session_id=sid,
    )
    refreshed = users.get(conn, int(user["id"]))
    token = auth.issue_session(conn, refreshed, client_hint=(request.user_agent.string or "")[:120])
    response = ok({"user": users.public(refreshed)})
    auth.set_session_cookies(response, token)
    audit.record(conn, int(user["id"]), "auth.password_change", user["username"])
    return response

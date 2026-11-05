"""登录态与跨站防护。

* 令牌放在 HttpOnly Cookie 里，前端拿不到明文，XSS 也偷不走；
* 令牌是 HMAC-SHA256 自签串，但**是否有效以服务端会话表为准**：登出、停用、改密
  都会立刻吊销，不依赖客户端删除 Cookie；
* 所有写操作要求 ``X-CSRF-Token`` 与同名 Cookie 双提交一致，并校验 Origin；
* Cookie ``SameSite=Lax`` + ``Secure``（HTTPS 入口下由配置打开）。
"""

from __future__ import annotations

import hmac
from functools import wraps
from typing import Any, Callable
from urllib.parse import urlsplit

from flask import Flask, Response, current_app, g, request

from backend import db, security
from backend.errors import ApiError, forbidden, unauthorized

SESSION_COOKIE = "hx17_session"
CSRF_COOKIE = "hx17_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# 只有这一个已鉴权路由接受 multipart（文件上传）；其它写接口仍然必须是 JSON。
# 用 endpoint（蓝图.视图）而不是路径前缀，避免以后新增路由被顺带放宽。
MULTIPART_ENDPOINTS = {"assets.upload_asset"}


def _settings():
    return current_app.config["HX17"]


def _key() -> bytes:
    return current_app.config["HX17_SESSION_KEY"]


# --------------------------------------------------------------------------- 会话


def issue_session(conn, user, *, client_hint: str = "") -> str:
    settings = _settings()
    sid = security.new_token(24)
    now_epoch = security.now_epoch()
    expires_at = now_epoch + settings.session_ttl_hours * 3600
    # 顺手清掉过期记录，避免会话表无限增长。
    conn.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (now_epoch - 86400,))
    conn.execute(
        """
        INSERT INTO auth_sessions (id, user_id, created_at, expires_at, client_hint)
        VALUES (?, ?, ?, ?, ?)
        """,
        (sid, int(user["id"]), security.now_iso(), expires_at, client_hint[:120]),
    )
    conn.commit()
    return security.sign_token(
        {
            "sid": sid,
            "sub": int(user["id"]),
            "tv": int(user["token_version"]),
            "iat": now_epoch,
            "exp": expires_at,
        },
        _key(),
    )


def revoke_session(conn, sid: str | None) -> None:
    if not sid:
        return
    conn.execute(
        "UPDATE auth_sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (security.now_iso(), sid),
    )
    conn.commit()


def current_user() -> dict | None:
    if "auth_user" in g:
        return g.auth_user
    g.auth_user = _resolve_user()
    return g.auth_user


def _resolve_user() -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    payload = security.verify_token(token, _key())
    if not payload:
        return None
    sid = payload.get("sid")
    sub = payload.get("sub")
    token_version = payload.get("tv")
    if not isinstance(sid, str) or not isinstance(sub, int) or not isinstance(token_version, int):
        return None
    row = db.get_db().execute(
        """
        SELECT u.*, s.id AS session_id
          FROM auth_sessions s
          JOIN users u ON u.id = s.user_id
         WHERE s.id = ? AND s.revoked_at IS NULL AND s.expires_at > ?
        """,
        (sid, security.now_epoch()),
    ).fetchone()
    if row is None:
        return None
    # 停用与改密都会让 token_version 前进，旧令牌随之失效。
    if row["status"] != "active" or int(row["token_version"]) != token_version:
        return None
    g.session_id = sid
    return dict(row)


def session_id() -> str | None:
    return getattr(g, "session_id", None)


def set_session_cookies(response: Response, token: str) -> None:
    settings = _settings()
    max_age = settings.session_ttl_hours * 3600
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        samesite="Lax",
        secure=settings.cookie_secure,
        path="/",
    )
    # 登录成功轮换 CSRF 令牌，避免登录前的值被继续使用。
    response.set_cookie(
        CSRF_COOKIE,
        security.new_token(),
        max_age=max_age,
        httponly=False,
        samesite="Lax",
        secure=settings.cookie_secure,
        path="/",
    )


def clear_cookies(response: Response) -> None:
    settings = _settings()
    response.delete_cookie(SESSION_COOKIE, path="/", secure=settings.cookie_secure)
    response.delete_cookie(CSRF_COOKIE, path="/", secure=settings.cookie_secure)


# --------------------------------------------------------------------------- 装饰器


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        user = current_user()
        if user is None:
            raise unauthorized()
        return view(*args, **kwargs)

    return wrapper


def admin_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        user = current_user()
        if user is None:
            raise unauthorized()
        if user["role"] != "admin":
            raise forbidden("仅本机管理员可以执行该操作")
        return view(*args, **kwargs)

    return wrapper


# --------------------------------------------------------------------------- 请求钩子


def _same_origin(origin: str) -> bool:
    parsed = urlsplit(origin)
    return bool(parsed.netloc) and parsed.netloc.lower() == (request.host or "").lower()


def _guard_state_changing() -> None:
    # 只收 JSON：简单的跨站表单无法伪造 Content-Type，天然挡掉一类 CSRF。
    # 文件上传是唯一例外，且仍然要过后面的 CSRF 双提交与 Origin 校验。
    if request.content_length and not request.is_json:
        allowed = request.mimetype == "multipart/form-data" and request.endpoint in MULTIPART_ENDPOINTS
        if not allowed:
            raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "请求体必须是 application/json")

    if request.cookies.get(SESSION_COOKIE):
        expected = request.cookies.get(CSRF_COOKIE) or ""
        supplied = request.headers.get("X-CSRF-Token") or ""
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            raise ApiError(403, "CSRF_FAILED", "缺少或错误的 CSRF 令牌，请刷新页面后重试")

    origin = request.headers.get("Origin")
    if origin and not _same_origin(origin):
        raise ApiError(403, "CSRF_ORIGIN", "请求来源与站点不一致，已拒绝写入")


def init_app(app: Flask) -> None:
    @app.before_request
    def _before() -> None:
        if request.method in SAFE_METHODS:
            if not request.cookies.get(CSRF_COOKIE):
                g.new_csrf_token = security.new_token()
            return
        _guard_state_changing()

    @app.after_request
    def _after(response: Response) -> Response:
        token = getattr(g, "new_csrf_token", None)
        if token:
            settings = _settings()
            response.set_cookie(
                CSRF_COOKIE,
                token,
                max_age=settings.session_ttl_hours * 3600,
                httponly=False,
                samesite="Lax",
                secure=settings.cookie_secure,
                path="/",
            )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

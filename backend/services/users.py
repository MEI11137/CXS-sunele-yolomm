"""账号：创建、启停、改密、会话吊销。

全部通过本机 CLI 或管理员接口调用，没有公开注册入口，也不存在默认账号。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from backend import security
from backend.errors import ApiError, conflict, not_found

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128
ROLES = ("admin", "member")
STATUSES = ("active", "disabled")

PUBLIC_FIELDS = ("id", "username", "display_name", "role", "status", "created_at", "updated_at")


def normalize_username(raw: Any) -> str:
    username = str(raw or "").strip().lower()
    if not USERNAME_RE.match(username):
        raise ApiError(
            400,
            "BAD_USERNAME",
            "用户名需 3-32 位，只能用小写字母、数字、点、下划线或连字符，且以字母或数字开头",
        )
    return username


def validate_password(password: Any, *, username: str | None = None) -> str:
    text = str(password or "")
    if len(text) < PASSWORD_MIN_LENGTH:
        raise ApiError(400, "WEAK_PASSWORD", f"密码至少 {PASSWORD_MIN_LENGTH} 位")
    if len(text) > PASSWORD_MAX_LENGTH:
        raise ApiError(400, "WEAK_PASSWORD", f"密码不能超过 {PASSWORD_MAX_LENGTH} 位")
    if text.strip() != text:
        raise ApiError(400, "WEAK_PASSWORD", "密码首尾不能是空白字符")
    if username and text.lower() == username.lower():
        raise ApiError(400, "WEAK_PASSWORD", "密码不能与用户名相同")
    if len(set(text)) == 1:
        raise ApiError(400, "WEAK_PASSWORD", "密码不能是同一个字符重复")
    return text


def public(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    return {key: data[key] for key in PUBLIC_FIELDS if key in data}


def count_users(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def get(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
    if row is None:
        raise not_found("账号")
    return row


def find_by_username(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (str(username).strip(),)
    ).fetchone()


def create(
    conn: sqlite3.Connection,
    *,
    username: str,
    password: str,
    display_name: str | None = None,
    role: str = "member",
) -> sqlite3.Row:
    if role not in ROLES:
        raise ApiError(400, "BAD_ROLE", "角色只能是 admin 或 member")
    name = normalize_username(username)
    validate_password(password, username=name)
    if find_by_username(conn, name) is not None:
        raise conflict("该用户名已被占用", code="USERNAME_TAKEN")
    now = security.now_iso()
    label = (display_name or "").strip()[:64] or name
    cursor = conn.execute(
        """
        INSERT INTO users (username, display_name, password_hash, role, status,
                           token_version, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'active', 1, ?, ?)
        """,
        (name, label, security.hash_password(password), role, now, now),
    )
    conn.commit()
    return get(conn, int(cursor.lastrowid))


def list_all(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM users ORDER BY role = 'admin' DESC, id ASC"
    ).fetchall()
    return [public(row) for row in rows]


def set_display_name(conn: sqlite3.Connection, user_id: int, display_name: str) -> sqlite3.Row:
    label = str(display_name or "").strip()[:64]
    if not label:
        raise ApiError(400, "VALIDATION", "显示名不能为空")
    conn.execute(
        "UPDATE users SET display_name = ?, updated_at = ? WHERE id = ?",
        (label, security.now_iso(), int(user_id)),
    )
    conn.commit()
    return get(conn, user_id)


def revoke_sessions(
    conn: sqlite3.Connection, user_id: int, *, keep_session_id: str | None = None
) -> int:
    now = security.now_iso()
    if keep_session_id:
        cursor = conn.execute(
            "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL AND id <> ?",
            (now, int(user_id), keep_session_id),
        )
    else:
        cursor = conn.execute(
            "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (now, int(user_id)),
        )
    conn.commit()
    return cursor.rowcount


def _bump_token_version(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute(
        "UPDATE users SET token_version = token_version + 1, updated_at = ? WHERE id = ?",
        (security.now_iso(), int(user_id)),
    )


def set_status(conn: sqlite3.Connection, user_id: int, status: str) -> sqlite3.Row:
    if status not in STATUSES:
        raise ApiError(400, "BAD_STATUS", "状态只能是 active 或 disabled")
    row = get(conn, user_id)
    if row["status"] == status:
        return row
    if status == "disabled":
        # 停用立刻生效：吊销全部会话并让已签发令牌的版本号失效。
        _bump_token_version(conn, user_id)
        revoke_sessions(conn, user_id)
    conn.execute(
        "UPDATE users SET status = ?, updated_at = ? WHERE id = ?",
        (status, security.now_iso(), int(user_id)),
    )
    conn.commit()
    return get(conn, user_id)


def reset_password(conn: sqlite3.Connection, user_id: int, new_password: str) -> None:
    row = get(conn, user_id)
    validate_password(new_password, username=row["username"])
    _bump_token_version(conn, user_id)
    revoke_sessions(conn, user_id)
    conn.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        (security.hash_password(new_password), security.now_iso(), int(user_id)),
    )
    conn.commit()


def change_password(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    current_password: str,
    new_password: str,
    keep_session_id: str | None = None,
) -> None:
    row = get(conn, user_id)
    if not security.verify_password(current_password, row["password_hash"]):
        raise ApiError(400, "WRONG_PASSWORD", "当前密码不正确")
    validate_password(new_password, username=row["username"])
    if security.verify_password(new_password, row["password_hash"]):
        raise ApiError(400, "SAME_PASSWORD", "新密码不能与当前密码相同")
    _bump_token_version(conn, user_id)
    revoke_sessions(conn, user_id, keep_session_id=keep_session_id)
    conn.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        (security.hash_password(new_password), security.now_iso(), int(user_id)),
    )
    conn.commit()

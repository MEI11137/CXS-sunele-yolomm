"""会话与消息。

会话属于项目与用户，两者必须同时匹配；软删除后可恢复。
WB17-01 只保存用户消息，**不生成助手回复**：内部 Agent 运行时在 WB17-02 接入。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from backend import security
from backend.errors import ApiError, conflict, not_found

TITLE_MAX = 80
CONTENT_MAX = 8000
DEFAULT_TITLE = "新会话"


def public(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "project_id": data["project_id"],
        "title": data["title"],
        "status": data["status"],
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "deleted_at": data["deleted_at"],
    }


def _validate_title(raw: Any, *, fallback: str = DEFAULT_TITLE) -> str:
    title = str(raw or "").strip() or fallback
    if len(title) > TITLE_MAX:
        raise ApiError(400, "VALIDATION", f"会话名称不能超过 {TITLE_MAX} 个字符")
    return title


def create(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    owner_id: int,
    title: str | None = None,
) -> sqlite3.Row:
    now = security.now_iso()
    session_id = security.new_id("ses")
    conn.execute(
        """
        INSERT INTO sessions (id, project_id, owner_id, title, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'active', ?, ?)
        """,
        (session_id, project_id, int(owner_id), _validate_title(title), now, now),
    )
    conn.commit()
    return get_owned(conn, session_id, owner_id)


def list_for(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    owner_id: int,
    include_deleted: bool = False,
) -> list[dict]:
    status = "deleted" if include_deleted else "active"
    rows = conn.execute(
        """
        SELECT * FROM sessions
        WHERE project_id = ? AND owner_id = ? AND status = ?
        ORDER BY updated_at DESC, created_at DESC
        """,
        (str(project_id), int(owner_id), status),
    ).fetchall()
    return [public(row) for row in rows]


def get_owned(
    conn: sqlite3.Connection,
    session_id: str,
    owner_id: int,
    *,
    include_deleted: bool = False,
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ? AND owner_id = ?",
        (str(session_id), int(owner_id)),
    ).fetchone()
    if row is None:
        raise not_found("会话")
    if row["status"] == "deleted" and not include_deleted:
        raise not_found("会话")
    return row


def rename(conn: sqlite3.Connection, session: sqlite3.Row, title: str) -> sqlite3.Row:
    label = _validate_title(title, fallback=session["title"])
    conn.execute(
        "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
        (label, security.now_iso(), session["id"]),
    )
    conn.commit()
    return get_owned(conn, session["id"], session["owner_id"])


def soft_delete(conn: sqlite3.Connection, session: sqlite3.Row) -> None:
    now = security.now_iso()
    conn.execute(
        "UPDATE sessions SET status = 'deleted', deleted_at = ?, updated_at = ? WHERE id = ?",
        (now, now, session["id"]),
    )
    conn.commit()


def restore(conn: sqlite3.Connection, session: sqlite3.Row) -> sqlite3.Row:
    if session["status"] != "deleted":
        raise conflict("该会话未处于回收站", code="NOT_DELETED")
    conn.execute(
        "UPDATE sessions SET status = 'active', deleted_at = NULL, updated_at = ? WHERE id = ?",
        (security.now_iso(), session["id"]),
    )
    conn.commit()
    return get_owned(conn, session["id"], session["owner_id"])


def message_public(row: sqlite3.Row) -> dict:
    data = dict(row)
    try:
        meta = json.loads(data.get("meta") or "{}")
    except ValueError:
        meta = {}
    return {
        "id": data["id"],
        "session_id": data["session_id"],
        "role": data["role"],
        "content": data["content"],
        "meta": meta if isinstance(meta, dict) else {},
        "created_at": data["created_at"],
    }


def list_messages(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    limit: int = 100,
    before: int | None = None,
) -> dict:
    """按“最近一页 + 向前翻页”读取消息。

    旧实现是 ``ORDER BY id ASC LIMIT 200``：对话超过 200 条后，**所有新消息**
    （包括助手最终回答）都会落在窗口之外，历史接口永远看不到。
    这里改成取最后 N 条（按 id 倒序再反转），并用 ``before=<id>`` 向前翻。
    """
    cap = max(1, min(int(limit), 200))
    params: list = [str(session_id)]
    clause = "session_id = ?"
    if before is not None:
        clause += " AND id < ?"
        params.append(int(before))
    params.append(cap + 1)
    rows = conn.execute(
        f"SELECT * FROM messages WHERE {clause} ORDER BY id DESC LIMIT ?", params
    ).fetchall()
    has_more = len(rows) > cap
    page = rows[:cap]
    page.reverse()
    return {
        "messages": [message_public(row) for row in page],
        "has_more": has_more,
        "next_before": (int(page[0]["id"]) if has_more and page else None),
    }


def append_message(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    role: str,
    content: str,
    meta: dict | None = None,
    limit: int = CONTENT_MAX,
) -> dict:
    if role not in {"user", "assistant", "system"}:
        raise ApiError(400, "BAD_ROLE", "消息角色不合法")
    text = str(content or "").strip()
    if not text:
        raise ApiError(400, "VALIDATION", "消息内容不能为空")
    # 用户输入上限 8000；助手最终回答允许更长（由调用方传入 limit）。
    cap = max(1, int(limit))
    if len(text) > cap:
        if role == "user":
            raise ApiError(400, "VALIDATION", f"单条消息不能超过 {cap} 个字符")
        text = text[:cap]
    now = security.now_iso()
    cursor = conn.execute(
        "INSERT INTO messages (session_id, role, content, meta, created_at) VALUES (?, ?, ?, ?, ?)",
        (str(session_id), role, text, json.dumps(meta or {}, ensure_ascii=False), now),
    )
    # 会话列表按 updated_at 排序，有新消息就把会话顶上去。
    conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, str(session_id)))
    conn.commit()
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return message_public(row)

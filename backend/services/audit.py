"""操作审计：目录绑定、账号状态、连接变更都留痕，便于解释“权限何时被扩大”。"""

from __future__ import annotations

import sqlite3

from backend.security import now_iso


def record(
    conn: sqlite3.Connection,
    actor_id: int | None,
    action: str,
    target: str = "",
    detail: str = "",
) -> None:
    conn.execute(
        "INSERT INTO audit_log (at, actor_id, action, target, detail) VALUES (?, ?, ?, ?, ?)",
        (now_iso(), actor_id, action, target[:200], detail[:500]),
    )
    conn.commit()


def recent(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT id, at, actor_id, action, target, detail FROM audit_log ORDER BY id DESC LIMIT ?",
        (max(1, min(int(limit), 200)),),
    ).fetchall()
    return [dict(row) for row in rows]

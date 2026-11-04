"""本机允许根目录，以及“哪个账号能用哪个根”的授予关系。

规则：

* ``allowed_roots`` 是管理员认定的服务器目录白名单；
* ``root_grants`` 决定某个账号能用其中哪几个 —— **默认不共享**，必须显式授予；
* 普通用户的列表 / 浏览 / 绑定都只基于自己的授予，看不到别人的目录；
* 管理员可把同一个根授予多人；撤销后立即生效，已绑定的项目也会被重新校验；
* 迁移 v2 只建关系表，不会把历史全局授权自动发给任何账号。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from backend import paths, security
from backend.errors import ApiError, conflict, not_found
from backend.services import audit, users

LABEL_MAX = 60


def public(row: sqlite3.Row | dict, *, grants: list[dict] | None = None) -> dict:
    data = dict(row)
    result = {
        "id": data["id"],
        "path": data["path"],
        "label": data["label"],
        "created_at": data["created_at"],
    }
    if grants is not None:
        result["grants"] = grants
    return result


def list_all(conn: sqlite3.Connection) -> list[dict]:
    """全部授权根（管理员与本机 CLI 用，不代表普通用户可见）。"""
    rows = conn.execute("SELECT * FROM allowed_roots ORDER BY path").fetchall()
    return [dict(row) for row in rows]


def list_for_user(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """当前账号获授的根目录，按路径排序。"""
    rows = conn.execute(
        """
        SELECT r.* FROM allowed_roots r
          JOIN root_grants g ON g.root_id = r.id
         WHERE g.user_id = ?
         ORDER BY r.path
        """,
        (int(user_id),),
    ).fetchall()
    return [dict(row) for row in rows]


def paths_for_user(conn: sqlite3.Connection, user_id: int) -> list[Path]:
    return [Path(item["path"]) for item in list_for_user(conn, user_id)]


def get(conn: sqlite3.Connection, root_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM allowed_roots WHERE id = ?", (int(root_id),)).fetchone()
    if row is None:
        raise not_found("授权目录")
    return row


def find_by_path(conn: sqlite3.Connection, raw_path: str) -> sqlite3.Row | None:
    wanted = os.path.normcase(paths.canonical(raw_path))
    for row in list_all(conn):
        if os.path.normcase(paths.canonical(row["path"])) == wanted:
            return conn.execute(
                "SELECT * FROM allowed_roots WHERE id = ?", (int(row["id"]),)
            ).fetchone()
    return None


# --------------------------------------------------------------------------- 授予


def grants_of_root(conn: sqlite3.Connection, root_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT g.user_id, u.username, u.display_name, g.granted_at
          FROM root_grants g
          JOIN users u ON u.id = g.user_id
         WHERE g.root_id = ?
         ORDER BY u.username
        """,
        (int(root_id),),
    ).fetchall()
    return [dict(row) for row in rows]


def grant(
    conn: sqlite3.Connection, *, actor_id: int | None, root_id: int, user_id: int
) -> None:
    row = get(conn, root_id)
    target = users.get(conn, user_id)
    exists = conn.execute(
        "SELECT 1 FROM root_grants WHERE root_id = ? AND user_id = ?",
        (int(root_id), int(user_id)),
    ).fetchone()
    if exists is not None:
        return
    conn.execute(
        """
        INSERT INTO root_grants (root_id, user_id, granted_by, granted_at)
        VALUES (?, ?, ?, ?)
        """,
        (int(root_id), int(user_id), actor_id, security.now_iso()),
    )
    conn.commit()
    audit.record(conn, actor_id, "root.grant", row["path"], str(target["username"]))


def grant_by_username(
    conn: sqlite3.Connection, *, actor_id: int | None, root_id: int, username: str
) -> None:
    target = users.find_by_username(conn, username)
    if target is None:
        raise not_found("账号")
    grant(conn, actor_id=actor_id, root_id=root_id, user_id=int(target["id"]))


def revoke(
    conn: sqlite3.Connection, *, actor_id: int | None, root_id: int, user_id: int
) -> bool:
    row = get(conn, root_id)
    target = users.get(conn, user_id)
    cursor = conn.execute(
        "DELETE FROM root_grants WHERE root_id = ? AND user_id = ?",
        (int(root_id), int(user_id)),
    )
    conn.commit()
    if cursor.rowcount:
        audit.record(conn, actor_id, "root.revoke", row["path"], str(target["username"]))
        return True
    return False


# --------------------------------------------------------------------------- 增删


def add(
    conn: sqlite3.Connection, *, actor_id: int | None, raw_path: str, label: str = ""
) -> sqlite3.Row:
    real = paths.validate_root(raw_path)
    wanted = os.path.normcase(str(real))
    existing_rows = list_all(conn)
    for existing in existing_rows:
        if os.path.normcase(existing["path"]) == wanted:
            raise conflict("该目录已在授权列表中", code="ROOT_EXISTS")
    # 嵌套范围提示：不阻止，但要让管理员知道自己加了一个更宽的入口。
    notes = [
        existing["path"]
        for existing in existing_rows
        if paths.is_within(real, existing["path"]) or paths.is_within(existing["path"], real)
    ]
    cursor = conn.execute(
        "INSERT INTO allowed_roots (path, label, created_by, created_at) VALUES (?, ?, ?, ?)",
        (str(real), str(label or "").strip()[:LABEL_MAX], actor_id, security.now_iso()),
    )
    conn.commit()
    audit.record(
        conn,
        actor_id,
        "root.add",
        str(real),
        f"overlap={notes}" if notes else "",
    )
    row = conn.execute("SELECT * FROM allowed_roots WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return row


def remove(conn: sqlite3.Connection, *, actor_id: int | None, root_id: int) -> None:
    row = get(conn, root_id)
    root = row["path"]
    # 还有项目绑定在该目录内时不允许删除，否则这些项目会留在授权范围之外。
    blocking = 0
    for project in conn.execute(
        "SELECT root_path FROM projects WHERE status = 'active' AND root_path IS NOT NULL"
    ).fetchall():
        if paths.is_within(project["root_path"], root):
            blocking += 1
    if blocking:
        raise conflict(
            f"仍有 {blocking} 个项目绑定在该目录内，请先解除绑定再移除授权",
            code="ROOT_IN_USE",
        )
    # 显式清掉授予；外键 ON DELETE CASCADE 只是兜底。
    conn.execute("DELETE FROM root_grants WHERE root_id = ?", (int(root_id),))
    conn.execute("DELETE FROM allowed_roots WHERE id = ?", (int(root_id),))
    conn.commit()
    audit.record(conn, actor_id, "root.remove", root)


def assert_granted(
    conn: sqlite3.Connection, user_id: int, raw_path: str, *, code: str = "GRANT_REVOKED"
) -> None:
    """已绑定的目录也要按当前授予重新校验（撤销后立即拒绝）。"""
    if not any(paths.is_within(raw_path, item["path"]) for item in list_for_user(conn, user_id)):
        raise ApiError(
            403,
            code,
            "该目录的授权已被管理员收回，请重新选择目录或联系管理员",
        )

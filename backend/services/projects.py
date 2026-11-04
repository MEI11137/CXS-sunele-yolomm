"""项目：CRUD、软删除/恢复、目录绑定。

规则：

* 所有读写都带 ``owner_id`` 条件，别人的项目一律按“不存在”处理（不泄露存在性）；
* 目录绑定只能落在**当前账号获授**的根目录内，绑定时做真实路径校验；已绑定的目录
  每次操作都重新校验授予，撤销后立即拒绝；
* 替换已有绑定必须显式 ``replace=True``，否则 409，避免界面误操作把项目悄悄指向别处。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from backend import paths, security
from backend.errors import ApiError, conflict, not_found
from backend.services import audit, roots

NAME_MAX = 80
DESCRIPTION_MAX = 400


def public(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "name": data["name"],
        "description": data["description"],
        "root_path": data["root_path"],
        "root_bound_at": data["root_bound_at"],
        "status": data["status"],
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "deleted_at": data["deleted_at"],
    }


def _validate_name(raw: Any) -> str:
    name = str(raw or "").strip()
    if not name:
        raise ApiError(400, "VALIDATION", "项目名称不能为空")
    if len(name) > NAME_MAX:
        raise ApiError(400, "VALIDATION", f"项目名称不能超过 {NAME_MAX} 个字符")
    return name


def create(
    conn: sqlite3.Connection, owner_id: int, *, name: str, description: str = ""
) -> sqlite3.Row:
    label = _validate_name(name)
    desc = str(description or "").strip()
    if len(desc) > DESCRIPTION_MAX:
        raise ApiError(400, "VALIDATION", f"项目说明不能超过 {DESCRIPTION_MAX} 个字符")
    now = security.now_iso()
    project_id = security.new_id("prj")
    conn.execute(
        """
        INSERT INTO projects (id, owner_id, name, description, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'active', ?, ?)
        """,
        (project_id, int(owner_id), label, desc, now, now),
    )
    conn.commit()
    audit.record(conn, owner_id, "project.create", project_id, label)
    return get_owned(conn, project_id, owner_id)


def list_for(
    conn: sqlite3.Connection, owner_id: int, *, include_deleted: bool = False
) -> list[dict]:
    status = "deleted" if include_deleted else "active"
    rows = conn.execute(
        "SELECT * FROM projects WHERE owner_id = ? AND status = ? ORDER BY created_at DESC",
        (int(owner_id), status),
    ).fetchall()
    return [public(row) for row in rows]


def get_owned(
    conn: sqlite3.Connection,
    project_id: str,
    owner_id: int,
    *,
    include_deleted: bool = False,
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM projects WHERE id = ? AND owner_id = ?",
        (str(project_id), int(owner_id)),
    ).fetchone()
    # 别人的项目与不存在的项目返回同一个错误，避免探测他人项目 ID。
    if row is None:
        raise not_found("项目")
    if row["status"] == "deleted" and not include_deleted:
        raise not_found("项目")
    return row


def update(
    conn: sqlite3.Connection,
    project: sqlite3.Row,
    *,
    name: str | None = None,
    description: str | None = None,
) -> sqlite3.Row:
    fields: list[str] = []
    params: list[Any] = []
    if name is not None:
        fields.append("name = ?")
        params.append(_validate_name(name))
    if description is not None:
        desc = str(description).strip()
        if len(desc) > DESCRIPTION_MAX:
            raise ApiError(400, "VALIDATION", f"项目说明不能超过 {DESCRIPTION_MAX} 个字符")
        fields.append("description = ?")
        params.append(desc)
    if not fields:
        return project
    fields.append("updated_at = ?")
    params.append(security.now_iso())
    params.extend([project["id"], project["owner_id"]])
    conn.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id = ? AND owner_id = ?", params)
    conn.commit()
    return get_owned(conn, project["id"], project["owner_id"], include_deleted=True)


def soft_delete(conn: sqlite3.Connection, project: sqlite3.Row) -> None:
    now = security.now_iso()
    conn.execute(
        "UPDATE projects SET status = 'deleted', deleted_at = ?, updated_at = ? WHERE id = ?",
        (now, now, project["id"]),
    )
    conn.commit()
    audit.record(conn, project["owner_id"], "project.delete", project["id"])


def restore(conn: sqlite3.Connection, project: sqlite3.Row) -> sqlite3.Row:
    if project["status"] != "deleted":
        raise conflict("该项目未处于回收站", code="NOT_DELETED")
    conn.execute(
        "UPDATE projects SET status = 'active', deleted_at = NULL, updated_at = ? WHERE id = ?",
        (security.now_iso(), project["id"]),
    )
    conn.commit()
    audit.record(conn, project["owner_id"], "project.restore", project["id"])
    return get_owned(conn, project["id"], project["owner_id"])


def allowed_roots(conn: sqlite3.Connection, owner_id: int) -> list[dict]:
    """该项目所属账号当前获授的根目录（不是全站列表）。"""
    return roots.list_for_user(conn, int(owner_id))


def granted_paths(conn: sqlite3.Connection, owner_id: int) -> list[Path]:
    return [Path(item["path"]) for item in allowed_roots(conn, owner_id)]


def assert_bound_root_granted(conn: sqlite3.Connection, project: sqlite3.Row) -> Path:
    """已绑定目录必须仍在当前账号的授予范围内；撤销后立即拒绝。"""
    bound = project["root_path"]
    if not bound:
        raise ApiError(409, "NO_PROJECT_ROOT", "该项目还没有绑定目录")
    if not any(
        paths.is_within(bound, root["path"]) for root in allowed_roots(conn, project["owner_id"])
    ):
        raise ApiError(
            403,
            "GRANT_REVOKED",
            "该目录的授权已被管理员收回，暂不能访问；请联系管理员重新授予",
        )
    return Path(bound)


def bind_root(
    conn: sqlite3.Connection,
    project: sqlite3.Row,
    *,
    raw_path: str,
    replace: bool = False,
) -> sqlite3.Row:
    if project["status"] == "deleted":
        raise conflict("回收站中的项目不能绑定目录", code="PROJECT_DELETED")
    # 已绑定目录也要按当前授予重新校验：撤销授权后连“重新绑定同一目录”都拒绝。
    if project["root_path"]:
        assert_bound_root_granted(conn, project)
    roots_for_user = granted_paths(conn, project["owner_id"])
    if not roots_for_user:
        raise ApiError(
            409, "NO_ALLOWED_ROOTS", "管理员尚未为你授权任何目录，暂不能绑定"
        )
    target = paths.validate_binding(raw_path, roots_for_user)
    existing = project["root_path"]
    if existing and not replace and str(target) != existing:
        raise conflict(
            "该项目已绑定其它目录；如需更换请确认替换（原绑定不会被自动改动）",
            code="ROOT_ALREADY_BOUND",
        )
    if existing and replace and str(target) == existing:
        return project
    now = security.now_iso()
    conn.execute(
        """
        UPDATE projects SET root_path = ?, root_bound_at = ?, root_bound_by = ?, updated_at = ?
        WHERE id = ?
        """,
        (str(target), now, project["owner_id"], now, project["id"]),
    )
    conn.commit()
    audit.record(
        conn,
        project["owner_id"],
        "project.bind_root",
        project["id"],
        f"{existing or '未绑定'} -> {target}",
    )
    return get_owned(conn, project["id"], project["owner_id"])


def unbind_root(conn: sqlite3.Connection, project: sqlite3.Row) -> sqlite3.Row:
    if not project["root_path"]:
        return project
    old = project["root_path"]
    conn.execute(
        """
        UPDATE projects SET root_path = NULL, root_bound_at = NULL, root_bound_by = NULL,
                            updated_at = ?
        WHERE id = ?
        """,
        (security.now_iso(), project["id"]),
    )
    conn.commit()
    audit.record(conn, project["owner_id"], "project.unbind_root", project["id"], str(old))
    return get_owned(conn, project["id"], project["owner_id"])

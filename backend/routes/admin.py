"""本机管理接口：账号与允许根目录。

* 只能由 ``role=admin`` 调用；
* 管理员不能停用自己，也不能通过接口创建新的管理员（新管理员用本机 CLI 创建）；
* 管理员看不到、也改不了其他用户的个人模型连接。
"""

from __future__ import annotations

from flask import Blueprint, request

from backend import auth, db
from backend.errors import ApiError, forbidden, ok
from backend.services import audit, roots, users

bp = Blueprint("admin", __name__)


def _current_admin() -> dict:
    admin = auth.current_user()
    assert admin is not None
    return admin


@bp.get("/admin/users")
@auth.admin_required
def list_users():
    return ok({"users": users.list_all(db.get_db())})


@bp.post("/admin/users")
@auth.admin_required
def create_user():
    payload = request.get_json(silent=True) or {}
    admin = _current_admin()
    username = payload.get("username")
    password = payload.get("password")
    if not password:
        raise ApiError(400, "VALIDATION", "请为新账号设置初始密码")
    # 强制普通用户：管理员账号只能由本机 CLI 创建，避免线上提权。
    row = users.create(
        db.get_db(),
        username=str(username or ""),
        password=str(password),
        display_name=payload.get("display_name"),
        role="member",
    )
    audit.record(db.get_db(), admin["id"], "user.create", row["username"])
    return ok({"user": users.public(row)})


@bp.put("/admin/users/<int:user_id>/status")
@auth.admin_required
def set_user_status(user_id: int):
    payload = request.get_json(silent=True) or {}
    status = str(payload.get("status") or "")
    admin = _current_admin()
    if int(user_id) == int(admin["id"]):
        raise forbidden("不能停用或启用自己")
    row = users.set_status(db.get_db(), user_id, status)
    audit.record(db.get_db(), admin["id"], f"user.status.{status}", row["username"])
    return ok({"user": users.public(row)})


@bp.post("/admin/users/<int:user_id>/reset-password")
@auth.admin_required
def reset_password(user_id: int):
    payload = request.get_json(silent=True) or {}
    password = str(payload.get("new_password") or "")
    if not password:
        raise ApiError(400, "VALIDATION", "请填写新密码")
    admin = _current_admin()
    target = users.get(db.get_db(), user_id)
    if int(user_id) == int(admin["id"]):
        raise ApiError(400, "VALIDATION", "修改自己的密码请使用“账号安全”，需要输入当前密码")
    users.reset_password(db.get_db(), user_id, password)
    audit.record(db.get_db(), admin["id"], "user.reset_password", target["username"])
    return ok({"reset": True})


@bp.get("/admin/roots")
@auth.admin_required
def list_roots():
    conn = db.get_db()
    return ok(
        {
            "roots": [
                roots.public(row, grants=roots.grants_of_root(conn, int(row["id"])))
                for row in roots.list_all(conn)
            ]
        }
    )


@bp.post("/admin/roots")
@auth.admin_required
def add_root():
    payload = request.get_json(silent=True) or {}
    admin = _current_admin()
    conn = db.get_db()
    row = roots.add(
        conn,
        actor_id=int(admin["id"]),
        raw_path=str(payload.get("path") or ""),
        label=str(payload.get("label") or ""),
    )
    # 授权根默认不共享：只有显式给出的账号才获得使用资格。
    granted: list[str] = []
    for username in payload.get("grant_to") or []:
        name = str(username or "").strip()
        if not name:
            continue
        roots.grant_by_username(
            conn, actor_id=int(admin["id"]), root_id=int(row["id"]), username=name
        )
        granted.append(name.lower())
    return ok(
        {
            "root": roots.public(
                row, grants=roots.grants_of_root(conn, int(row["id"]))
            ),
            "granted": granted,
        }
    )


@bp.post("/admin/roots/<int:root_id>/grants")
@auth.admin_required
def grant_root(root_id: int):
    payload = request.get_json(silent=True) or {}
    admin = _current_admin()
    conn = db.get_db()
    roots.grant_by_username(
        conn,
        actor_id=int(admin["id"]),
        root_id=root_id,
        username=str(payload.get("username") or ""),
    )
    row = roots.get(conn, root_id)
    return ok({"root": roots.public(row, grants=roots.grants_of_root(conn, root_id))})


@bp.delete("/admin/roots/<int:root_id>/grants/<int:user_id>")
@auth.admin_required
def revoke_root(root_id: int, user_id: int):
    admin = _current_admin()
    removed = roots.revoke(
        db.get_db(), actor_id=int(admin["id"]), root_id=root_id, user_id=user_id
    )
    return ok({"removed": removed})


@bp.delete("/admin/roots/<int:root_id>")
@auth.admin_required
def remove_root(root_id: int):
    admin = _current_admin()
    roots.remove(db.get_db(), actor_id=int(admin["id"]), root_id=root_id)
    return ok({"removed": True})


@bp.get("/admin/audit")
@auth.admin_required
def audit_tail():
    limit = request.args.get("limit", type=int) or 50
    return ok({"entries": audit.recent(db.get_db(), limit)})

"""项目接口：CRUD、回收站、目录绑定。"""

from __future__ import annotations

from flask import Blueprint, current_app, request

from backend import auth, db, paths
from backend.errors import ok
from backend.services import projects

bp = Blueprint("projects", __name__)


def _user() -> dict:
    user = auth.current_user()
    assert user is not None
    return user


def _stop_project_turns(project_id: str, user_id: int, reason: str) -> None:
    runtime = current_app.config.get("HX17_AGENT")
    if runtime is None:
        return
    try:
        runtime.invalidate_project(user_id, project_id, reason=reason)
    except Exception:
        pass


def _trash_flag() -> bool:
    raw = (request.args.get("trash") or "").strip().lower()
    return raw in {"1", "true", "yes", "only"}


@bp.get("/projects")
@auth.login_required
def list_projects():
    conn = db.get_db()
    rows = projects.list_for(conn, int(_user()["id"]), include_deleted=_trash_flag())
    return ok({"projects": [projects.public(row) for row in rows]})


@bp.post("/projects")
@auth.login_required
def create_project():
    payload = request.get_json(silent=True) or {}
    row = projects.create(
        db.get_db(),
        int(_user()["id"]),
        name=str(payload.get("name") or ""),
        description=str(payload.get("description") or ""),
    )
    return ok({"project": projects.public(row)})


@bp.get("/projects/<project_id>")
@auth.login_required
def get_project(project_id: str):
    conn = db.get_db()
    row = projects.get_owned(conn, project_id, int(_user()["id"]), include_deleted=True)
    return ok({"project": projects.public(row)})


@bp.put("/projects/<project_id>")
@auth.login_required
def update_project(project_id: str):
    conn = db.get_db()
    user_id = int(_user()["id"])
    row = projects.get_owned(conn, project_id, user_id)
    payload = request.get_json(silent=True) or {}
    updated = projects.update(
        conn,
        row,
        name=payload.get("name") if "name" in payload else None,
        description=payload.get("description") if "description" in payload else None,
    )
    return ok({"project": projects.public(updated)})


@bp.delete("/projects/<project_id>")
@auth.login_required
def delete_project(project_id: str):
    conn = db.get_db()
    user_id = int(_user()["id"])
    row = projects.get_owned(conn, project_id, user_id)
    projects.soft_delete(conn, row)
    runtime = current_app.config.get("HX17_AGENT")
    if runtime is not None:
        try:
            runtime.invalidate_project(
                user_id, project_id, reason="项目已删除，运行中的轮次已终止"
            )
        except Exception:
            pass
    return ok({"deleted": True, "project_id": project_id})


@bp.post("/projects/<project_id>/restore")
@auth.login_required
def restore_project(project_id: str):
    conn = db.get_db()
    row = projects.get_owned(
        conn, project_id, int(_user()["id"]), include_deleted=True
    )
    restored = projects.restore(conn, row)
    return ok({"project": projects.public(restored)})


@bp.put("/projects/<project_id>/root")
@auth.login_required
def bind_root(project_id: str):
    payload = request.get_json(silent=True) or {}
    replace = bool(payload.get("replace"))
    conn = db.get_db()
    row = projects.get_owned(conn, project_id, int(_user()["id"]))
    updated = projects.bind_root(
        conn, row, raw_path=str(payload.get("path") or ""), replace=replace
    )
    _stop_project_turns(updated["id"], int(_user()["id"]), "项目目录已变更，运行中的轮次已终止")
    return ok({"project": projects.public(updated), "replaced": replace})


@bp.delete("/projects/<project_id>/root")
@auth.login_required
def unbind_root(project_id: str):
    conn = db.get_db()
    row = projects.get_owned(conn, project_id, int(_user()["id"]))
    updated = projects.unbind_root(conn, row)
    _stop_project_turns(updated["id"], int(_user()["id"]), "项目目录绑定已解除，运行中的轮次已终止")
    return ok({"project": projects.public(updated)})


@bp.get("/projects/<project_id>/root/entries")
@auth.login_required
def browse_project_root(project_id: str):
    """项目目录浏览器：只能在绑定目录内部逐层查看，且授予必须仍然有效。"""
    conn = db.get_db()
    project = projects.get_owned(conn, project_id, int(_user()["id"]))
    bound = projects.assert_bound_root_granted(conn, project)
    target = request.args.get("path") or str(bound)
    data = paths.list_entries(bound, target)
    data["base"] = str(bound)
    return ok(data)

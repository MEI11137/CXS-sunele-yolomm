"""服务器目录浏览（不是评委电脑的目录）。

* 没有项目上下文时，只能浏览**当前账号获授**的根目录；
* 带 ``project_id`` 时，只能浏览该项目已绑定目录内部，且绑定目录必须仍在授予范围内
  （管理员撤销授予后立即拒绝）；
* 任何路径都先做真实路径归一化，再做包含判断，链接/联接越界一律 403。
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, request

from backend import auth, db, paths
from backend.errors import ApiError, ok
from backend.services import projects, roots

bp = Blueprint("directories", __name__)


def _user() -> dict:
    user = auth.current_user()
    assert user is not None
    return user


def _root_entries(items: list[dict]) -> list[dict]:
    return [
        {
            "name": item["label"] or Path(item["path"]).name or item["path"],
            "path": item["path"],
            "is_dir": True,
            "size": 0,
            "is_link": False,
        }
        for item in items
    ]


@bp.get("/directories/roots")
@auth.login_required
def list_roots():
    """普通用户需要知道自己能在哪些范围内绑定目录（按账号授予，不共享）。"""
    items = roots.list_for_user(db.get_db(), int(_user()["id"]))
    return ok(
        {
            "roots": [
                # 只给路径和标签，不暴露是谁在何时添加的。
                {"path": item["path"], "label": item["label"]}
                for item in items
            ]
        }
    )


@bp.get("/directories/browse")
@auth.login_required
def browse():
    conn = db.get_db()
    user_id = int(_user()["id"])
    project_id = (request.args.get("project_id") or "").strip()
    raw = (request.args.get("path") or "").strip()
    root_items = roots.list_for_user(conn, user_id)

    if project_id:
        project = projects.get_owned(conn, project_id, user_id)
        base = projects.assert_bound_root_granted(conn, project)
        if not raw:
            return ok(
                {
                    "path": None,
                    "base": str(base),
                    "entries": _root_entries([{"path": str(base), "label": project["name"]}]),
                    "blocked": 0,
                }
            )
        data = paths.list_entries(base, raw)
        data["base"] = str(base)
        return ok(data)

    if not raw:
        if not root_items:
            raise ApiError(409, "NO_ALLOWED_ROOTS", "管理员尚未为你授权任何目录")
        return ok(
            {
                "path": None,
                "base": None,
                "entries": _root_entries(root_items),
                "blocked": 0,
                "roots": [item["path"] for item in root_items],
            }
        )

    if not root_items:
        raise ApiError(409, "NO_ALLOWED_ROOTS", "管理员尚未为你授权任何目录")
    target = paths.canonical(raw)
    for item in root_items:
        if paths.is_within(target, item["path"]):
            data = paths.list_entries(item["path"], target)
            data["base"] = item["path"]
            return ok(data)
    raise ApiError(403, "OUTSIDE_ALLOWED_ROOTS", "只能浏览管理员授权给你的目录")

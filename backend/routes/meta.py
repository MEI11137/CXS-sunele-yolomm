"""公开运行元信息：版本、阶段、功能开关、是否已有账号。

前端用 ``initialized`` 决定显示登录框还是“本机初始化指引”；
用 ``features`` 决定哪些入口显示“未实现”，不允许界面自行假设能力。
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify

from backend import db
from backend.services import users

bp = Blueprint("meta", __name__)


@bp.get("/meta")
def meta():
    settings = current_app.config["HX17"]
    data = settings.public()
    data["initialized"] = users.count_users(db.get_db()) > 0
    data["auth"] = "cookie-session"
    return jsonify({"code": 0, "msg": "ok", "data": data})

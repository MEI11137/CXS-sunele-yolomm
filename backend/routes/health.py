"""公开健康检查：只报版本、阶段与存活状态。

不返回数据目录、端口、密钥路径等内部配置，避免匿名访问者据此摸清部署结构。
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify

bp = Blueprint("health", __name__)


@bp.get("/health")
def health():
    return jsonify(
        {
            "code": 0,
            "msg": "ok",
            "data": {
                "status": "ok",
                "service": current_app.config["HX17"].service,
                "version": current_app.config["HX17"].version,
                "stage": current_app.config["HX17"].stage,
                "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        }
    )

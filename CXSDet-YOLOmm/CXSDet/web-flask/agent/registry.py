"""Agent 模块的统一注册入口（B-01：公共迁移与后端注册由本模块管理）。

``app.py`` 只需要两行：导入 ``register_agent`` 并在 ``create_app`` 内调用。
其他人的路由文件、服务文件都不需要改动。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

_REGISTERED = set()


def register_agent(app) -> Dict[str, Any]:
    """在给定 Flask 应用上挂载 Agent 路由并准备数据。"""
    from agent import legacy, manuals, store
    from agent.recovery import reconcile_startup
    from agent.routes import agent_bp

    db_path = app.config.get("DATABASE")
    conn = store.connect(str(db_path) if db_path else None)
    conn.close()

    key = str(db_path or store.current_db_path())
    first_time = key not in _REGISTERED

    report: Dict[str, Any] = {"database": key, "first_registration": first_time}
    if first_time:
        _REGISTERED.add(key)
        try:
            report["builtin_manuals_added"] = manuals.ensure_builtin_manuals()
        except Exception as exc:  # noqa: BLE001 - 初始化失败不应阻断整个站点
            report["builtin_manuals_error"] = str(exc)[:200]
        try:
            report["legacy_resources"] = legacy.migrate_legacy_resources()
        except Exception as exc:  # noqa: BLE001
            report["legacy_resources_error"] = str(exc)[:200]
        try:
            report["startup_reconcile"] = reconcile_startup()
        except Exception as exc:  # noqa: BLE001
            report["startup_reconcile_error"] = str(exc)[:200]

    if "agent_bp" not in app.blueprints:
        app.register_blueprint(agent_bp)
    return report


def reset_registration_cache() -> None:
    """测试切换数据库时使用。"""
    _REGISTERED.clear()

"""Agent 模块的统一注册入口（B-01：公共迁移与后端注册由本模块管理）。

``app.py`` 只需要两行：导入 ``register_agent`` 并在 ``create_app`` 内调用。
其他人的路由文件、服务文件都不需要改动。

INT-01 调整：所有初始化都在 ``with app.app_context():`` 内执行，使用这个 app 的
``DATABASE``；同时装配 C 的真实业务后端。此前在 create_app 外部调用时会回退到
进程默认库，导致迁移与启动核对作用在错误的数据库上。
"""

from __future__ import annotations

from typing import Any, Dict

_REGISTERED = set()


def register_agent(app) -> Dict[str, Any]:
    """在给定 Flask 应用上挂载 Agent 路由、准备数据并装配业务后端。"""
    from agent import legacy, manuals, store
    from agent.bootstrap import configure_business_backend
    from agent.recovery import reconcile_startup
    from agent.routes import agent_bp

    report: Dict[str, Any] = {}
    with app.app_context():
        db_path = app.config.get("DATABASE") or store.current_db_path()
        key = (id(app), str(db_path))
        first_time = key not in _REGISTERED

        report = {"database": str(db_path), "first_registration": first_time}
        needs_recovery = False
        if first_time:
            conn = store.connect(str(db_path))
            conn.close()
            from agent.process_lease import claim
            needs_recovery = claim(db_path)
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
                report["startup_reconcile"] = reconcile_startup() if needs_recovery else {'skipped': 'same live process'}
                if needs_recovery:
                    from services.training_runner import reconcile_training_jobs
                    report['interrupted_training_jobs'] = reconcile_training_jobs(db_path)
            except Exception as exc:  # noqa: BLE001
                report["startup_reconcile_error"] = str(exc)[:200]

        # 真实业务装配：失败要如实上报，不能静默降级成"看起来已接通"。
        try:
            report["business"] = configure_business_backend(app)
            if needs_recovery:
                backend = app.extensions['haixun_agent']['business_backend']
                report['unknown_business_operations'] = backend.reconcile_unfinished()
        except Exception as exc:  # noqa: BLE001
            report["business_error"] = f"{type(exc).__name__}: {exc}"[:400]

    if "agent_bp" not in app.blueprints:
        app.register_blueprint(agent_bp)
    return report


def reset_registration_cache() -> None:
    """测试切换数据库时使用。"""
    _REGISTERED.clear()

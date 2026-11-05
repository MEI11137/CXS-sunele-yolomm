"""Agent 运行时装配。

一个应用实例持有：私有工具网关、引擎进程池、轮次运行器。三者生命周期与 Flask 应用一致，
但**都不挂在对外端口上**：网关只监听 127.0.0.1 的随机端口，引擎同样只监听回环。
"""

from __future__ import annotations

import threading
import time

from backend.agent import engine, runner, store
from backend.agent.gateway import ToolGateway


class AgentRuntime:
    def __init__(self, settings, *, spawner=None) -> None:
        self.settings = settings
        self.gateway = ToolGateway(settings)
        self.manager = engine.EngineManager(settings, spawner=spawner, tokens=self.gateway)
        self.runner = runner.TurnRunner(settings, self.manager, self.gateway)
        self._started_at = time.time()
        self._sweeper: threading.Thread | None = None
        self._sweeper_stop = threading.Event()

    def start(self) -> None:
        self.gateway.start()
        self._sweeper = threading.Thread(target=self._sweep_loop, name="hx17-engine-sweeper", daemon=True)
        self._sweeper.start()

    def _sweep_loop(self) -> None:
        while not self._sweeper_stop.wait(120):
            try:
                self.manager.sweep_idle()
            except Exception:
                pass

    def shutdown(self) -> None:
        self._sweeper_stop.set()
        self.runner.shutdown()
        try:
            self.manager.shutdown()
        finally:
            self.gateway.stop()

    # ------------------------------------------------------------------ 上下文变更

    def invalidate_session(self, session_id: str, *, reason: str) -> int:
        """会话被删除：立刻停引擎、撤令牌，并把活动轮次标成失败（不假装完成）。"""
        from backend import db

        self.gateway.revoke_session(session_id)
        self.manager.stop_session(session_id)
        conn = db.connect(self.settings.db_path)
        try:
            from backend.services import followups as followup_service

            followup_service.cancel_session(conn, session_id, "会话已删除")
            return store.interrupt_scope(
                conn,
                code="SESSION_CLOSED",
                message=reason,
                session_id=session_id,
            )
        finally:
            conn.close()

    def invalidate_project(self, user_id: int, project_id: str, *, reason: str) -> int:
        from backend import db

        self.manager.stop_project(int(user_id), project_id)
        conn = db.connect(self.settings.db_path)
        try:
            from backend.services import followups as followup_service

            followup_service.cancel_project(conn, project_id, int(user_id), "项目已关闭或目录权限被撤销")
            return store.interrupt_scope(
                conn,
                code="PROJECT_CLOSED",
                message=reason,
                project_id=project_id,
                owner_id=int(user_id),
            )
        finally:
            conn.close()

    def invalidate_user(self, user_id: int, *, code: str, reason: str) -> int:
        """连接变更/删除、账号停用：旧引擎立即失效，运行中的轮次不能再用旧凭据收尾。"""
        from backend import db

        self.manager.stop_user(int(user_id))
        conn = db.connect(self.settings.db_path)
        try:
            from backend.services import followups as followup_service

            followup_service.cancel_user(conn, int(user_id), "个人模型连接已变更或失效，需重新确认")
            return store.interrupt_scope(
                conn, code=code, message=reason, owner_id=int(user_id)
            )
        finally:
            conn.close()

    def describe(self) -> dict:
        """对外可见的运行时信息：不含路径、端口与令牌。"""
        return {
            "engine_version": self.settings.engine_version,
            "strict_version": bool(self.settings.engine_strict_version),
            "shell_enabled": False,
            "gateway": "loopback",
            "active_engines": self.manager.active_count(),
            "active_turns": self.runner.active_threads(),
        }

"""服务端身份上下文（B-01）。

原则：``user_id`` **只**从服务端会话/JWT 与数据库里的用户行推导。
任何来自请求体、模型工具参数、浏览器 ``approved`` 标记的身份声明都不被采信。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from agent.errors import AgentError, Code
from agent.store import connect, query_one


@dataclass(frozen=True)
class ActorContext:
    """一次请求/一个任务步骤的可信身份。"""

    user_id: int
    roles: Tuple[str, ...] = ()
    request_id: str = ""
    source: str = "http"

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "roles": list(self.roles),
            "request_id": self.request_id,
            "source": self.source,
        }

    def assert_owner(self, owner_id: int) -> None:
        """跨用户操作一律拒绝（管理员也不例外，除非走显式管理入口）。"""
        if owner_id != self.user_id:
            raise AgentError(Code.FORBIDDEN, "不能操作其他用户的工作区", http_status=403)

    def canonical_owner_id(self, requested: Optional[int] = None) -> int:
        """忽略客户端传来的 owner_id，始终返回当前身份的用户。"""
        return self.user_id


def _roles_for(user_row: Dict[str, Any]) -> Tuple[str, ...]:
    roles = []
    if int(user_row.get("role") or 0) == 1:
        roles.append("admin")
    roles.append("user")
    return tuple(roles)


def load_actor(user_id: int, *, request_id: str = "", source: str = "http") -> ActorContext:
    """按用户 ID 从数据库读取角色，状态异常直接拒绝。"""
    conn = connect()
    try:
        row = query_one(
            conn,
            "SELECT id, username, role, status FROM user WHERE id = ?",
            (user_id,),
        )
    finally:
        conn.close()
    if not row:
        raise AgentError(Code.UNAUTHENTICATED, "账号不存在或已被删除", http_status=401)
    if int(row.get("status") or 0) != 1:
        raise AgentError(Code.UNAUTHENTICATED, "账号已被停用", http_status=401)
    return ActorContext(
        user_id=int(row["id"]),
        roles=_roles_for(row),
        request_id=request_id or uuid.uuid4().hex,
        source=source,
    )


def from_flask_request() -> ActorContext:
    """在 ``@token_required`` 之后调用，只使用服务端写入 ``g`` 的身份。"""
    from flask import g, request

    user_id = getattr(g, "user_id", None)
    if not user_id:
        raise AgentError(Code.UNAUTHENTICATED, "缺少有效登录状态", http_status=401)
    return load_actor(
        int(user_id),
        request_id=request.headers.get("X-Request-Id", ""),
        source="http",
    )


def system_actor(reason: str = "system") -> ActorContext:
    """迁移、启动核对等后台动作使用的特权身份，不暴露给 HTTP 入口。

    管理员 ID 取数据库中最早的管理员账号；没有管理员时返回 0，调用方需自行判断。
    """
    conn = connect()
    try:
        row = query_one(
            conn,
            "SELECT id FROM user WHERE role = 1 AND status = 1 ORDER BY id LIMIT 1",
        )
    finally:
        conn.close()
    user_id = int(row["id"]) if row else 0
    return ActorContext(user_id=user_id, roles=("admin", "user"), source=reason)


@dataclass(frozen=True)
class ServerContext:
    """交给第三人业务工具的受信任上下文（不含任何凭据明文）。"""

    actor: ActorContext
    request_id: str = ""
    project_id: Optional[str] = None
    task_id: Optional[str] = None
    authorization: Dict[str, Any] = field(default_factory=dict)

    def as_tool_context(self) -> Dict[str, Any]:
        return {
            "user_id": self.actor.user_id,
            "roles": list(self.actor.roles),
            "request_id": self.request_id or self.actor.request_id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "source": self.actor.source,
            "authorization": dict(self.authorization),
        }

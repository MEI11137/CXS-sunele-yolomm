"""会话与消息持久化（B-03）。

一段会话归属一个项目与一个用户；消息里保存附件/资源引用与任务关联，
刷新页面后前端可按会话取回完整记录。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.errors import AgentError, Code
from agent.identity import ActorContext
from agent.permissions import get_project
from agent.store import connect, dumps, loads, new_id, now, query_all, query_one

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"
ROLE_SYSTEM_EVENT = "system_event"


def create_session(
    actor: ActorContext,
    project_id: str,
    title: str = "",
    *,
    manual_id: Optional[str] = None,
    manual_version: Optional[int] = None,
) -> Dict[str, Any]:
    get_project(actor, project_id)
    session_id = new_id("ses")
    stamp = now()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO agent_session "
            "(id, project_id, owner_id, title, manual_id, manual_version, "
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                project_id,
                actor.user_id,
                title or "新会话",
                manual_id,
                manual_version,
                stamp,
                stamp,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_session(actor, session_id)


def get_session(actor: ActorContext, session_id: str) -> Dict[str, Any]:
    conn = connect()
    try:
        row = query_one(conn, "SELECT * FROM agent_session WHERE id = ?", (session_id,))
    finally:
        conn.close()
    if not row or int(row["owner_id"]) != actor.user_id:
        raise AgentError(Code.NOT_FOUND, "会话不存在", http_status=404)
    return _session_view(row)


def list_sessions(
    actor: ActorContext, project_id: Optional[str] = None, limit: int = 50
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM agent_session WHERE owner_id = ?"
    params: List[Any] = [actor.user_id]
    if project_id:
        get_project(actor, project_id)
        sql += " AND project_id = ?"
        params.append(project_id)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    conn = connect()
    try:
        rows = query_all(conn, sql, params)
    finally:
        conn.close()
    return [_session_view(row) for row in rows]


def touch_session(session_id: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE agent_session SET updated_at = ? WHERE id = ?", (now(), session_id)
        )
        conn.commit()
    finally:
        conn.close()


def append_message(
    session_id: str,
    role: str,
    content: str,
    *,
    task_id: Optional[str] = None,
    refs: Optional[List[Any]] = None,
    tool_name: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> Dict[str, Any]:
    message_id = new_id("msg")
    stamp = now()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO agent_message "
            "(id, session_id, task_id, role, content, refs_json, tool_name, "
            " tool_call_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                session_id,
                task_id,
                role,
                content,
                dumps(refs or []),
                tool_name,
                tool_call_id,
                stamp,
            ),
        )
        conn.execute(
            "UPDATE agent_session SET updated_at = ? WHERE id = ?", (stamp, session_id)
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "id": message_id,
        "session_id": session_id,
        "task_id": task_id,
        "role": role,
        "content": content,
        "refs": refs or [],
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "created_at": stamp,
    }


def list_messages(
    session_id: str, limit: Optional[int] = None, newest_first: bool = False
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM agent_message WHERE session_id = ? ORDER BY created_at, rowid"
    params: List[Any] = [session_id]
    if limit:
        # 取最近 N 条：先按时间倒序截断，再翻回正序
        sql = (
            "SELECT * FROM (SELECT rowid AS message_order, * FROM agent_message WHERE session_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?) ORDER BY created_at, message_order"
        )
        params.append(int(limit))
    conn = connect()
    try:
        rows = query_all(conn, sql, params)
    finally:
        conn.close()
    if newest_first:
        rows.reverse()
    return [_message_view(row) for row in rows]


def _session_view(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "owner_id": int(row["owner_id"]),
        "title": row.get("title") or "",
        "manual_id": row.get("manual_id"),
        "manual_version": row.get("manual_version"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _message_view(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "task_id": row.get("task_id"),
        "role": row["role"],
        "content": row["content"],
        "refs": loads(row.get("refs_json"), []),
        "tool_name": row.get("tool_name"),
        "tool_call_id": row.get("tool_call_id"),
        "created_at": row["created_at"],
    }

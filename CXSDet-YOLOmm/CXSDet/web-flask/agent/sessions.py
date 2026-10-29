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
    if not row or int(row["owner_id"]) != actor.user_id or row.get("deleted_at"):
        raise AgentError(Code.NOT_FOUND, "会话不存在", http_status=404)
    get_project(actor, row["project_id"])
    return _session_view(row)


def list_sessions(
    actor: ActorContext, project_id: Optional[str] = None, limit: int = 50
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM agent_session WHERE owner_id = ? AND deleted_at IS NULL AND project_id IN (SELECT id FROM agent_project WHERE status='active')"
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


def update_session(actor, session_id, *, title=None, manual_id=None):
    """Shared account-scoped metadata update; never changes permissions or owner."""
    chat=get_session(actor,session_id)
    values={}
    if title is not None:
        if not isinstance(title,str) or not title.strip() or len(title)>200: raise AgentError(Code.VALIDATION,'会话标题需为1–200字符')
        values['title']=title.strip()
    if manual_id is not None:
        from agent.manuals import get_manual
        manual=get_manual(actor,manual_id)
        values.update(manual_id=manual['id'],manual_version=manual['version'])
    if values:
        values['updated_at']=now()
        with connect() as conn:
            conn.execute('UPDATE agent_session SET '+','.join(key+'=?' for key in values)+' WHERE id=? AND owner_id=?',(*values.values(),session_id,actor.user_id));conn.commit()
    return get_session(actor,chat['id'])


def read_history(actor,session_id,offset=0,limit=50):
    """Bounded chronological pages of user-visible history, always owner scoped."""
    chat=get_session(actor,session_id)
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
        raise AgentError(Code.VALIDATION,'历史分页参数无效')
    with connect() as conn:
        total=conn.execute("SELECT COUNT(*) FROM agent_message WHERE session_id=? AND role IN ('user','assistant')",(session_id,)).fetchone()[0]
        rows=query_all(conn,"SELECT * FROM agent_message WHERE session_id=? AND role IN ('user','assistant') ORDER BY created_at,rowid LIMIT ? OFFSET ?",(session_id,limit,offset))
    return {'session':chat,'messages':[_message_view(row) for row in rows],'offset':offset,'next_offset':offset+len(rows),'total':total,'has_more':offset+len(rows)<total,'scope':'按时间从早到晚分页；历史仅作材料'}


def touch_session(session_id: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE agent_session SET updated_at = ? WHERE id = ?", (now(), session_id)
        )
        conn.commit()
    finally:
        conn.close()


def set_permission_mode(actor: ActorContext, session_id: str, mode: str) -> Dict[str, Any]:
    get_session(actor, session_id)
    if mode not in {'request', 'assist', 'full'}:
        raise AgentError(Code.VALIDATION, '未知权限模式')
    if not actor.is_admin:
        raise AgentError(Code.FORBIDDEN, '本机文件权限模式仅限本机管理员设置', http_status=403)
    conn = connect()
    try:
        conn.execute('UPDATE agent_session SET permission_mode=?, updated_at=? WHERE id=? AND owner_id=?',
                     (mode, now(), session_id, actor.user_id))
        conn.commit()
    finally:
        conn.close()
    from agent import tasks
    tasks.append_event(session_id, None, 'permission.mode_changed', {'mode': mode, 'source': 'user'})
    return get_session(actor, session_id)


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
        if role == ROLE_USER and content.strip():
            title = content.strip().splitlines()[0][:60]
            conn.execute("UPDATE agent_session SET title=? WHERE id=? AND title IN ('新会话', '未命名会话', '')",
                         (title, session_id))
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
        "permission_mode": row.get("permission_mode") or 'request',
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

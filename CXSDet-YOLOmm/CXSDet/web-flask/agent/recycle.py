"""项目/会话回收站：仅改变可见性，保留业务数据和本机文件。"""
from agent.errors import AgentError, Code
from agent.store import connect, query_all, query_one, now


def _owned(conn, table, actor, key):
    row = query_one(conn, f"SELECT * FROM {table} WHERE id=? AND owner_id=?", (key, actor.user_id))
    if not row:
        raise AgentError(Code.NOT_FOUND, "项目或会话不存在", http_status=404)
    return row


def _idle(conn, where, key, initiating_task_id=None):
    if query_one(conn, "SELECT id FROM agent_task WHERE " + where + "=? AND id<>? AND status IN ('running','planning','awaiting_business','unknown')", (key, initiating_task_id or '')):
        raise AgentError(Code.TASK_STATE_CONFLICT, "此项目或会话仍有执行中或待核实任务，请先停止或核实任务", http_status=409)


def change(actor, kind, key, *, restore=False, initiating_task_id=None):
    table = "agent_project" if kind == "project" else "agent_session"
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _owned(conn, table, actor, key)
        initiator=None
        if initiating_task_id:
            initiator=query_one(conn,'SELECT * FROM agent_task WHERE id=? AND owner_id=?',(initiating_task_id,actor.user_id))
            if not initiator: raise AgentError(Code.FORBIDDEN,'操作任务不存在')
        if kind == "project":
            if row["origin"] == "system":
                raise AgentError(Code.FORBIDDEN, "系统资源项目不能删除", http_status=403)
            if not restore:
                _idle(conn, "project_id", key, initiating_task_id)
                # 训练进程可能独立于聊天存活；有本项目模型且本机训练锁占用时拒绝删除。
                lock_exists = query_one(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='training_lock'")
                if lock_exists and query_one(conn, "SELECT 1 AS busy FROM training_lock LIMIT 1") and query_one(conn,
                        "SELECT id FROM agent_resource WHERE project_id=? AND kind='model' LIMIT 1", (key,)):
                    raise AgentError(Code.TASK_STATE_CONFLICT, "存在训练任务，请训练结束后再删除项目", http_status=409)
            conn.execute("UPDATE agent_project SET status=?, updated_at=? WHERE id=?", ("active" if restore else "deleted", now(), key))
        else:
            project = _owned(conn, "agent_project", actor, row["project_id"])
            if project["status"] != "active":
                raise AgentError(Code.VALIDATION, "请先恢复所属项目")
            if not restore:
                _idle(conn, "session_id", key, initiating_task_id)
            conn.execute("UPDATE agent_session SET deleted_at=?, updated_at=? WHERE id=?", (None if restore else now(), now(), key))
        if not restore and initiator and (initiator['project_id'] if kind=='project' else initiator['session_id'])==key:
            conn.execute("UPDATE agent_task SET status='stopped',summary=?,updated_at=? WHERE id=?",('所属项目或会话已按确认删除，停止后续步骤',now(),initiating_task_id))
        if not restore:
            scope='project_id' if kind=='project' else 'session_id'
            conn.execute("UPDATE agent_action SET status='superseded',updated_at=? WHERE "+scope+"=? AND status='pending'",(now(),key))
            conn.execute("UPDATE agent_task SET status='stopped',summary=?,updated_at=? WHERE "+scope+"=? AND status IN ('awaiting_action','awaiting_plan','awaiting_human','awaiting_input','interrupted')",('所属项目或会话已删除，恢复后请重新提出任务',now(),key))
        conn.commit()
        return {"id": key, "project_id": key if kind == "project" else row["project_id"], "restored": restore, "recoverable": True}
    finally:
        conn.close()


def list_deleted(actor):
    conn = connect()
    try:
        return {"projects": query_all(conn, "SELECT id,name,updated_at FROM agent_project WHERE owner_id=? AND status='deleted' ORDER BY updated_at DESC", (actor.user_id,)),
                "sessions": query_all(conn, "SELECT s.id,s.title,s.project_id,p.name AS project_name,s.deleted_at FROM agent_session s JOIN agent_project p ON s.project_id=p.id WHERE s.owner_id=? AND s.deleted_at IS NOT NULL AND p.status='active' ORDER BY s.deleted_at DESC", (actor.user_id,))}
    finally:
        conn.close()

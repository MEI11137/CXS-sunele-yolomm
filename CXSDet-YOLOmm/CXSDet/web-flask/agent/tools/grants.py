"""外部工具授权策略（B-09）。

* 预置业务工具按风险等级决定是否需要确认（写入/训练/发布必确认）；
* 外部 MCP 工具**默认每次确认**，只有用户显式核对过的**具体只读工具**才自动放行；
* 放行绑定到"具体连接 + 具体工具 + 工具版本"，工具版本/能力变化后授权自动失效；
* 工具自称"只读"只作为核对材料，不构成放行依据。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.errors import AgentError, Code
from agent.identity import ActorContext
from agent.store import connect, new_id, now, query_all, query_one
from agent.tools.contract import SOURCE_CONTROL, SOURCE_MCP, ToolSpec

MODE_CONFIRM_EACH = "confirm_each"
MODE_AUTO_READONLY = "auto_readonly"


def needs_confirmation(
    actor: ActorContext,
    spec: ToolSpec,
    *,
    project_id: Optional[str] = None,
    source: str = "",
) -> bool:
    """返回 True 表示必须先展示确认卡片。"""
    effective_source = source or spec.source
    if effective_source == SOURCE_CONTROL:
        return False
    # R5：写入/训练/发布/计费的强制确认先于任何"工具自称可免确认"的说法，
    # 包括外部连接的只读放行记录——放行只能覆盖只读且不计费的具体工具。
    if spec.forced_confirmation:
        return True
    if effective_source != SOURCE_MCP:
        return spec.needs_confirmation

    # 外部工具：只读且不计费，才看得具体授权记录
    if not spec.readonly or spec.billable:
        return True
    grant = _find_grant(actor, spec, project_id)
    if grant and grant["mode"] == MODE_AUTO_READONLY:
        return False
    return True


def grant_readonly(
    actor: ActorContext,
    server_id: str,
    tool_name: str,
    tool_version: str,
    *,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """放行某个连接中的某个具体只读工具。"""
    if not server_id or not tool_name:
        raise AgentError(Code.VALIDATION, "需要指定连接与工具名称")
    conn = connect()
    try:
        existing = query_one(
            conn,
            "SELECT * FROM agent_tool_grant WHERE user_id = ? AND server_id = ? "
            "AND tool_name = ? AND revoked_at IS NULL",
            (actor.user_id, server_id, tool_name),
        )
        stamp = now()
        if existing:
            conn.execute(
                "UPDATE agent_tool_grant SET tool_version = ?, mode = ?, project_id = ?, "
                "created_at = ? WHERE id = ?",
                (
                    tool_version,
                    MODE_AUTO_READONLY,
                    project_id,
                    stamp,
                    existing["id"],
                ),
            )
        else:
            conn.execute(
                "INSERT INTO agent_tool_grant "
                "(id, user_id, project_id, server_id, tool_name, tool_version, mode, "
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("grt"),
                    actor.user_id,
                    project_id,
                    server_id,
                    tool_name,
                    tool_version,
                    MODE_AUTO_READONLY,
                    stamp,
                ),
            )
        conn.commit()
        row = query_one(
            conn,
            "SELECT * FROM agent_tool_grant WHERE user_id = ? AND server_id = ? "
            "AND tool_name = ? AND revoked_at IS NULL",
            (actor.user_id, server_id, tool_name),
        )
    finally:
        conn.close()
    return _view(row)


def revoke_grant(actor: ActorContext, grant_id: str) -> bool:
    conn = connect()
    try:
        cursor = conn.execute(
            "UPDATE agent_tool_grant SET revoked_at = ? WHERE id = ? AND user_id = ?",
            (now(), grant_id, actor.user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def list_grants(actor: ActorContext) -> List[Dict[str, Any]]:
    conn = connect()
    try:
        rows = query_all(
            conn,
            "SELECT * FROM agent_tool_grant WHERE user_id = ? AND revoked_at IS NULL "
            "ORDER BY created_at DESC",
            (actor.user_id,),
        )
    finally:
        conn.close()
    return [_view(row) for row in rows]


def invalidate_on_version_change(
    actor: ActorContext, server_id: str, tool_name: str, tool_version: str
) -> bool:
    """工具版本变化后作废旧放行，返回是否真的失效过。"""
    conn = connect()
    try:
        stale = query_one(
            conn,
            "SELECT id FROM agent_tool_grant WHERE user_id = ? AND server_id = ? "
            "AND tool_name = ? AND revoked_at IS NULL AND tool_version != ?",
            (actor.user_id, server_id, tool_name, tool_version),
        )
        if not stale:
            return False
        conn.execute(
            "UPDATE agent_tool_grant SET revoked_at = ? WHERE user_id = ? AND server_id = ? "
            "AND tool_name = ? AND tool_version != ?",
            (now(), actor.user_id, server_id, tool_name, tool_version),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def _find_grant(
    actor: ActorContext, spec: ToolSpec, project_id: Optional[str]
) -> Optional[Dict[str, Any]]:
    if not spec.server_id:
        return None
    conn = connect()
    try:
        row = query_one(
            conn,
            "SELECT * FROM agent_tool_grant WHERE user_id = ? AND server_id = ? "
            "AND tool_name = ? AND revoked_at IS NULL",
            (actor.user_id, spec.server_id, spec.name),
        )
    finally:
        conn.close()
    if not row:
        return None
    if str(row["tool_version"]) != str(spec.version):
        # 版本不一致视为授权失效，需要重新核对
        invalidate_on_version_change(actor, spec.server_id, spec.name, spec.version)
        return None
    if row.get("project_id") and project_id and row["project_id"] != project_id:
        return None
    return _view(row)


def _view(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not row:
        raise AgentError(Code.NOT_FOUND, "授权记录不存在", http_status=404)
    return {
        "id": row["id"],
        "project_id": row.get("project_id"),
        "server_id": row["server_id"],
        "tool_name": row["tool_name"],
        "tool_version": row["tool_version"],
        "mode": row["mode"],
        "created_at": row["created_at"],
    }

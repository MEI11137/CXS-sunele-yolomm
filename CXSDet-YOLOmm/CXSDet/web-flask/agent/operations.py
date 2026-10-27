"""业务操作记录（B-03 / B-05）。

每次真实工具调用的结果都落库：状态、业务引用、逐项结果、是否替身、
以及 ``request_key`` —— 恢复与防重都以这张表为准，而不是聊天里的文字。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.errors import AgentError, Code, ExecutionState
from agent.identity import ActorContext
from agent.store import connect, dumps, loads, new_id, now, query_all, query_one
from agent.tools.contract import OperationResult

STATUS_RUNNING = "running"


def record(
    actor: ActorContext,
    *,
    task_id: Optional[str],
    action_id: Optional[str],
    tool_name: str,
    request_key: str,
    result: OperationResult,
    attempts: int = 1,
) -> Dict[str, Any]:
    operation_id = new_id("op")
    stamp = now()
    conn = connect()
    try:
        existing = query_one(
            conn, "SELECT * FROM agent_operation WHERE request_key = ?", (request_key,)
        )
        if existing:
            conn.execute(
                "UPDATE agent_operation SET status = ?, business_refs_json = ?, "
                "outputs_json = ?, item_results_json = ?, error_json = ?, is_stub = ?, "
                "attempts = ?, updated_at = ? WHERE id = ?",
                (
                    result.status,
                    dumps(result.business_refs),
                    dumps(result.outputs),
                    dumps(result.item_results),
                    dumps(result.error) if result.error else None,
                    int(result.is_stub),
                    attempts,
                    stamp,
                    existing["id"],
                ),
            )
            conn.commit()
            row = query_one(conn, "SELECT * FROM agent_operation WHERE id = ?", (existing["id"],))
            return _view(row)
        conn.execute(
            "INSERT INTO agent_operation "
            "(id, task_id, action_id, tool_name, request_key, operation_id, status, "
            " business_refs_json, outputs_json, item_results_json, error_json, is_stub, "
            " attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                operation_id,
                task_id,
                action_id,
                tool_name,
                request_key,
                result.operation_id,
                result.status,
                dumps(result.business_refs),
                dumps(result.outputs),
                dumps(result.item_results),
                dumps(result.error) if result.error else None,
                int(result.is_stub),
                attempts,
                stamp,
                stamp,
            ),
        )
        conn.commit()
        row = query_one(conn, "SELECT * FROM agent_operation WHERE id = ?", (operation_id,))
    finally:
        conn.close()
    return _view(row)


def mark_unknown(
    actor: ActorContext,
    *,
    task_id: Optional[str],
    action_id: Optional[str],
    tool_name: str,
    request_key: str,
    message: str,
) -> Dict[str, Any]:
    return record(
        actor,
        task_id=task_id,
        action_id=action_id,
        tool_name=tool_name,
        request_key=request_key,
        result=OperationResult(
            status="unknown",
            error={"code": Code.EXECUTION_UNKNOWN, "message": message},
            execution_state=ExecutionState.UNKNOWN,
        ),
    )


def refresh_status(operation_id: str, result: OperationResult) -> Dict[str, Any]:
    conn = connect()
    try:
        conn.execute(
            "UPDATE agent_operation SET status = ?, business_refs_json = ?, "
            "outputs_json = ?, item_results_json = ?, error_json = ?, updated_at = ? "
            "WHERE id = ?",
            (
                result.status,
                dumps(result.business_refs),
                dumps(result.outputs),
                dumps(result.item_results),
                dumps(result.error) if result.error else None,
                now(),
                operation_id,
            ),
        )
        conn.commit()
        row = query_one(conn, "SELECT * FROM agent_operation WHERE id = ?", (operation_id,))
    finally:
        conn.close()
    return _view(row)


def get(operation_id: str) -> Optional[Dict[str, Any]]:
    conn = connect()
    try:
        row = query_one(conn, "SELECT * FROM agent_operation WHERE id = ?", (operation_id,))
    finally:
        conn.close()
    return _view(row) if row else None


def get_by_action(action_id: str) -> Optional[Dict[str, Any]]:
    conn = connect()
    try:
        row = query_one(
            conn, "SELECT * FROM agent_operation WHERE action_id = ?", (action_id,)
        )
    finally:
        conn.close()
    return _view(row) if row else None


def list_for_task(task_id: str) -> List[Dict[str, Any]]:
    conn = connect()
    try:
        rows = query_all(
            conn,
            "SELECT * FROM agent_operation WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        )
    finally:
        conn.close()
    return [_view(row) for row in rows]


def _view(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not row:
        raise AgentError(Code.NOT_FOUND, "操作记录不存在", http_status=404)
    return {
        "id": row["id"],
        "task_id": row.get("task_id"),
        "action_id": row.get("action_id"),
        "tool_name": row["tool_name"],
        "request_key": row["request_key"],
        "operation_id": row.get("operation_id"),
        "status": row["status"],
        "business_refs": loads(row.get("business_refs_json"), []),
        "outputs": loads(row.get("outputs_json")),
        "item_results": loads(row.get("item_results_json"), []),
        "error": loads(row.get("error_json")) if row.get("error_json") else None,
        "is_stub": bool(row.get("is_stub")),
        "attempts": int(row.get("attempts") or 1),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

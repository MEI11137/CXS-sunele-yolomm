"""动作确认与防重执行（B-05）。

* 每个需要确认的动作持久化：目标项目、参数、预览、工具版本、资源版本；
* 确认时用 ``UPDATE ... WHERE status='pending' AND revision=?`` **原子领取执行权**，
  重复点击只会拿到同一份结果，不会重复导入/回流/训练；
* 目标或资源版本变化后旧确认自动失效（``superseded``），再确认返回 ``ACTION_STALE``；
* ``request_key`` 由任务、工具、参数与版本哈希得到，用来向第三人做业务幂等。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from agent.config import settings
from agent.errors import AgentError, Code, ExecutionState
from agent.identity import ActorContext
from agent.store import connect, dumps, loads, new_id, now, query_all, query_one
from agent import tasks as task_store

STATUS_PENDING = "pending"
STATUS_EXECUTING = "executing"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"
STATUS_SUPERSEDED = "superseded"
STATUS_UNKNOWN = "unknown"

OPEN_STATUSES = (STATUS_PENDING, STATUS_EXECUTING)


def build_request_key(
    task_id: str,
    tool_name: str,
    tool_version: str,
    parameters: Dict[str, Any],
    resource_revisions: Dict[str, Any],
    attempt: int = 0,
) -> str:
    canonical = json.dumps(
        {
            "task": task_id,
            "tool": tool_name,
            "version": tool_version,
            "parameters": parameters or {},
            "revisions": resource_revisions or {},
            "attempt": attempt,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return "req_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:40]


def summarize(tool_title: str, parameters: Dict[str, Any], targets: List[str]) -> str:
    parts = [tool_title or "执行操作"]
    keys = [k for k in parameters.keys() if not k.startswith("_")]
    if keys:
        brief = "、".join(keys[:4])
        parts.append(f"参数：{brief}")
    if targets:
        parts.append("目标项目：" + "、".join(targets))
    return "；".join(parts)


def create_action(
    actor: ActorContext,
    *,
    task_id: str,
    tool_name: str,
    tool_version: str,
    tool_title: str,
    parameters: Dict[str, Any],
    preview: Optional[Dict[str, Any]] = None,
    target_projects: Optional[List[str]] = None,
    resource_revisions: Optional[Dict[str, Any]] = None,
    revision: int = 1,
    prepared_id: Optional[str] = None,
    automatic_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """创建确认卡。

    ``prepared_id`` 是业务侧（C）在 prepare 阶段生成的预备记录 ID。确认后
    执行必须复用它，不能重新预备一份不同预览；这里随动作一起持久化。
    """
    task = task_store.get_task(actor, task_id)
    revisions = resource_revisions or {}
    request_key = build_request_key(
        task_id, tool_name, tool_version, parameters, revisions
    )
    # 标题随预览一起持久化，避免前端只能看到工具内部名
    preview_payload = dict(preview or {})
    preview_payload.setdefault("tool_title", tool_title)
    if automatic_mode:
        preview_payload['approval'] = {'source': 'session_policy', 'mode': automatic_mode}

    conn = connect()
    try:
        existing = query_one(
            conn, "SELECT * FROM agent_action WHERE request_key = ?", (request_key,)
        )
        if existing:
            # 同一 request_key 视为同一次动作，直接复用（含已执行结果）
            if existing["parameters_json"] != dumps(parameters):
                raise AgentError(
                    Code.ACTION_REQUEST_KEY_CONFLICT,
                    "同一请求键对应了不同参数，拒绝执行",
                    http_status=409,
                )
            if automatic_mode and existing['status'] == STATUS_PENDING:
                # A fresh server-side preview replaces the old pending snapshot under
                # the same request key. This never revives rejected/completed actions.
                conn.execute('UPDATE agent_action SET preview_json=?,prepared_id=?,created_at=?,updated_at=? WHERE id=? AND status=?',
                             (dumps(preview_payload), prepared_id, now(), now(), existing['id'], STATUS_PENDING))
                conn.commit()
                existing = query_one(conn, 'SELECT * FROM agent_action WHERE id=?', (existing['id'],))
            return _action_view(existing)

        action_id = new_id("act")
        stamp = now()
        conn.execute(
            "INSERT INTO agent_action "
            "(id, task_id, session_id, project_id, owner_id, tool_name, tool_version, "
            " parameters_json, parameters_summary, preview_json, target_projects_json, "
            " resource_revisions_json, status, revision, request_key, execution_state, "
            " prepared_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                action_id,
                task_id,
                task["session_id"],
                task["project_id"],
                actor.user_id,
                tool_name,
                tool_version,
                dumps(parameters),
                summarize(tool_title, parameters, target_projects or []),
                dumps(preview_payload),
                dumps(target_projects or []),
                dumps(revisions),
                STATUS_PENDING,
                revision,
                request_key,
                ExecutionState.NOT_EXECUTED,
                prepared_id,
                stamp,
                stamp,
            ),
        )
        conn.commit()
        row = query_one(conn, "SELECT * FROM agent_action WHERE id = ?", (action_id,))
    finally:
        conn.close()

    if automatic_mode:
        return _action_view(row)
    task_store.append_event(
        task["session_id"],
        task_id,
        task_store.EVENT_ACTION_REQUIRED,
        {
            "action": _action_view(row),
            "summary": row["parameters_summary"],
            "preview": loads(row["preview_json"]),
        },
    )
    task_store.set_status(
        task_id, task_store.STATUS_AWAITING_ACTION, summary="等待用户确认动作", emit=True
    )
    return _action_view(row)


def get_action(actor: ActorContext, action_id: str) -> Dict[str, Any]:
    conn = connect()
    try:
        row = query_one(conn, "SELECT * FROM agent_action WHERE id = ?", (action_id,))
    finally:
        conn.close()
    if not row or int(row["owner_id"]) != actor.user_id:
        raise AgentError(Code.ACTION_NOT_FOUND, "动作不存在", http_status=404)
    return _action_view(row)


def list_actions(
    task_id: str, statuses: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM agent_action WHERE task_id = ?"
    params: List[Any] = [task_id]
    status_list = [s for s in (statuses or []) if s]
    if status_list:
        sql += " AND status IN (" + ",".join("?" for _ in status_list) + ")"
        params.extend(status_list)
    sql += " ORDER BY created_at"
    conn = connect()
    try:
        rows = query_all(conn, sql, params)
    finally:
        conn.close()
    return [_action_view(row) for row in rows]


def _is_expired(row: Dict[str, Any]) -> bool:
    ttl = settings().action_ttl_seconds
    if ttl <= 0:
        return False
    created = str(row.get("created_at") or "")
    if not created:
        return False
    from datetime import datetime

    try:
        age = (datetime.now() - datetime.fromisoformat(created)).total_seconds()
    except ValueError:
        return False
    return age > ttl


def check_still_valid(row: Dict[str, Any]) -> None:
    """确认前核对：目标资源版本未变、未超时。"""
    stored = loads(row.get("resource_revisions_json"))
    if stored:
        conn = connect()
        try:
            for resource_id, revision in stored.items():
                current = query_one(
                    conn, "SELECT revision FROM agent_resource WHERE id = ?", (resource_id,)
                )
                if not current:
                    raise AgentError(
                        Code.ACTION_TARGET_CHANGED,
                        "确认涉及的目标资源已不存在，请重新发起",
                        http_status=409,
                        details={"resource_id": resource_id},
                    )
                if int(current["revision"]) != int(revision):
                    raise AgentError(
                        Code.ACTION_TARGET_CHANGED,
                        "确认涉及的目标资源已更新，需要重新确认",
                        http_status=409,
                        details={"resource_id": resource_id},
                    )
        finally:
            conn.close()
    if _is_expired(row):
        raise AgentError(
            Code.ACTION_STALE,
            "该确认已超过有效期，请重新发起该操作",
            http_status=409,
            execution_state=ExecutionState.NOT_EXECUTED,
        )


def confirm(actor: ActorContext, action_id: str, *, expected_revision: Optional[int] = None,
            decision_source: str = 'human') -> Dict[str, Any]:
    """领取执行权。返回 ``{"claimed": bool, "action": {...}}``。"""
    row = get_action(actor, action_id)
    if row["status"] in (STATUS_REJECTED,):
        raise AgentError(Code.ACTION_ALREADY_DECIDED, "该动作已被拒绝", http_status=409)
    if row["status"] in (STATUS_SUPERSEDED,):
        raise AgentError(Code.ACTION_STALE, "该确认已失效，请按最新目标重新确认", http_status=409)

    try:
        check_still_valid({**row, "resource_revisions_json": dumps(row["resource_revisions"])})
    except AgentError:
        _force_status(action_id, STATUS_SUPERSEDED, error_code=Code.ACTION_STALE)
        raise

    if expected_revision is not None and int(expected_revision) != int(row["revision"]):
        raise AgentError(
            Code.ACTION_STALE,
            "确认版本与当前动作不一致，请刷新后重试",
            http_status=409,
        )

    stamp = now()
    conn = connect()
    try:
        if row["status"] == STATUS_PENDING:
            cursor = conn.execute(
                "UPDATE agent_action SET status = ?, claimed_at = ?, decided_at = ?, "
                "decided_by = ?, updated_at = ? "
                "WHERE id = ? AND status = ? AND revision = ?",
                (
                    STATUS_EXECUTING,
                    stamp,
                    stamp,
                    actor.user_id,
                    stamp,
                    action_id,
                    STATUS_PENDING,
                    int(row["revision"]),
                ),
            )
            conn.commit()
            claimed = cursor.rowcount == 1
        else:
            claimed = False
        fresh = query_one(conn, "SELECT * FROM agent_action WHERE id = ?", (action_id,))
    finally:
        conn.close()

    task_store.append_event(
        row["session_id"],
        row["task_id"],
        task_store.EVENT_ACTION_DECIDED,
        {
            "action_id": action_id,
            "decision": "policy_approved" if decision_source == 'session_policy' else "confirmed",
            "decision_source": decision_source,
            "claimed": claimed,
            "decided_by": actor.user_id,
        },
    )
    if claimed:
        task_store.set_status(
            row["task_id"], task_store.STATUS_RUNNING, summary="动作已确认，正在执行", emit=True
        )
    return {"claimed": claimed, "action": _action_view(fresh)}


def reject(actor: ActorContext, action_id: str, reason: str = "") -> Dict[str, Any]:
    row = get_action(actor, action_id)
    if row["status"] not in (STATUS_PENDING,):
        raise AgentError(
            Code.ACTION_ALREADY_DECIDED,
            "该动作已经处理过，不能再次拒绝",
            http_status=409,
            details={"status": row["status"]},
        )
    conn = connect()
    try:
        cursor = conn.execute(
            "UPDATE agent_action SET status = ?, decided_at = ?, decided_by = ?, "
            "error_code = ?, updated_at = ? WHERE id = ? AND status = ?",
            (
                STATUS_REJECTED,
                now(),
                actor.user_id,
                reason or "用户拒绝",
                now(),
                action_id,
                STATUS_PENDING,
            ),
        )
        conn.commit()
        changed = cursor.rowcount == 1
        fresh = query_one(conn, "SELECT * FROM agent_action WHERE id = ?", (action_id,))
    finally:
        conn.close()
    task_store.append_event(
        row["session_id"],
        row["task_id"],
        task_store.EVENT_ACTION_DECIDED,
        {"action_id": action_id, "decision": "rejected", "reason": reason},
    )
    if changed:
        task = task_store.get_task_by_id(row['task_id'])
        if task['status'] not in task_store.TERMINAL_STATUSES:
            pending = list_actions(row['task_id'], [STATUS_PENDING])
            task_store.clear_waiting(row['task_id'])
            task_store.set_status(row['task_id'], task_store.STATUS_AWAITING_ACTION if pending else task_store.STATUS_AWAITING_INPUT,
                                  summary='动作被拒绝，等待新的指令', emit=True, force=True)
    return _action_view(fresh)


def mark_result(
    action_id: str,
    *,
    status: str,
    result: Optional[Dict[str, Any]] = None,
    error_code: Optional[str] = None,
    execution_state: str = ExecutionState.NOT_EXECUTED,
    operation_id: Optional[str] = None,
) -> Dict[str, Any]:
    conn = connect()
    try:
        conn.execute(
            "UPDATE agent_action SET status = ?, result_json = ?, error_code = ?, "
            "execution_state = ?, operation_id = ?, updated_at = ? WHERE id = ?",
            (
                status,
                dumps(result or {}),
                error_code,
                execution_state,
                operation_id,
                now(),
                action_id,
            ),
        )
        conn.commit()
        row = query_one(conn, "SELECT * FROM agent_action WHERE id = ?", (action_id,))
    finally:
        conn.close()
    return _action_view(row)


def supersede_pending(task_id: str, reason: str, *, exclude: Optional[str] = None) -> int:
    """目标/参数变化时把**未开始**的旧确认作废；已领取执行权的动作另行处理。"""
    conn = connect()
    try:
        sql = "UPDATE agent_action SET status = ?, error_code = ?, updated_at = ? " \
              "WHERE task_id = ? AND status = ?"
        params: List[Any] = [
            STATUS_SUPERSEDED,
            reason,
            now(),
            task_id,
            STATUS_PENDING,
        ]
        if exclude:
            sql += " AND id != ?"
            params.append(exclude)
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def list_executing(task_id: str) -> List[Dict[str, Any]]:
    return list_actions(task_id, [STATUS_EXECUTING])


def _force_status(action_id: str, status: str, *, error_code: Optional[str] = None) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE agent_action SET status = ?, error_code = ?, updated_at = ? WHERE id = ?",
            (status, error_code, now(), action_id),
        )
        conn.commit()
    finally:
        conn.close()


def attach_prepared(
    action_id: str,
    prepared_id: str,
    preview: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """确认之后补上业务预备记录（外部工具只能在这个时点 prepare，R4）。

    只允许给尚未执行的动作补；已经执行过的不再改动，避免把结果和新的预备
    记录混在一起。
    """
    conn = connect()
    try:
        row = query_one(conn, "SELECT * FROM agent_action WHERE id = ?", (action_id,))
        if not row:
            raise AgentError(Code.ACTION_NOT_FOUND, "动作不存在", http_status=404)
        if row["status"] not in (STATUS_PENDING, STATUS_EXECUTING):
            raise AgentError(
                Code.ACTION_STALE,
                "该动作已经处理过，不能再补预备记录",
                http_status=409,
            )
        merged = dict(loads(row.get("preview_json")) or {})
        if preview:
            merged.update(preview)
        if prepared_id:
            merged.setdefault("prepared_id", prepared_id)
        conn.execute(
            "UPDATE agent_action SET prepared_id = ?, preview_json = ?, updated_at = ? "
            "WHERE id = ?",
            (prepared_id or row.get("prepared_id"), dumps(merged), now(), action_id),
        )
        conn.commit()
        updated = query_one(conn, "SELECT * FROM agent_action WHERE id = ?", (action_id,))
    finally:
        conn.close()
    return _action_view(updated)


def mark_unknown(action_id: str, reason: str) -> Dict[str, Any]:
    return mark_result(
        action_id,
        status=STATUS_UNKNOWN,
        error_code=Code.EXECUTION_UNKNOWN,
        execution_state=ExecutionState.UNKNOWN,
        result={"message": reason},
    )


def _action_view(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not row:
        raise AgentError(Code.ACTION_NOT_FOUND, "动作不存在", http_status=404)
    preview = loads(row.get("preview_json")) or {}
    return {
        "action_id": row["id"],
        "id": row["id"],
        "task_id": row["task_id"],
        "session_id": row["session_id"],
        "project_id": row["project_id"],
        "tool_name": row["tool_name"],
        "tool_title": preview.get("tool_title") or row["tool_name"],
        "tool_version": row["tool_version"],
        "parameters": loads(row.get("parameters_json")),
        "parameters_summary": row["parameters_summary"],
        "preview": preview,
        "target_projects": loads(row.get("target_projects_json"), []),
        "resource_revisions": loads(row.get("resource_revisions_json")),
        "status": row["status"],
        "revision": int(row["revision"]),
        "request_key": row["request_key"],
        "result": loads(row.get("result_json")),
        "error_code": row.get("error_code"),
        "execution_state": row["execution_state"],
        "prepared_id": row.get("prepared_id"),
        "operation_id": row.get("operation_id"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

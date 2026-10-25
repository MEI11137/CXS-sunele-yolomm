"""任务、状态机与事件流（B-03）。

任务快照字段与第一人任务书一致：``id, session_id, project_id, status, steps,
waiting_for, business_refs``；事件带**会话内递增序号**，用于断线续取与去重。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from agent.errors import AgentError, Code
from agent.identity import ActorContext
from agent.store import (
    connect,
    dumps,
    loads,
    new_id,
    now,
    query_all,
    query_one,
    session_scope,
)

# 任务状态（开始开发前一次性核对，之后不得无通知更名）
STATUS_PLANNING = "planning"
STATUS_AWAITING_INPUT = "awaiting_input"
STATUS_AWAITING_PLAN = "awaiting_plan"
STATUS_RUNNING = "running"
STATUS_AWAITING_ACTION = "awaiting_action"
STATUS_AWAITING_HUMAN = "awaiting_human"
STATUS_AWAITING_BUSINESS = "awaiting_business"
STATUS_COMPLETED = "completed"
STATUS_PARTIAL_FAILED = "partial_failed"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"
STATUS_INTERRUPTED = "interrupted"
STATUS_UNKNOWN = "unknown"

ALL_STATUSES = (
    STATUS_PLANNING,
    STATUS_AWAITING_INPUT,
    STATUS_AWAITING_PLAN,
    STATUS_RUNNING,
    STATUS_AWAITING_ACTION,
    STATUS_AWAITING_HUMAN,
    STATUS_AWAITING_BUSINESS,
    STATUS_COMPLETED,
    STATUS_PARTIAL_FAILED,
    STATUS_FAILED,
    STATUS_STOPPED,
    STATUS_INTERRUPTED,
    STATUS_UNKNOWN,
)

TERMINAL_STATUSES = {
    STATUS_COMPLETED,
    STATUS_PARTIAL_FAILED,
    STATUS_FAILED,
    STATUS_STOPPED,
}

WAITING_STATUSES = {
    STATUS_AWAITING_INPUT,
    STATUS_AWAITING_PLAN,
    STATUS_AWAITING_ACTION,
    STATUS_AWAITING_HUMAN,
    STATUS_AWAITING_BUSINESS,
}

# 给用户看的表述：内部状态码不直接出现在对话里
STATUS_LABELS = {
    STATUS_PLANNING: "规划中",
    STATUS_AWAITING_INPUT: "等待你补充信息",
    STATUS_AWAITING_PLAN: "等待你确认计划",
    STATUS_RUNNING: "执行中",
    STATUS_AWAITING_ACTION: "等待你确认操作",
    STATUS_AWAITING_HUMAN: "等待你在页面上操作",
    STATUS_AWAITING_BUSINESS: "等待业务处理完成",
    STATUS_COMPLETED: "已完成",
    STATUS_PARTIAL_FAILED: "部分步骤失败",
    STATUS_FAILED: "失败",
    STATUS_STOPPED: "已停止",
    STATUS_INTERRUPTED: "已中断，需核实",
    STATUS_UNKNOWN: "状态待核实",
}

# 目标变更后、重新规划前的任务摘要（用于避免把"等重规划"误判成已完成）
SUMMARY_REPLAN = "目标变更，等待重新规划"

# 事件类型
EVENT_TASK_CREATED = "task.created"
EVENT_STATUS_CHANGED = "task.status"
EVENT_PLAN_PROPOSED = "task.plan_proposed"
EVENT_PLAN_CONFIRMED = "task.plan_confirmed"
EVENT_MESSAGE = "message"
EVENT_ASSISTANT_MESSAGE = "message.assistant"
EVENT_QUESTION = "message.question"
EVENT_TOOL_REQUESTED = "tool.requested"
EVENT_TOOL_RESULT = "tool.result"
EVENT_TOOL_ERROR = "tool.error"
EVENT_ACTION_REQUIRED = "action.required"
EVENT_ACTION_DECIDED = "action.decided"
EVENT_ACTION_EXECUTED = "action.executed"
EVENT_WAITING = "task.waiting"
EVENT_RESUMED = "task.resumed"
EVENT_PROGRESS = "task.progress"
EVENT_STOPPED = "task.stopped"
EVENT_INTERRUPTED = "task.interrupted"
EVENT_NOTE_UPDATED = "task.note_updated"
EVENT_BUDGET_EXCEEDED = "task.budget_exceeded"


# --------------------------------------------------------------------------- #
# 事件
# --------------------------------------------------------------------------- #
def append_event(
    session_id: str,
    task_id: Optional[str],
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """写入事件并分配会话内递增序号（``BEGIN IMMEDIATE`` 保证不重号）。"""
    stamp = now()
    with session_scope(immediate=True) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM agent_event "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        sequence = int(row["next"])
        event_id = new_id("evt")
        conn.execute(
            "INSERT INTO agent_event "
            "(id, session_id, task_id, sequence, type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                session_id,
                task_id,
                sequence,
                event_type,
                dumps(payload or {}),
                stamp,
            ),
        )
        conn.execute(
            "UPDATE agent_session SET updated_at = ? WHERE id = ?", (stamp, session_id)
        )
    return {
        "event_id": event_id,
        "sequence": sequence,
        "task_id": task_id,
        "type": event_type,
        "payload": payload or {},
        "created_at": stamp,
    }


def list_events(
    session_id: str,
    after_sequence: int = 0,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    conn = connect()
    try:
        rows = query_all(
            conn,
            "SELECT * FROM agent_event WHERE session_id = ? AND sequence > ? "
            "ORDER BY sequence LIMIT ?",
            (session_id, int(after_sequence or 0), max(1, min(int(limit or 200), 500))),
        )
    finally:
        conn.close()
    return [_event_view(row) for row in rows]


def latest_sequence(session_id: str) -> int:
    conn = connect()
    try:
        row = query_one(
            conn,
            "SELECT COALESCE(MAX(sequence), 0) AS seq FROM agent_event WHERE session_id = ?",
            (session_id,),
        )
    finally:
        conn.close()
    return int(row["seq"]) if row else 0


def _event_view(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "event_id": row["id"],
        "sequence": int(row["sequence"]),
        "task_id": row.get("task_id"),
        "type": row["type"],
        "payload": loads(row.get("payload_json")),
        "created_at": row["created_at"],
    }


# --------------------------------------------------------------------------- #
# 任务
# --------------------------------------------------------------------------- #
def create_task(
    actor: ActorContext,
    session_id: str,
    project_id: str,
    goal: str,
    *,
    manual_id: Optional[str] = None,
    manual_version: Optional[int] = None,
) -> Dict[str, Any]:
    task_id = new_id("tsk")
    stamp = now()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO agent_task "
            "(id, session_id, project_id, owner_id, goal, status, steps_json, "
            " business_refs_json, manual_id, manual_version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, '[]', '[]', ?, ?, ?, ?)",
            (
                task_id,
                session_id,
                project_id,
                actor.user_id,
                goal,
                STATUS_PLANNING,
                manual_id,
                manual_version,
                stamp,
                stamp,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    append_event(
        session_id,
        task_id,
        EVENT_TASK_CREATED,
        {"goal": goal, "manual_id": manual_id, "manual_version": manual_version},
    )
    return get_task(actor, task_id)


def get_task(actor: ActorContext, task_id: str) -> Dict[str, Any]:
    conn = connect()
    try:
        row = query_one(conn, "SELECT * FROM agent_task WHERE id = ?", (task_id,))
    finally:
        conn.close()
    if not row or int(row["owner_id"]) != actor.user_id:
        raise AgentError(Code.TASK_NOT_FOUND, "任务不存在", http_status=404)
    return _task_view(row)


def find_active_task(actor: ActorContext, session_id: str) -> Optional[Dict[str, Any]]:
    """会话里最近一个未结束的任务。"""
    conn = connect()
    try:
        row = query_one(
            conn,
            "SELECT * FROM agent_task WHERE session_id = ? AND owner_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (session_id, actor.user_id),
        )
    finally:
        conn.close()
    if not row:
        return None
    task = _task_view(row)
    return None if task["status"] in TERMINAL_STATUSES else task


def list_tasks(
    actor: ActorContext,
    project_id: Optional[str] = None,
    statuses: Optional[Iterable[str]] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM agent_task WHERE owner_id = ?"
    params: List[Any] = [actor.user_id]
    if project_id:
        sql += " AND project_id = ?"
        params.append(project_id)
    status_list = [s for s in (statuses or []) if s]
    if status_list:
        sql += " AND status IN (" + ",".join("?" for _ in status_list) + ")"
        params.extend(status_list)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(int(limit or 50), 200)))
    conn = connect()
    try:
        rows = query_all(conn, sql, params)
    finally:
        conn.close()
    return [_task_view(row) for row in rows]


def set_status(
    task_id: str,
    status: str,
    *,
    summary: Optional[str] = None,
    emit: bool = True,
    extra: Optional[Dict[str, Any]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    if status not in ALL_STATUSES:
        raise AgentError(Code.VALIDATION, f"未知任务状态：{status}")
    conn = connect()
    try:
        row = query_one(conn, "SELECT * FROM agent_task WHERE id = ?", (task_id,))
        if not row:
            raise AgentError(Code.TASK_NOT_FOUND, "任务不存在", http_status=404)
        previous = row["status"]
        if previous in TERMINAL_STATUSES and not force and status != previous:
            raise AgentError(
                Code.TASK_STATE_CONFLICT,
                "任务已经结束，不能改为其他状态",
                http_status=409,
                details={"from": previous, "to": status},
            )
        stamp = now()
        finished = stamp if status in TERMINAL_STATUSES else None
        fields = ["status = ?", "updated_at = ?"]
        params: List[Any] = [status, stamp]
        if summary is not None:
            fields.append("summary = ?")
            params.append(summary)
        if finished:
            fields.append("finished_at = ?")
            params.append(finished)
        params.append(task_id)
        conn.execute(f"UPDATE agent_task SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
        session_id = row["session_id"]
    finally:
        conn.close()

    if emit and previous != status:
        payload = {"from": previous, "to": status}
        if summary:
            payload["summary"] = summary
        if extra:
            payload.update(extra)
        append_event(session_id, task_id, EVENT_STATUS_CHANGED, payload)
    return get_task_by_id(task_id)


def get_task_by_id(task_id: str) -> Dict[str, Any]:
    """内部使用：已通过上层归属校验后按 ID 取任务。"""
    conn = connect()
    try:
        row = query_one(conn, "SELECT * FROM agent_task WHERE id = ?", (task_id,))
    finally:
        conn.close()
    if not row:
        raise AgentError(Code.TASK_NOT_FOUND, "任务不存在", http_status=404)
    return _task_view(row)


def set_plan(
    task_id: str,
    *,
    goal: Optional[str],
    steps: List[Dict[str, Any]],
    expected_outputs: Optional[List[str]] = None,
    open_questions: Optional[List[str]] = None,
    manual_id: Optional[str] = None,
    manual_version: Optional[int] = None,
) -> Dict[str, Any]:
    task = get_task_by_id(task_id)
    plan_version = int(task["plan_version"]) + 1
    plan = {
        "version": plan_version,
        "goal": goal or task["goal"],
        "steps": steps,
        "expected_outputs": expected_outputs or [],
        "open_questions": open_questions or [],
        "proposed_at": now(),
    }
    normalised_steps = []
    for index, step in enumerate(steps):
        normalised_steps.append(
            {
                "id": step.get("id") or f"s{index + 1}",
                "index": index,
                "title": step.get("title") or f"步骤 {index + 1}",
                "status": step.get("status") or "pending",
                "note": step.get("note") or "",
                "needs_confirmation": bool(step.get("needs_confirmation")),
                "needs_human": bool(step.get("needs_human")),
                "business_ref": step.get("business_ref"),
            }
        )
    fields = [
        "plan_json = ?",
        "plan_version = ?",
        "plan_confirmed_at = NULL",
        "steps_json = ?",
        "updated_at = ?",
    ]
    params: List[Any] = [dumps(plan), plan_version, dumps(normalised_steps), now()]
    if manual_id:
        fields.append("manual_id = ?")
        params.append(manual_id)
    if manual_version is not None:
        fields.append("manual_version = ?")
        params.append(manual_version)
    params.append(task_id)
    conn = connect()
    try:
        conn.execute(f"UPDATE agent_task SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
    finally:
        conn.close()
    append_event(
        task["session_id"], task_id, EVENT_PLAN_PROPOSED, {"plan": plan, "steps": normalised_steps}
    )
    return set_status(task_id, STATUS_AWAITING_PLAN, summary="等待确认整体计划", emit=True)


def confirm_plan(task_id: str, decided_by: int) -> Dict[str, Any]:
    task = get_task_by_id(task_id)
    if task["status"] not in (STATUS_AWAITING_PLAN, STATUS_PLANNING):
        raise AgentError(
            Code.TASK_STATE_CONFLICT,
            "当前任务不在等待确认计划的状态",
            http_status=409,
            details={"status": task["status"]},
        )
    if not task.get("plan"):
        raise AgentError(Code.PLAN_NOT_CONFIRMED, "任务还没有可确认的计划")
    conn = connect()
    try:
        conn.execute(
            "UPDATE agent_task SET plan_confirmed_at = ?, updated_at = ? WHERE id = ?",
            (now(), now(), task_id),
        )
        conn.commit()
    finally:
        conn.close()
    append_event(
        task["session_id"],
        task_id,
        EVENT_PLAN_CONFIRMED,
        {"plan_version": task["plan_version"], "decided_by": decided_by},
    )
    return set_status(task_id, STATUS_RUNNING, summary="计划已确认，开始执行")


def update_steps(task_id: str, steps: List[Dict[str, Any]]) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE agent_task SET steps_json = ?, updated_at = ? WHERE id = ?",
            (dumps(steps), now(), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_step(
    task_id: str,
    step_id: str,
    status: str,
    note: str = "",
    business_ref: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    task = get_task_by_id(task_id)
    steps = task["steps"]
    found = False
    for step in steps:
        if step.get("id") == step_id:
            step["status"] = status
            if note:
                step["note"] = note
            if business_ref:
                step["business_ref"] = business_ref
            found = True
            break
    if not found:
        raise AgentError(Code.VALIDATION, f"计划里没有步骤 {step_id}")
    update_steps(task_id, steps)
    return get_task_by_id(task_id)


def set_waiting(
    task_id: str,
    *,
    kind: str,
    reason: str,
    refs: Optional[List[Dict[str, Any]]] = None,
    conditions: Optional[List[Dict[str, Any]]] = None,
    page_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """记录"在等什么"，恢复时据此核实真实业务状态，不靠用户口头声明。"""
    waiting = {
        "kind": kind,
        "reason": reason,
        "refs": refs or [],
        "conditions": conditions or [],
        "page_hint": page_hint,
        "since": now(),
    }
    conn = connect()
    try:
        conn.execute(
            "UPDATE agent_task SET waiting_for_json = ?, updated_at = ? WHERE id = ?",
            (dumps(waiting), now(), task_id),
        )
        conn.commit()
    finally:
        conn.close()
    task = get_task_by_id(task_id)
    append_event(task["session_id"], task_id, EVENT_WAITING, waiting)
    return task


def clear_waiting(task_id: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE agent_task SET waiting_for_json = NULL, updated_at = ? WHERE id = ?",
            (now(), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def add_business_ref(task_id: str, ref: Dict[str, Any]) -> None:
    task = get_task_by_id(task_id)
    refs = task["business_refs"]
    key = (ref.get("kind"), ref.get("id"))
    for existing in refs:
        if (existing.get("kind"), existing.get("id")) == key:
            return
    refs.append(ref)
    conn = connect()
    try:
        conn.execute(
            "UPDATE agent_task SET business_refs_json = ?, updated_at = ? WHERE id = ?",
            (dumps(refs), now(), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def tasks_in_statuses(statuses: Iterable[str]) -> List[Dict[str, Any]]:
    status_list = [s for s in statuses if s]
    if not status_list:
        return []
    sql = (
        "SELECT * FROM agent_task WHERE status IN ("
        + ",".join("?" for _ in status_list)
        + ")"
    )
    conn = connect()
    try:
        rows = query_all(conn, sql, status_list)
    finally:
        conn.close()
    return [_task_view(row) for row in rows]


def _task_view(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "project_id": row["project_id"],
        "owner_id": int(row["owner_id"]),
        "goal": row["goal"],
        "status": row["status"],
        "plan": loads(row.get("plan_json"), {}) or None,
        "plan_version": int(row.get("plan_version") or 0),
        "plan_confirmed_at": row.get("plan_confirmed_at"),
        "steps": loads(row.get("steps_json"), []),
        "waiting_for": loads(row.get("waiting_for_json"), {}) or None,
        "business_refs": loads(row.get("business_refs_json"), []),
        "manual_id": row.get("manual_id"),
        "manual_version": row.get("manual_version"),
        "summary": row.get("summary"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "finished_at": row.get("finished_at"),
    }


def snapshot(task: Dict[str, Any]) -> Dict[str, Any]:
    """对外快照：字段与第一人任务书约定一致，去掉内部字段。"""
    return {
        "id": task["id"],
        "session_id": task["session_id"],
        "project_id": task["project_id"],
        "status": task["status"],
        "goal": task["goal"],
        "plan": task.get("plan"),
        "plan_version": task.get("plan_version", 0),
        "steps": task.get("steps", []),
        "waiting_for": task.get("waiting_for"),
        "business_refs": task.get("business_refs", []),
        "manual": (
            {"id": task.get("manual_id"), "version": task.get("manual_version")}
            if task.get("manual_id")
            else None
        ),
        "summary": task.get("summary"),
        "updated_at": task["updated_at"],
    }

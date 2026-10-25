"""任务交接笔记（B-08）。

定位：便于继续工作的摘要，**不是执行证明**。内容全部由持久化记录生成：
目标来自任务、决定来自用户消息、完成项来自步骤状态与真实操作记录、
待办与等待来自任务状态机。用户指出错误时，按原始记录重新核对并重建。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.errors import AgentError, Code
from agent.identity import ActorContext
from agent.store import connect, dumps, loads, now, query_all, query_one
from agent import tasks as task_store

_MAX_DECISION_CHARS = 160


def refresh(task_id: str) -> Dict[str, Any]:
    """按原始记录重建交接笔记并落库。"""
    task = task_store.get_task_by_id(task_id)
    conn = connect()
    try:
        user_messages = query_all(
            conn,
            "SELECT content, created_at FROM agent_message "
            "WHERE session_id = ? AND task_id = ? AND role = 'user' "
            "ORDER BY created_at",
            (task["session_id"], task_id),
        )
        operations = query_all(
            conn,
            "SELECT tool_name, status, business_refs_json, outputs_json, is_stub, created_at "
            "FROM agent_operation WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        )
        actions = query_all(
            conn,
            "SELECT tool_name, status, error_code FROM agent_action WHERE task_id = ? "
            "ORDER BY created_at",
            (task_id,),
        )
        existing = query_one(
            conn, "SELECT version FROM agent_handoff_note WHERE task_id = ?", (task_id,)
        )
    finally:
        conn.close()

    decisions = [
        {
            "text": str(row["content"])[:_MAX_DECISION_CHARS],
            "at": row["created_at"],
        }
        for row in user_messages[-6:]
    ]

    done_steps = [
        step for step in task["steps"] if step.get("status") in ("done", "succeeded")
    ]
    open_steps = [
        step
        for step in task["steps"]
        if step.get("status") not in ("done", "succeeded", "skipped")
    ]

    completed_operations = []
    failed_operations = []
    for row in operations:
        entry = {
            "tool": row["tool_name"],
            "status": row["status"],
            "business_refs": loads(row.get("business_refs_json"), []),
            "is_stub": bool(row.get("is_stub")),
        }
        if row["status"] in ("succeeded", "running"):
            completed_operations.append(entry)
        else:
            entry["status"] = row["status"]
            failed_operations.append(entry)

    note = {
        "goal": task["goal"],
        "project_id": task["project_id"],
        "task_status": task["status"],
        "plan_version": task["plan_version"],
        "plan_confirmed": bool(task.get("plan_confirmed_at")) if "plan_confirmed_at" in task else bool(task.get("plan_confirmed_at")),
        "manual": (
            {"id": task.get("manual_id"), "version": task.get("manual_version")}
            if task.get("manual_id")
            else None
        ),
        "decisions": decisions,
        "done_steps": [{"id": s["id"], "title": s["title"], "note": s.get("note", "")} for s in done_steps],
        "completed_operations": completed_operations,
        "failed_operations": failed_operations,
        "pending_steps": [{"id": s["id"], "title": s["title"]} for s in open_steps],
        "waiting_for": task.get("waiting_for"),
        "business_refs": task["business_refs"],
        "rejected_actions": [
            {"tool": row["tool_name"], "reason": row.get("error_code")}
            for row in actions
            if row["status"] == "rejected"
        ],
        "caveat": "本笔记是继续工作的摘要，不是业务已完成的证明；继续任务前需重新核对真实业务状态。",
        "rebuilt_at": now(),
    }

    version = int(existing["version"]) + 1 if existing else 1
    conn = connect()
    try:
        if existing:
            conn.execute(
                "UPDATE agent_handoff_note SET note_json = ?, version = ?, updated_at = ? "
                "WHERE task_id = ?",
                (dumps(note), version, now(), task_id),
            )
        else:
            conn.execute(
                "INSERT INTO agent_handoff_note (task_id, note_json, version, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (task_id, dumps(note), version, now()),
            )
        conn.commit()
    finally:
        conn.close()

    task_store.append_event(
        task["session_id"], task_id, task_store.EVENT_NOTE_UPDATED, {"version": version}
    )
    return {"task_id": task_id, "version": version, "note": note}


def get_note(task_id: str) -> Optional[Dict[str, Any]]:
    conn = connect()
    try:
        row = query_one(
            conn, "SELECT * FROM agent_handoff_note WHERE task_id = ?", (task_id,)
        )
    finally:
        conn.close()
    if not row:
        return None
    return {
        "task_id": task_id,
        "version": int(row["version"]),
        "needs_recheck": bool(row.get("needs_recheck")),
        "updated_at": row["updated_at"],
        "note": loads(row.get("note_json")),
    }


def flag_error(
    actor: ActorContext, task_id: str, description: str
) -> Dict[str, Any]:
    """用户指出笔记有误：标记待复核，再按原始记录重建，不保留错误表述。"""
    task_store.get_task(actor, task_id)  # 归属校验
    conn = connect()
    try:
        conn.execute(
            "UPDATE agent_handoff_note SET needs_recheck = 0, updated_at = ? WHERE task_id = ?",
            (now(), task_id),
        )
        conn.commit()
    finally:
        conn.close()
    task = task_store.get_task_by_id(task_id)
    task_store.append_event(
        task["session_id"],
        task_id,
        task_store.EVENT_NOTE_UPDATED,
        {"reported_error": (description or "")[:300], "action": "rebuilt_from_records"},
    )
    rebuilt = refresh(task_id)
    rebuilt["reported_error"] = description
    rebuilt["rebuilt_from_records"] = True
    return rebuilt


def note_prompt(note: Optional[Dict[str, Any]], max_chars: int = 2000) -> str:
    if not note:
        return "（本任务还没有交接笔记）"
    payload = note.get("note", note)
    text = dumps(payload)
    if len(text) > max_chars:
        text = text[:max_chars] + "…（已截断）"
    return (
        "任务交接笔记（摘要，不等于已执行证明）：\n"
        f"{text}\n"
        "如与用户描述或实际业务状态冲突，以真实业务状态为准，并说明需要重新核对。"
    )


def ensure_note(actor: ActorContext, task_id: str) -> Dict[str, Any]:
    existing = get_note(task_id)
    if existing:
        return existing
    return refresh(task_id)

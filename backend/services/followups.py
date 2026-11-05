"""人工提交之后的会话接力。

问题：Agent 让用户去页面复核，用户提交完还得回聊天里说一句“我提交了”。
这里把“等哪条记录、等什么结果、原来要做什么”存成等待项，提交时自己接上。

三条规则：

1. **登记**只在 Agent 打开某条记录的复核页时发生（``hx_open_page``），
   同一 ``(项目, 用户, 会话, 记录)`` 只有一条等待项；
2. **触发**由本人提交/驳回驱动，把等待项标 ``ready``，再为这个会话里
   **所有** ready 项合成**一轮**续跑（kind=``review_followup``），
   幂等键由“等待项 id + 版本号集合”算出来，多次提交/刷新不会唤醒多次；
3. **失效**：发新消息、停止、删会话/项目、撤权、换连接都会取消等待项，
   旧目标不会被反复唤醒。取消只改状态，不删历史。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Callable

from backend import security
from backend.agent import store as turn_store

KIND = "continuation"
REQUEST_PREFIX = "review-followup:"
# review_followups.intent 的 CHECK 只允许 notify/open_reflow/reflow_preview/reflow；
# 人工提交后的接力属于“等到真实结果再通知并继续”，用 notify。
INTENT = "notify"
ACTIVE = ("waiting", "ready", "running")


def _loads(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (ValueError, TypeError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def public(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "session_id": data["session_id"],
        "record_id": data["record_id"],
        "intent": data["intent"],
        "instruction": data["instruction"],
        "status": data["status"],
        "result": _loads(data.get("result"), {}),
        "cancel_reason": data.get("cancel_reason"),
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "completed_at": data.get("completed_at"),
    }


def register(
    conn,
    actor,
    record_id: str,
    *,
    instruction: str = "",
    payload: dict | None = None,
) -> dict:
    """登记“我在等这条记录的人工结论”。重复登记是更新，不新增行。"""
    record = str(record_id or "").strip()
    if not record:
        return {"registered": False, "reason": "缺少 record_id"}
    now = security.now_iso()
    row = conn.execute(
        "SELECT * FROM review_followups WHERE project_id = ? AND owner_id = ?"
        " AND session_id = ? AND record_id = ? AND intent = ?",
        (str(actor.project_id), int(actor.user_id), str(actor.session_id), record, INTENT),
    ).fetchone()
    text = str(instruction or "").strip()[:1000]
    data = json.dumps(payload or {}, ensure_ascii=False)
    if row is None:
        followup_id = security.new_id("rfu")
        conn.execute(
            "INSERT INTO review_followups (id, project_id, owner_id, session_id, record_id,"
            " intent, instruction, payload, mode, provider, model, fingerprint, status,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?)",
            (
                followup_id,
                str(actor.project_id),
                int(actor.user_id),
                str(actor.session_id),
                record,
                INTENT,
                text,
                data,
                str(actor.mode),
                str(actor.provider),
                str(actor.model),
                str(actor.connection_fingerprint),
                now,
                now,
            ),
        )
        conn.commit()
        return {"registered": True, "followup_id": followup_id, "status": "waiting"}
    # 已经结束的等待项 = 上一轮目标已完成；同一条记录再次打开复核，重新开始等待。
    status = "waiting" if row["status"] in {"done", "cancelled", "failed"} else row["status"]
    conn.execute(
        "UPDATE review_followups SET instruction = ?, payload = ?, mode = ?, provider = ?,"
        " model = ?, fingerprint = ?, status = ?, cancel_reason = NULL, updated_at = ?"
        " WHERE id = ?",
        (
            text or str(row["instruction"]),
            data if payload else row["payload"],
            str(actor.mode),
            str(actor.provider),
            str(actor.model),
            str(actor.connection_fingerprint),
            status,
            now,
            str(row["id"]),
        ),
    )
    conn.commit()
    return {"registered": True, "followup_id": row["id"], "status": status}


def on_submitted(
    conn,
    *,
    project_id: str,
    owner_id: int,
    record_id: str,
    version: int,
    action: str,
    outcome: str | None,
    boxes: int,
    start: Callable[[str], None] | None = None,
) -> dict:
    """本人提交/驳回后的真实事件：把等待项标 ready，并尝试接一轮。"""
    rows = conn.execute(
        "SELECT * FROM review_followups WHERE project_id = ? AND owner_id = ? AND record_id = ?"
        " AND status IN ('waiting', 'ready')",
        (str(project_id), int(owner_id), str(record_id)),
    ).fetchall()
    if not rows:
        return {"followups": 0, "started": [], "note": "这条记录没有登记的等待项，只发通知。"}
    now = security.now_iso()
    result = json.dumps(
        {"version": int(version), "action": action, "outcome": outcome, "boxes": int(boxes)},
        ensure_ascii=False,
    )
    for row in rows:
        conn.execute(
            "UPDATE review_followups SET status = 'ready', result = ?, updated_at = ? WHERE id = ?",
            (result, now, str(row["id"])),
        )
    conn.commit()
    started: list[str] = []
    for session_id in sorted({str(row["session_id"]) for row in rows}):
        started.extend(drain(conn, session_id, start=start))
    return {"followups": len(rows), "started": started}


def drain(conn, session_id: str, *, start: Callable[[str], None] | None) -> list[str]:
    """把这个会话里所有 ready 项合成一轮续跑；正在跑就等它结束再续。"""
    if start is None:
        return []
    rows = conn.execute(
        "SELECT * FROM review_followups WHERE session_id = ? AND status = 'ready'"
        " ORDER BY created_at ASC LIMIT 50",
        (str(session_id),),
    ).fetchall()
    if not rows:
        return []
    if turn_store.active_for_session(conn, str(session_id)) is not None:
        # 会话正忙：保持在 ready，等这一轮收尾时再 drain，绝不并发抢同一个会话。
        return []
    key = _request_key(rows)
    try:
        turn, created = turn_store.create(
            conn,
            session_id=str(session_id),
            project_id=str(rows[0]["project_id"]),
            owner_id=int(rows[0]["owner_id"]),
            request_id=key,
            mode=str(rows[0]["mode"]),
            provider=str(rows[0]["provider"]),
            model=str(rows[0]["model"]),
            fingerprint=str(rows[0]["fingerprint"]),
            kind=KIND,
        )
    except sqlite3.IntegrityError:
        # 已有活动轮次（并发提交抢同一会话）：留给收尾时的 drain。
        return []
    if not created:
        return []
    now = security.now_iso()
    conn.executemany(
        "UPDATE review_followups SET status = 'running', request_id = ?, updated_at = ? WHERE id = ?",
        [(key, now, str(row["id"])) for row in rows],
    )
    conn.commit()
    try:
        start(str(turn["id"]))
    except Exception:  # noqa: BLE001 - 启动失败必须回到 ready，不能让等待项卡在 running
        conn.executemany(
            "UPDATE review_followups SET status = 'ready', updated_at = ? WHERE id = ?",
            [(security.now_iso(), str(row["id"])) for row in rows],
        )
        conn.commit()
        return []
    return [str(turn["id"])]


def finish_turn(conn, turn_id: str) -> int:
    """轮次结束：把这一批等待项标 done（无论成功失败，都不能永远 running）。"""
    row = conn.execute("SELECT * FROM turns WHERE id = ?", (str(turn_id),)).fetchone()
    if row is None:
        return 0
    cursor = conn.execute(
        "UPDATE review_followups SET status = 'done', completed_at = ?, updated_at = ?"
        " WHERE session_id = ? AND request_id = ? AND status = 'running'",
        (security.now_iso(), security.now_iso(), str(row["session_id"]), str(row["request_id"])),
    )
    conn.commit()
    return cursor.rowcount


def prompt_for(conn, request_id: str) -> str | None:
    """给续跑轮生成真实事件说明；没有等待项就不该有这一轮。"""
    if not request_id:
        return None
    rows = conn.execute(
        "SELECT * FROM review_followups WHERE request_id = ? AND status IN ('running', 'done')"
        " ORDER BY created_at ASC",
        (str(request_id),),
    ).fetchall()
    if not rows:
        return None
    lines = []
    for row in rows:
        result = _loads(row["result"], {})
        action = str(result.get("action") or "")
        outcome = result.get("outcome")
        verdict = (
            "已驳回" if action == "rejected"
            else "确认有缺陷" if outcome == "defect"
            else "确认无缺陷" if outcome == "no_defect"
            else str(action or "已提交")
        )
        lines.append(
            f"- 记录 {row['record_id']}：第 {result.get('version', '?')} 版，{verdict}，"
            f"{result.get('boxes', 0)} 个框。"
            f"用户原来的要求：{row['instruction'] or '（未说明后续去向）'}"
        )
    body = "\n".join(lines)
    return (
        "【系统事件】用户刚在页面上**本人**提交了人工复核，这是真实结果，不是你的建议：\n"
        f"{body}\n\n"
        "请现在继续，不要重复已经完成的步骤：\n"
        "1) 用 hx_review_state 读这条记录的真实状态（不要凭记忆或推断）；\n"
        "2) 用户在上面已经写明回流去向（回原修订/另建数据集、父数据集、划分）时，按它继续调用真实工具；\n"
        "3) 没写清去向就不要猜，在回答里简短问一句；\n"
        "4) 不要重新检测，也不要再调用付费核查；被驳回的记录不要回流。"
    )


def cancel_session(conn, session_id: str, reason: str) -> int:
    return _cancel(conn, "session_id = ?", (str(session_id),), reason)


def cancel_project(conn, project_id: str, owner_id: int, reason: str) -> int:
    return _cancel(
        conn, "project_id = ? AND owner_id = ?", (str(project_id), int(owner_id)), reason
    )


def cancel_user(conn, owner_id: int, reason: str) -> int:
    return _cancel(conn, "owner_id = ?", (int(owner_id),), reason)


def cancel_record(conn, project_id: str, owner_id: int, record_id: str, reason: str) -> int:
    return _cancel(
        conn,
        "project_id = ? AND owner_id = ? AND record_id = ?",
        (str(project_id), int(owner_id), str(record_id)),
        reason,
    )


def _cancel(conn, where: str, params: tuple, reason: str) -> int:
    cursor = conn.execute(
        f"UPDATE review_followups SET status = 'cancelled', cancel_reason = ?, updated_at = ?"
        f" WHERE {where} AND status IN ('waiting', 'ready', 'running')",
        (str(reason)[:200], security.now_iso(), *params),
    )
    conn.commit()
    return cursor.rowcount


def _request_key(rows: list[sqlite3.Row]) -> str:
    """等待项 + 版本集 → 稳定幂等键：同一批只接一轮。"""
    seed = "|".join(
        f"{row['id']}:{_loads(row['result'], {}).get('version', 0)}" for row in rows
    )
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]
    return f"{REQUEST_PREFIX}{digest}"


__all__ = [
    "KIND",
    "REQUEST_PREFIX",
    "cancel_project",
    "cancel_record",
    "cancel_session",
    "cancel_user",
    "drain",
    "finish_turn",
    "on_submitted",
    "prompt_for",
    "public",
    "register",
]

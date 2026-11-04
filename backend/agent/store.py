"""轮次 / 事件 / 动作 / 工具任务的持久化与状态机。

状态机（``turns.status``）：

    queued ──▶ running ──▶ completed
      │           │   └──▶ failed
      │           │   └──▶ stopped
      │           └──▶ waiting_action ──▶ running（用户确认后继续）
      └──────────▶ failed（引擎启动失败等）

约束：

* ``UNIQUE(session_id, request_id)`` + 部分唯一索引保证“每会话只有一个活动轮次”，
  重复提交只会拿到同一轮，不会新建第二轮；
* 事件只追加，``seq`` 递增，前端断开后按 ``after`` 续拉；
* 状态迁移用条件 UPDATE（``WHERE status IN (...)``），并发下只有一个赢家。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from backend import security

ACTIVE_STATUSES = ("queued", "running", "waiting_action")
TERMINAL_STATUSES = ("completed", "failed", "stopped")
EVENT_LIMIT = 500


# --------------------------------------------------------------------------- 轮次


def turn_public(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "session_id": data["session_id"],
        "project_id": data["project_id"],
        "kind": data["kind"],
        "mode": data["mode"],
        "provider": data["provider"],
        "model": data["model"],
        "status": data["status"],
        "stop_requested": bool(data.get("stop_requested_at")),
        "error_code": data.get("error_code"),
        "error_message": data.get("error_message"),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
    }


def get(conn: sqlite3.Connection, turn_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM turns WHERE id = ?", (str(turn_id),)).fetchone()
    if row is None:
        raise KeyError(turn_id)
    return row


def active_for_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM turns WHERE session_id = ? AND status IN (?, ?, ?) "
        "ORDER BY created_at DESC LIMIT 1",
        (str(session_id), *ACTIVE_STATUSES),
    ).fetchone()


def by_request(conn: sqlite3.Connection, session_id: str, request_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM turns WHERE session_id = ? AND request_id = ?",
        (str(session_id), str(request_id)),
    ).fetchone()


def create(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    project_id: str,
    owner_id: int,
    request_id: str,
    mode: str,
    provider: str,
    model: str,
    fingerprint: str,
    kind: str = "user",
) -> tuple[sqlite3.Row, bool]:
    """幂等创建：同一 (session, request_id) 只存在一轮。返回 (row, created)。"""
    existing = by_request(conn, session_id, request_id)
    if existing is not None:
        return existing, False
    now = security.now_iso()
    turn_id = security.new_id("trn")
    try:
        conn.execute(
            """
            INSERT INTO turns (id, session_id, project_id, owner_id, request_id, kind, mode,
                               provider, model, connection_fingerprint, status,
                               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                turn_id,
                str(session_id),
                str(project_id),
                int(owner_id),
                str(request_id),
                kind,
                mode,
                provider,
                model,
                fingerprint,
                now,
                now,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # 要么 request_id 撞了（返回已有轮次），要么该会话已有活动轮次（由调用方报 409）。
        conn.rollback()
        again = by_request(conn, session_id, request_id)
        if again is not None:
            return again, False
        raise
    return get(conn, turn_id), True


def transition(
    conn: sqlite3.Connection, turn_id: str, *, expect: Iterable[str], to: str, **fields: Any
) -> bool:
    """条件状态迁移；返回是否真的迁移成功（并发下只有一个赢家）。"""
    expected = list(expect)
    assignments = ["status = ?", "updated_at = ?"]
    params: list[Any] = [to, security.now_iso()]
    for key, value in fields.items():
        assignments.append(f"{key} = ?")
        params.append(value)
    placeholders = ",".join("?" for _ in expected)
    params.extend([str(turn_id), *expected])
    cursor = conn.execute(
        f"UPDATE turns SET {', '.join(assignments)} WHERE id = ? AND status IN ({placeholders})",
        params,
    )
    conn.commit()
    return cursor.rowcount > 0


def mark_running(conn: sqlite3.Connection, turn_id: str) -> bool:
    return transition(
        conn, turn_id, expect=("queued",), to="running", started_at=security.now_iso()
    )


def finish(
    conn: sqlite3.Connection,
    turn_id: str,
    status: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
    expect: Iterable[str] = ACTIVE_STATUSES,
) -> bool:
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"非终态：{status}")
    return transition(
        conn,
        turn_id,
        expect=list(expect),
        to=status,
        finished_at=security.now_iso(),
        error_code=error_code,
        error_message=error_message,
    )


def request_stop(conn: sqlite3.Connection, turn_id: str) -> bool:
    """标记“希望停止”。真正停没停以引擎状态为准，由运行器回报。"""
    cursor = conn.execute(
        "UPDATE turns SET stop_requested_at = ?, updated_at = ? "
        "WHERE id = ? AND status IN (?, ?, ?) AND stop_requested_at IS NULL",
        (security.now_iso(), security.now_iso(), str(turn_id), *ACTIVE_STATUSES),
    )
    conn.commit()
    return cursor.rowcount > 0


def interrupted(conn: sqlite3.Connection, *, code: str, message: str) -> int:
    """进程重启后清理遗留的活动轮次：明确标记中断，不假装完成。"""
    cursor = conn.execute(
        "UPDATE turns SET status = 'failed', error_code = ?, error_message = ?, "
        "finished_at = ?, updated_at = ? WHERE status IN (?, ?, ?)",
        (code, message, security.now_iso(), security.now_iso(), *ACTIVE_STATUSES),
    )
    conn.commit()
    return cursor.rowcount


def interrupt_scope(
    conn: sqlite3.Connection,
    *,
    code: str,
    message: str,
    session_id: str | None = None,
    project_id: str | None = None,
    owner_id: int | None = None,
) -> int:
    """停止某个范围内的活动轮次（删会话/删项目/停用账号/换连接时用）。

    只改状态并写明原因，不伪造回答；运行器在下一次轮询复查上下文时会自行退出。
    """
    clauses = ["status IN (?, ?, ?)"]
    params: list = [*ACTIVE_STATUSES]
    if session_id:
        clauses.append("session_id = ?")
        params.append(str(session_id))
    if project_id:
        clauses.append("project_id = ?")
        params.append(str(project_id))
    if owner_id is not None:
        clauses.append("owner_id = ?")
        params.append(int(owner_id))
    now = security.now_iso()
    cursor = conn.execute(
        f"UPDATE turns SET status = 'failed', error_code = ?, error_message = ?, "
        f"finished_at = ?, updated_at = ? WHERE {' AND '.join(clauses)}",
        (code, message, now, now, *params),
    )
    conn.commit()
    return cursor.rowcount


def recent_for_session(conn: sqlite3.Connection, session_id: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM turns WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
        (str(session_id), max(1, min(int(limit), 100))),
    ).fetchall()
    return [turn_public(row) for row in rows]


# --------------------------------------------------------------------------- 事件


def event_public(row: sqlite3.Row) -> dict:
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        payload = {}
    return {
        "seq": int(row["seq"]),
        "turn_id": row["turn_id"],
        "type": row["type"],
        "payload": payload if isinstance(payload, dict) else {},
        "created_at": row["created_at"],
    }


def append_event(
    conn: sqlite3.Connection, turn_id: str, session_id: str, type_: str, payload: dict
) -> dict:
    cursor = conn.execute(
        "INSERT INTO turn_events (turn_id, session_id, type, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            str(turn_id),
            str(session_id),
            str(type_),
            json.dumps(payload, ensure_ascii=False),
            security.now_iso(),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM turn_events WHERE seq = ?", (cursor.lastrowid,)).fetchone()
    return event_public(row)


def events_after(conn: sqlite3.Connection, turn_id: str, after: int = 0) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM turn_events WHERE turn_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
        (str(turn_id), int(after), EVENT_LIMIT),
    ).fetchall()
    return [event_public(row) for row in rows]


# --------------------------------------------------------------------------- 动作


def action_public(row: sqlite3.Row) -> dict:
    def _json(value: str) -> dict:
        try:
            data = json.loads(value or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    data = dict(row)
    return {
        "id": data["id"],
        "turn_id": data["turn_id"],
        "session_id": data["session_id"],
        "tool": data["tool"],
        "risk": data["risk"],
        "title": data["title"],
        "preview": data["preview"],
        "status": data["status"],
        "result": _json(data["result"]),
        "error": (
            {"code": data["error_code"], "message": data["error_message"]}
            if data["error_code"]
            else None
        ),
        "created_at": data["created_at"],
        "decided_at": data["decided_at"],
    }


def create_action(
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    session_id: str,
    project_id: str,
    owner_id: int,
    tool: str,
    risk: str,
    title: str,
    args: dict,
    preview: str,
) -> sqlite3.Row:
    """同一轮里完全相同的待确认动作只保留一张卡，避免模型重复调用刷屏。"""
    fingerprint = json.dumps(args, ensure_ascii=False, sort_keys=True)
    existing = conn.execute(
        "SELECT * FROM actions WHERE turn_id = ? AND tool = ? AND args = ? AND status = 'proposed' "
        "ORDER BY created_at DESC LIMIT 1",
        (str(turn_id), tool, fingerprint),
    ).fetchone()
    if existing is not None:
        return existing
    now = security.now_iso()
    action_id = security.new_id("act")
    conn.execute(
        """
        INSERT INTO actions (id, turn_id, session_id, project_id, owner_id, tool, risk, title,
                             args, preview, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
        """,
        (
            action_id,
            str(turn_id),
            str(session_id),
            str(project_id),
            int(owner_id),
            tool,
            risk,
            title[:120],
            fingerprint,
            preview[:300],
            now,
            now,
        ),
    )
    conn.commit()
    return conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()


def get_action(conn: sqlite3.Connection, action_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM actions WHERE id = ?", (str(action_id),)).fetchone()


def claim_action(conn: sqlite3.Connection, action_id: str) -> bool:
    """把 proposed 抢成 executing；重复点击只有一个能成功（幂等）。"""
    cursor = conn.execute(
        "UPDATE actions SET status = 'executing', updated_at = ? WHERE id = ? AND status = 'proposed'",
        (security.now_iso(), str(action_id)),
    )
    conn.commit()
    return cursor.rowcount > 0


def settle_action(
    conn: sqlite3.Connection,
    action_id: str,
    *,
    status: str,
    result: dict | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    conn.execute(
        "UPDATE actions SET status = ?, result = ?, error_code = ?, error_message = ?, "
        "decided_at = COALESCE(decided_at, ?), updated_at = ? WHERE id = ?",
        (
            status,
            json.dumps(result or {}, ensure_ascii=False),
            error_code,
            error_message,
            security.now_iso(),
            security.now_iso(),
            str(action_id),
        ),
    )
    conn.commit()


def reject_action(conn: sqlite3.Connection, action_id: str) -> bool:
    cursor = conn.execute(
        "UPDATE actions SET status = 'rejected', decided_at = ?, updated_at = ? "
        "WHERE id = ? AND status = 'proposed'",
        (security.now_iso(), security.now_iso(), str(action_id)),
    )
    conn.commit()
    return cursor.rowcount > 0


def pending_actions(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM actions WHERE session_id = ? AND status IN ('proposed', 'executing') "
        "ORDER BY created_at DESC LIMIT 50",
        (str(session_id),),
    ).fetchall()
    return [action_public(row) for row in rows]


def latest_actions(conn: sqlite3.Connection, turn_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM actions WHERE turn_id = ? ORDER BY created_at ASC LIMIT 50",
        (str(turn_id),),
    ).fetchall()
    return [action_public(row) for row in rows]


def expire_actions(conn: sqlite3.Connection, turn_id: str) -> None:
    conn.execute(
        "UPDATE actions SET status = 'expired', updated_at = ? WHERE turn_id = ? AND status = 'proposed'",
        (security.now_iso(), str(turn_id)),
    )
    conn.commit()


# --------------------------------------------------------------------------- 工具任务


def job_public(row: sqlite3.Row) -> dict:
    try:
        result = json.loads(row["result"] or "{}")
    except ValueError:
        result = {}
    return {
        "id": row["id"],
        "tool": row["tool"],
        "status": row["status"],
        "result": result if isinstance(result, dict) else {},
        "error": (
            {"code": row["error_code"], "message": row["error_message"]}
            if row["error_code"]
            else None
        ),
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
    }


def job_create(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    turn_id: str | None,
    session_id: str | None,
    owner_id: int,
    tool: str,
    args: dict | None = None,
) -> sqlite3.Row:
    """兼容入口：只建任务，不判断是否已存在。新代码请用 :func:`job_claim`。"""
    now = security.now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO tool_jobs (id, turn_id, session_id, owner_id, tool, args, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)",
        (
            str(job_id),
            turn_id,
            session_id,
            int(owner_id),
            tool,
            json.dumps(args or {}, ensure_ascii=False, sort_keys=True),
            now,
            now,
        ),
    )
    conn.commit()
    return conn.execute("SELECT * FROM tool_jobs WHERE id = ?", (str(job_id),)).fetchone()


def claim_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    turn_id: str,
    session_id: str,
    owner_id: int,
    tool: str,
    args: dict,
) -> tuple[sqlite3.Row, bool]:
    """原子认领一次工具执行。

    ``tool_jobs.id`` 是主键，插入成功才算“抢到执行权”；并发的同一个 ``call_id``
    只有一个线程能插入成功，另一个拿到 IntegrityError 后读回既有记录，
    因此不会出现两个线程同时执行同一操作。读回时还要核对 owner/tool/参数：
    同 key 不同操作、或别的账号碰巧用了同一个 key，都按冲突拒绝而不是覆盖。
    """
    payload = json.dumps(args or {}, ensure_ascii=False, sort_keys=True)
    now = security.now_iso()
    from backend.agent.context import ToolError
    try:
        conn.execute(
            "INSERT INTO tool_jobs (id, turn_id, session_id, owner_id, tool, args, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)",
            (str(job_id), str(turn_id), str(session_id), int(owner_id), tool, payload, now, now),
        )
        conn.commit()
        return conn.execute("SELECT * FROM tool_jobs WHERE id = ?", (str(job_id),)).fetchone(), True
    except sqlite3.IntegrityError:
        conn.rollback()
        row = conn.execute("SELECT * FROM tool_jobs WHERE id = ?", (str(job_id),)).fetchone()
        if row is None:
            raise
        stored_args = str(row["args"] or "{}")
        if int(row["owner_id"]) != int(owner_id) or str(row["tool"]) != str(tool):
            raise ToolError(
                "CALL_ID_CONFLICT",
                "这个 call_id 已属于另一个工具或账号；请换一个新的 call_id 重试",
                status=409,
            )
        if stored_args != payload:
            raise ToolError(
                "CALL_ID_CONFLICT",
                "这个 call_id 之前已用于不同参数的操作；不能复用，请换新的 call_id",
                status=409,
            )
        return row, False


def job_finish(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    owner_id: int,
    status: str,
    result: dict | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    if status not in {"done", "failed"}:
        raise ValueError("任务终态只能是 done/failed")
    conn.execute(
        "UPDATE tool_jobs SET status = ?, result = ?, error_code = ?, error_message = ?, "
        "finished_at = ?, updated_at = ? WHERE id = ? AND owner_id = ?",
        (
            status,
            json.dumps(result or {}, ensure_ascii=False),
            error_code,
            error_message,
            security.now_iso(),
            security.now_iso(),
            str(job_id),
            int(owner_id),
        ),
    )
    conn.commit()


def running_jobs(conn: sqlite3.Connection, turn_id: str) -> list[sqlite3.Row]:
    """这一轮里仍被标记为“执行中”的工具任务。

    停止请求只是“请停止”，进程被杀之前已经在跑的工具不会凭空回滚；
    界面要据此如实说明，而不是笼统地写“已全部停止”。
    """
    return conn.execute(
        "SELECT id, tool FROM tool_jobs WHERE turn_id = ? AND status = 'running' "
        "ORDER BY created_at",
        (str(turn_id),),
    ).fetchall()


def fail_running_jobs(
    conn: sqlite3.Connection, turn_id: str, *, code: str, message: str
) -> list[str]:
    """把这一轮里无人能确认结果的执行中任务标成失败（返回被改的 job id）。

    引擎进程被强制结束、执行线程随之消失时，这些任务永远不会有真正的返回值，
    留在 ``running`` 会让后续 ``call_id`` 重放一直拿到“请稍后查询”。
    """
    rows = running_jobs(conn, turn_id)
    now = security.now_iso()
    for row in rows:
        conn.execute(
            "UPDATE tool_jobs SET status = 'failed', error_code = ?, error_message = ?, "
            "finished_at = ?, updated_at = ? WHERE id = ? AND status = 'running'",
            (str(code), str(message), now, now, str(row["id"])),
        )
    if rows:
        conn.commit()
    return [str(row["id"]) for row in rows]


# --------------------------------------------------------------------------- 偏好 / 引擎会话


def prefs(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute("SELECT * FROM user_prefs WHERE user_id = ?", (int(user_id),)).fetchone()
    if row is None:
        return {"mode": None, "model": None, "updated_at": None}
    return {"mode": row["mode"], "model": row["model"], "updated_at": row["updated_at"]}


def save_prefs(
    conn: sqlite3.Connection, user_id: int, *, mode: str | None, model: str | None
) -> None:
    now = security.now_iso()
    conn.execute(
        """
        INSERT INTO user_prefs (user_id, mode, model, updated_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            mode = COALESCE(excluded.mode, user_prefs.mode),
            model = COALESCE(excluded.model, user_prefs.model),
            updated_at = excluded.updated_at
        """,
        (int(user_id), mode, model, now),
    )
    conn.commit()


def clear_model_pref(conn: sqlite3.Connection, user_id: int) -> None:
    """清掉“另选的模型”，回到个人连接里保存的那个。

    ``save_prefs(model=None)`` 的语义是“这一项不改”，所以不能用它来清空；
    没有这个出口，用户一旦切过模型就再也回不到连接默认值了。
    """
    conn.execute(
        "UPDATE user_prefs SET model = NULL, updated_at = ? WHERE user_id = ?",
        (security.now_iso(), int(user_id)),
    )
    conn.commit()


def engine_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM engine_sessions WHERE session_id = ?", (str(session_id),)
    ).fetchone()


def set_engine_session(
    conn: sqlite3.Connection,
    session_id: str,
    engine_session_id: str,
    fingerprint: str,
    storage_key: str = "",
) -> None:
    """记录会话对应的 Native 会话 ID，以及它建在哪一代引擎存储上。

    ``storage_key`` 是那份隔离存储目录的指纹；缺了它就没法判断“这个 ID 在当前
    引擎里是否还存在”，视觉能力变化或存储被回收后会把失效 ID 当成有效 ID 复用。
    """
    conn.execute(
        """
        INSERT INTO engine_sessions
            (session_id, engine_session_id, fingerprint, storage_key, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            engine_session_id = excluded.engine_session_id,
            fingerprint = excluded.fingerprint,
            storage_key = excluded.storage_key,
            updated_at = excluded.updated_at
        """,
        (
            str(session_id),
            str(engine_session_id),
            str(fingerprint),
            str(storage_key or ""),
            security.now_iso(),
        ),
    )
    conn.commit()


def clear_engine_sessions(conn: sqlite3.Connection, *, owner_id: int) -> int:
    cursor = conn.execute(
        "DELETE FROM engine_sessions WHERE session_id IN "
        "(SELECT id FROM sessions WHERE owner_id = ?)",
        (int(owner_id),),
    )
    conn.commit()
    return cursor.rowcount

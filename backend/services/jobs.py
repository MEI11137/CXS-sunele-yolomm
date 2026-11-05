"""后台业务作业：长任务不占聊天轮次，也不依赖轮次还活着。

为什么要单独一套：

* 一次批量检测/AI 核查可能跑几十秒到几分钟，而模型对话早就结束了；
* 聊天轮次结束、浏览器刷新、关掉侧栏都不该让业务结果丢失；
* 但也不该让模型再写一个“自己的循环”——作业是**服务端**的确定性任务，
  模型只负责发起与查询，真正的执行在这里。

因此作业有自己的表、自己的事件流（``job_events``，按项目与会话可查）、
自己的幂等键 ``(project_id, request_key)``。前端按会话拉事件即可恢复现场，
不需要“再发一句话”来唤醒。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from backend import db, security

_settings = None
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hx17-job")
_lock = threading.RLock()

TERMINAL = {"done", "failed", "partial", "interrupted"}
# 进程重启后不可能还有真实返回值：这些状态要标成“结果未知”，而不是继续 running。
UNFINISHED = {"queued", "running"}


def interrupted(conn) -> int:
    """启动时把上一次进程遗留的未完成作业标成 interrupted。

    不自动重发：已经发出去的模型请求可能已经计费，重跑会让用户为同一件事付两次钱。
    已完成的作业保持原样，进度与逐条结果都保留。
    """
    rows = conn.execute(
        f"SELECT * FROM jobs WHERE status IN ({','.join('?' for _ in UNFINISHED)})",
        tuple(sorted(UNFINISHED)),
    ).fetchall()
    if not rows:
        return 0
    now = security.now_iso()
    for row in rows:
        conn.execute(
            "UPDATE jobs SET status = 'interrupted', error_code = ?, error_message = ?, finished_at = ? "
            "WHERE id = ? AND status IN ('queued', 'running')",
            (
                "SERVICE_RESTART",
                "服务重启时这个作业还没有结束，结果未知；不会自动重发（可能已经产生费用）。",
                now,
                str(row["id"]),
            ),
        )
    conn.commit()
    for row in rows:
        fresh = get(conn, row["id"])
        if str(fresh["status"]) == "interrupted":
            emit(conn, fresh, "job.interrupted", {"message": fresh["error_message"]})
    return len(rows)


def unfinished_for_turn(conn, turn_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs WHERE turn_id = ? AND status IN ('queued', 'running') "
        "ORDER BY created_at ASC",
        (str(turn_id),),
    ).fetchall()


def configure(settings) -> None:
    global _settings
    _settings = settings


def settings():
    """工厂阶段注入的配置快照（供后台线程使用，不依赖 Flask 应用上下文）。"""
    return _settings


def public(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "project_id": data["project_id"],
        "session_id": data.get("session_id"),
        "kind": data["kind"],
        "status": data["status"],
        "progress": _loads(data.get("progress"), {}),
        "result": _loads(data.get("result"), {}),
        "error": (
            {"code": data.get("error_code"), "message": data.get("error_message")}
            if data.get("error_code")
            else None
        ),
        "created_at": data["created_at"],
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
    }


def _loads(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (ValueError, TypeError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def create(
    conn,
    *,
    project_id: str,
    owner_id: int,
    kind: str,
    request_key: str,
    payload: dict,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> tuple[sqlite3.Row, bool]:
    """幂等创建：同一个 request_key 返回已有作业，不重复执行。"""
    key = str(request_key or "").strip()
    if not key:
        raise ValueError("request_key required")
    existing = conn.execute(
        "SELECT * FROM jobs WHERE project_id = ? AND request_key = ?",
        (str(project_id), key),
    ).fetchone()
    if existing is not None:
        return existing, False
    job_id = security.new_id("job")
    try:
        conn.execute(
            """
            INSERT INTO jobs (id, project_id, owner_id, session_id, turn_id, kind, request_key,
                              status, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                job_id,
                str(project_id),
                int(owner_id),
                session_id,
                turn_id,
                str(kind),
                key,
                json.dumps(payload, ensure_ascii=False),
                security.now_iso(),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        existing = conn.execute(
            "SELECT * FROM jobs WHERE project_id = ? AND request_key = ?",
            (str(project_id), key),
        ).fetchone()
        if existing is not None:
            return existing, False
        raise
    emit(conn, get(conn, job_id), "job.queued", {"kind": kind})
    return get(conn, job_id), True


def get(conn, job_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (str(job_id),)).fetchone()
    if row is None:
        raise KeyError(job_id)
    return row


def get_owned(conn, user_id: int, project_id: str, job_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM jobs WHERE id = ? AND project_id = ? AND owner_id = ?",
        (str(job_id), str(project_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise KeyError(job_id)
    return row


def list_for_project(conn, user_id: int, project_id: str, *, session_id: str | None = None, limit: int = 20) -> dict:
    clauses = ["project_id = ?", "owner_id = ?"]
    params: list[Any] = [str(project_id), int(user_id)]
    if session_id:
        clauses.append("session_id = ?")
        params.append(str(session_id))
    rows = conn.execute(
        f"SELECT * FROM jobs WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
        (*params, max(1, min(int(limit), 100))),
    ).fetchall()
    return {"jobs": [public(row) for row in rows]}


def emit(conn, job: sqlite3.Row, type_: str, payload: dict) -> None:
    conn.execute(
        """
        INSERT INTO job_events (job_id, project_id, owner_id, session_id, type, payload, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(job["id"]),
            str(job["project_id"]),
            int(job["owner_id"]),
            job["session_id"],
            str(type_),
            json.dumps(payload, ensure_ascii=False),
            security.now_iso(),
        ),
    )
    conn.commit()


def events_after(
    conn,
    user_id: int,
    project_id: str,
    *,
    after: int = 0,
    session_id: str | None = None,
    limit: int = 200,
) -> dict:
    """会话级事件恢复：浏览器刷新/轮次结束后都能从这里把现场捞回来。"""
    clauses = ["project_id = ?", "owner_id = ?", "seq > ?"]
    params: list[Any] = [str(project_id), int(user_id), int(after or 0)]
    if session_id:
        clauses.append("session_id = ?")
        params.append(str(session_id))
    rows = conn.execute(
        f"SELECT * FROM job_events WHERE {' AND '.join(clauses)} ORDER BY seq LIMIT ?",
        (*params, max(1, min(int(limit), 500))),
    ).fetchall()
    return {
        "events": [
            {
                "seq": int(row["seq"]),
                "job_id": row["job_id"],
                "session_id": row["session_id"],
                "type": row["type"],
                "payload": _loads(row["payload"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ],
        "last_seq": int(rows[-1]["seq"]) if rows else int(after or 0),
    }


def update(conn, job_id: str, **fields: Any) -> None:
    if not fields:
        return
    columns = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(
        f"UPDATE jobs SET {columns} WHERE id = ?",
        (*[fields[key] for key in fields], str(job_id)),
    )
    conn.commit()


def submit(job_id: str, handler: Callable[[sqlite3.Connection, sqlite3.Row], dict]) -> None:
    """把作业丢进后台线程；worker 自己开连接（SQLite 连接不能跨线程）。"""
    if _settings is None:  # pragma: no cover - 配置一定在工厂阶段注入
        raise RuntimeError("jobs.configure() 未调用")
    _executor.submit(_run, str(job_id), handler)


def summary_text(kind: str, result: dict) -> str:
    """用**真实结果**拼一句完成摘要：这是通知，不是模型发言，也不需要再调模型。"""
    if not isinstance(result, dict):
        return ""
    if kind == "ai_review":
        done = int(result.get("done") or 0)
        failed = int(result.get("failed") or 0)
        total = int(result.get("total") or done + failed)
        return f"AI 核查完成：成功 {done}/{total} 条、失败 {failed} 条；结论只作参考，人工复核需单独提交。"
    if kind == "detection_batch":
        done = int(result.get("done") or 0)
        failed = int(result.get("failed") or 0)
        paused = int(result.get("paused") or 0)
        parts = [f"批量检测完成：成功 {done} 张", f"失败 {failed} 张"]
        if paused:
            parts.append(f"因依赖失效暂停 {paused} 张")
        return "、".join(parts) + "。逐条结果见识别记录页。"
    if kind == "inspection":
        record_ids = result.get("record_ids") or []
        reviewed = result.get("review") or {}
        detected = result.get("detection") or {}
        parts = [
            f"目录巡检完成：检测 {int(detected.get('done') or 0)} 张",
            f"失败 {int(detected.get('failed') or 0)} 张",
            f"本轮记录 {len(record_ids)} 条",
        ]
        if isinstance(reviewed, dict):
            parts.append(f"AI 核查 {int(reviewed.get('done') or 0)} 条")
        queue = result.get("queue") or []
        if queue:
            parts.append(f"待人工复核 {len(queue)} 条（已按命中数排序）")
        return "、".join(parts) + "。"
    return ""


def _run(job_id: str, handler) -> None:
    conn = db.connect(_settings.db_path)
    try:
        try:
            job = get(conn, job_id)
        except KeyError:
            return
        if str(job["status"]) in TERMINAL:
            return
        update(conn, job_id, status="running", started_at=security.now_iso())
        job = get(conn, job_id)
        emit(conn, job, "job.started", {"kind": job["kind"]})
        try:
            result = handler(conn, job) or {}
        except Exception as exc:  # noqa: BLE001 - 作业失败必须落库，不能静默
            logging.getLogger(__name__).exception("业务作业失败 job=%s", job_id)
            update(
                conn,
                job_id,
                status="failed",
                error_code=getattr(exc, "code", "JOB_FAILED"),
                error_message=str(getattr(exc, "message", str(exc)))[:500],
                finished_at=security.now_iso(),
            )
            emit(conn, get(conn, job_id), "job.failed", {"message": str(exc)[:300]})
            return
        status = str(result.pop("status", "done"))
        spoken = summary_text(str(job["kind"]), result)
        update(
            conn,
            job_id,
            status=status,
            result=json.dumps(result, ensure_ascii=False),
            finished_at=security.now_iso(),
        )
        emit(conn, get(conn, job_id), f"job.{status}", {**result, "summary": spoken})
        if spoken:
            # 结果通知：由真实结果生成，卡片直接显示；这不需要（也不应该）再开一轮模型。
            emit(conn, get(conn, job_id), "job.summary", {"summary": spoken, "status": status})
    finally:
        conn.close()


def wait(job_id: str, *, timeout: float = 120.0) -> dict:
    """测试/脚本用：等到终态（前端不用这个，它只轮询接口）。"""
    if _settings is None:  # pragma: no cover
        raise RuntimeError("jobs.configure() 未调用")
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = db.connect(_settings.db_path)
        try:
            job = get(conn, job_id)
            if str(job["status"]) in TERMINAL:
                return public(job)
        finally:
            conn.close()
        time.sleep(0.05)
    raise TimeoutError(f"作业未在 {timeout}s 内完成：{job_id}")


__all__ = [
    "TERMINAL",
    "settings",
    "configure",
    "create",
    "emit",
    "events_after",
    "get",
    "get_owned",
    "interrupted",
    "list_for_project",
    "public",
    "submit",
    "summary_text",
    "unfinished_for_turn",
    "update",
    "wait",
]

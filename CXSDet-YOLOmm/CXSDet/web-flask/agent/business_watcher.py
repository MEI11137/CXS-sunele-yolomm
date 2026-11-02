"""业务长任务的完成回调。

业务工具（训练、巡检等）提交后，runtime 会把任务置为 ``awaiting_business`` 并把
等待条件写进 ``waiting_for_json``。在此之前没有任何组件消费这份等待条件，于是
出现"训练已经跑完，会话里却还停在提交那一刻"的情况——用户必须再问一句才会被
告知结果。

本模块用一个后台线程定期核对业务表里的真实状态：进入终态后就在同一会话补一条
assistant 消息（带资源引用）、发事件，并把任务收尾。线程与 HTTP 请求解耦，所以
用户关掉页面也不影响。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger(__name__)

# models.status: 0 训练中 / 1 训练完成 / 2 训练失败 / 3 已发布 / 4 已终止
_MODEL_TERMINAL = {1: "succeeded", 2: "failed", 3: "succeeded", 4: "cancelled"}
_INTERVAL_SECONDS = 5.0
_MAX_PER_SCAN = 20
# 一轮引擎对话正常几十秒结束（引擎侧上限 240 秒）；十分钟还挂在 running 就是孤儿。
_STALE_RUNNING_SECONDS = 600

_started = False
_lock = threading.Lock()
_db_path: Optional[str] = None


def start(db_path: Optional[str] = None) -> bool:
    """启动后台回调线程；重复调用不会重复启动。"""
    global _started, _db_path
    with _lock:
        if _started:
            return False
        _db_path = str(db_path) if db_path else None
        _started = True
    threading.Thread(target=_loop, name="haixun-business-watcher", daemon=True).start()
    return True


def _loop() -> None:
    while True:
        try:
            scan_once()
        except Exception:  # noqa: BLE001 - 后台线程不能因为一次异常退出
            LOGGER.exception("business watcher 扫描失败")
        try:
            _reap_stale_running()
        except Exception:  # noqa: BLE001
            LOGGER.exception("business watcher 清理孤儿任务失败")
        time.sleep(_INTERVAL_SECONDS)


def _reap_stale_running() -> int:
    """把长时间停在 running 的轮次收尾。

    引擎会话被替换、后端重启、或监视线程随进程退出时，任务会永远停在 running，
    界面上就一直是"正在处理"。收尾成 interrupted，让用户知道需要重新提一次。
    """
    from agent import tasks

    cutoff = datetime.now() - timedelta(seconds=_STALE_RUNNING_SECONDS)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, updated_at FROM agent_task WHERE status=?",
            (tasks.STATUS_RUNNING,),
        ).fetchall()
    finally:
        conn.close()
    reaped = 0
    for row in rows:
        raw = str(row["updated_at"] or "")
        try:
            updated = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if updated > cutoff:
            continue
        tasks.set_status(
            row["id"],
            tasks.STATUS_INTERRUPTED,
            summary="本轮没有正常收尾（后端重启或引擎会话被替换），如需继续请重新提出。",
            force=True,
        )
        reaped += 1
    return reaped


def _connect():
    from agent.store import connect

    return connect(_db_path)


def _loads(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _status_code(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _model_rows(model_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    unique = sorted({str(item) for item in model_ids if item})
    if not unique:
        return {}
    marks = ", ".join("?" for _ in unique)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, name, version, status, metrics_json, train_result "
            f"FROM models WHERE id IN ({marks})",
            tuple(unique),
        ).fetchall()
    finally:
        conn.close()
    return {row["id"]: dict(row) for row in rows}


def _format_metrics(*sources: Any) -> str:
    # metrics_json 是扁平字典，train_result 里则藏在 results_dict / metrics 下，两种都收。
    metrics: Dict[str, Any] = {}
    for source in sources:
        payload = _loads(source)
        if not payload:
            continue
        for key in ("results_dict", "metrics"):
            value = payload.get(key)
            if isinstance(value, dict):
                metrics.update(value)
        metrics.update(
            {key: value for key, value in payload.items() if str(key).startswith("metrics/")}
        )
    if not metrics:
        return ""
    wanted = [
        ("mAP50", "metrics/mAP50(B)"),
        ("mAP50-95", "metrics/mAP50-95(B)"),
        ("精确率", "metrics/precision(B)"),
        ("召回率", "metrics/recall(B)"),
    ]
    parts = []
    for label, key in wanted:
        try:
            parts.append(f"{label} {float(metrics[key]):.4f}")
        except (KeyError, TypeError, ValueError):
            continue
    return " · ".join(parts)


def _failure_reason(train_result: Any) -> str:
    payload = _loads(train_result)
    reason = payload.get("error") or payload.get("message") or ""
    text = str(reason).strip().splitlines()
    return text[0][:300] if text else "训练器没有给出具体原因"


def _compose(model_ids: List[str], rows: Dict[str, Dict[str, Any]]) -> Tuple[str, bool]:
    lines: List[str] = []
    failed = False
    for model_id in model_ids:
        row = rows.get(model_id)
        if row is None:
            failed = True
            lines.append(f"训练任务结束了，但模型 {model_id} 已经不存在（可能被删除了）。")
            continue
        name = row.get("name") or model_id
        version = row.get("version")
        status = _status_code(row.get("status"))
        outcome = _MODEL_TERMINAL.get(status)
        if outcome == "succeeded":
            lines.append(f"训练完成：{name}（v{version}，模型 {model_id}）。")
            detail = _format_metrics(row.get("metrics_json"), row.get("train_result"))
            if detail:
                lines.append(detail)
            if status == 1:
                lines.append("现在还是草稿状态，发布之后才会出现在检测页的模型列表里。")
        elif outcome == "failed":
            failed = True
            lines.append(f"训练失败：{name}（v{version}，模型 {model_id}）。原因：{_failure_reason(row.get('train_result'))}")
        elif outcome == "cancelled":
            failed = True
            lines.append(f"训练已停止：{name}（v{version}，模型 {model_id}）。")
        else:  # pragma: no cover - 调用前已保证是终态
            return "", False
    return "\n".join(lines), failed


def scan_once() -> int:
    """扫描一次等待中的业务任务，返回本次处理的任务数。"""
    from agent import sessions, tasks

    handled = 0
    for task in tasks.tasks_in_statuses([tasks.STATUS_AWAITING_BUSINESS]):
        if handled >= _MAX_PER_SCAN:
            break
        waiting = task.get("waiting_for") or task.get("waiting") or {}
        if not isinstance(waiting, dict) or waiting.get("kind") != "business":
            continue
        refs = [ref for ref in (waiting.get("refs") or []) if isinstance(ref, dict)]
        model_ids = [
            str(ref.get("id"))
            for ref in refs
            if str(ref.get("type") or ref.get("kind") or "") == "model" and ref.get("id")
        ]
        if not model_ids:
            continue
        rows = _model_rows(model_ids)
        if any(_status_code((rows.get(model_id) or {}).get("status")) not in _MODEL_TERMINAL for model_id in model_ids):
            continue
        text, failed = _compose(model_ids, rows)
        if not text:
            continue
        sessions.append_message(
            task["session_id"],
            sessions.ROLE_ASSISTANT,
            text,
            task_id=task["id"],
            refs=refs,
        )
        tasks.append_event(
            task["session_id"],
            task["id"],
            tasks.EVENT_ASSISTANT_MESSAGE,
            {"content": text},
        )
        tasks.clear_waiting(task["id"])
        tasks.set_status(
            task["id"],
            tasks.STATUS_FAILED if failed else tasks.STATUS_COMPLETED,
            summary="业务任务已结束",
            force=True,
        )
        handled += 1
    return handled
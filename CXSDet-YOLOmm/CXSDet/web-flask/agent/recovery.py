"""人工接力、长任务跟踪与中断恢复（B-06 / B-07）。

核心原则：

* 只认业务系统的**真实状态**：用户在原页面提交、训练结束，都由程序查询确认，
  不靠聊天里的"我完成了"或关闭页面；
* 刷新页面不取消任务；后端重启后先核对，已生成的结果不重复创建；
* 未确认是否执行的写入/训练显示 ``interrupted``/``unknown``，不自动重做；
* 失败项不反复重试，只读且不计费的查询最多自动重试一次（在 runtime 内实现）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent import actions, connections, memory, operations, permissions
from agent import sessions as session_store
from agent import tasks as task_store
from agent.errors import AgentError, Code
from agent.identity import ActorContext
from agent.tools.registry import ToolRegistry, get_registry

MET_BUSINESS = {"succeeded", "completed", "completed_with_warnings"}
FAILED_BUSINESS = {"failed", "cancelled", "aborted", "partial_failed"}
MET_HUMAN = {"confirmed", "completed", "approved", "done", "submitted"}


def _provider_for(actor: ActorContext, task_id: str, registry: Optional[ToolRegistry]):
    reg = registry or get_registry()
    ops = operations.list_for_task(task_id)
    tool_name = ops[-1]["tool_name"] if ops else "query_project_overview"
    try:
        spec, provider, source = reg.resolve(tool_name, actor)
    except AgentError:
        return None
    return provider


def _query_state(provider: Any, actor: ActorContext, ref: Dict[str, Any]) -> Dict[str, Any]:
    if provider is None:
        return {"status": "unknown", "reason": "业务工具尚未接入，无法核实状态"}
    try:
        state = provider.get_resource_state(actor, ref)
    except AgentError as exc:
        return {"status": "unknown", "reason": exc.message}
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "reason": str(exc)[:160]}
    return state or {"status": "unknown", "reason": "业务未返回状态"}


def _verdict(kind: str, state: Dict[str, Any]) -> str:
    status = str(state.get("status") or "").lower()
    if kind == "human":
        human = str(state.get("human_review_status") or "").lower()
        if human in MET_HUMAN:
            return "met"
        if status in ("unknown", ""):
            return "unknown"
        return "not_met"
    if status in MET_BUSINESS:
        return "met"
    if status in FAILED_BUSINESS:
        return "failed"
    if status in ("unknown", ""):
        return "unknown"
    return "not_met"


def reconcile_task(
    actor: ActorContext,
    task_id: str,
    *,
    registry: Optional[ToolRegistry] = None,
    auto_continue: bool = True,
) -> Dict[str, Any]:
    """核对任务等待的业务状态；满足条件则自动接力继续。"""
    task = task_store.get_task(actor, task_id)
    if task["status"] not in (
        task_store.STATUS_AWAITING_HUMAN,
        task_store.STATUS_AWAITING_BUSINESS,
        task_store.STATUS_RUNNING,
        task_store.STATUS_UNKNOWN,
        task_store.STATUS_INTERRUPTED,
    ):
        return {
            "session_id": task["session_id"],
            "task": task_store.snapshot(task),
            "status": task["status"],
            "reply": "当前任务不在等待或执行中，无需核对。",
            "changed": False,
            "checks": [],
        }

    waiting = task.get("waiting_for") or {}
    kind = waiting.get("kind") or "business"
    refs = waiting.get("refs") or task.get("business_refs") or []
    provider = _provider_for(actor, task_id, registry)

    checks: List[Dict[str, Any]] = []
    if refs:
        for ref in refs:
            state = _query_state(provider, actor, ref)
            checks.append(
                {
                    "ref": ref,
                    "state": state,
                    "verdict": _verdict(kind, state),
                }
            )
    else:
        # 没有具体业务引用时，用最近一次操作的真实状态
        ops = operations.list_for_task(task_id)
        if ops:
            state = {"status": ops[-1]["status"], "operation_id": ops[-1]["id"]}
            checks.append({"ref": {"kind": "operation", "id": ops[-1]["id"]}, "state": state, "verdict": _verdict(kind, state)})
        else:
            checks.append({"ref": None, "state": {"status": "unknown"}, "verdict": "unknown"})

    verdicts = {item["verdict"] for item in checks}
    task_store.append_event(
        task["session_id"], task_id, "task.reconciled", {"checks": checks}
    )

    if "unknown" in verdicts:
        task_store.set_status(
            task_id,
            task_store.STATUS_UNKNOWN,
            summary="无法核实业务状态，请先确认再决定是否重试",
            force=True,
        )
        memory.refresh(task_id)
        return {
            "session_id": task["session_id"],
            "task": task_store.snapshot(task_store.get_task_by_id(task_id)),
            "status": task_store.STATUS_UNKNOWN,
            "reply": "我没有拿到可确认的业务状态，已标记为待核实。请到对应页面确认结果，再告诉我是继续还是重试。",
            "changed": True,
            "checks": checks,
        }

    if "failed" in verdicts:
        task_store.set_status(
            task_id,
            task_store.STATUS_PARTIAL_FAILED,
            summary="等待的业务任务失败，依赖步骤暂停",
            force=True,
        )
        memory.refresh(task_id)
        return {
            "session_id": task["session_id"],
            "task": task_store.snapshot(task_store.get_task_by_id(task_id)),
            "status": task_store.STATUS_PARTIAL_FAILED,
            "reply": "业务侧返回失败，依赖它的后续步骤已暂停；我不会自动重发，请确认原因后决定下一步。",
            "changed": True,
            "checks": checks,
        }

    if verdicts == {"met"}:
        task_store.clear_waiting(task_id)
        task_store.append_event(
            task["session_id"],
            task_id,
            task_store.EVENT_RESUMED,
            {"reason": "业务状态已满足等待条件", "checks": checks},
        )
        task_store.set_status(
            task_id,
            task_store.STATUS_RUNNING,
            summary="等待条件已满足，继续执行",
            force=True,
        )
        if not auto_continue:
            return {
                "session_id": task["session_id"],
                "task": task_store.snapshot(task_store.get_task_by_id(task_id)),
                "status": task_store.STATUS_RUNNING,
                "reply": "等待的业务状态已满足，可以继续。",
                "changed": True,
                "checks": checks,
            }

        # 延迟导入，避免模块循环
        from agent.runtime import AgentRuntime

        session = session_store.get_session(actor, task["session_id"])
        project = permissions.get_project(actor, task["project_id"])
        response = AgentRuntime()._run_loop(
            actor,
            session,
            project,
            task_store.get_task_by_id(task_id),
            resume_reason="业务状态已满足等待条件",
        )
        response["checks"] = checks
        response["changed"] = True
        return response

    memory.refresh(task_id)
    return {
        "session_id": task["session_id"],
        "task": task_store.snapshot(task_store.get_task_by_id(task_id)),
        "status": task_store.get_task_by_id(task_id)["status"],
        "reply": "条件还没有满足，继续等待；你无需回聊天里声明完成，我会按真实状态继续。",
        "changed": False,
        "checks": checks,
    }


def reconcile_startup() -> Dict[str, Any]:
    """进程启动时核对：执行中的任务标为中断，已领取的动作先标未知。"""
    from agent.store import connect, now, query_all

    interrupted = 0
    unknown_actions = 0

    conn = connect()
    try:
        running = query_all(
            conn,
            "SELECT id, session_id FROM agent_task WHERE status = ?",
            (task_store.STATUS_RUNNING,),
        )
        executing = query_all(
            conn,
            "SELECT id, task_id, session_id, tool_name FROM agent_action WHERE status = ?",
            (actions.STATUS_EXECUTING,),
        )
        stamp = now()
        for row in executing:
            conn.execute(
                "UPDATE agent_action SET status = ?, execution_state = 'unknown', "
                "error_code = ?, updated_at = ? WHERE id = ?",
                (actions.STATUS_UNKNOWN, Code.EXECUTION_UNKNOWN, stamp, row["id"]),
            )
            unknown_actions += 1
        for row in running:
            conn.execute(
                "UPDATE agent_task SET status = ?, updated_at = ? WHERE id = ?",
                (task_store.STATUS_INTERRUPTED, stamp, row["id"]),
            )
            interrupted += 1
        conn.commit()
    finally:
        conn.close()

    for row in running:
        task_store.append_event(
            row["session_id"],
            row["id"],
            task_store.EVENT_INTERRUPTED,
            {"reason": "后端重启，未完成步骤标记为中断，需核实后继续"},
        )
    for row in executing:
        task_store.append_event(
            row["session_id"],
            row["task_id"],
            task_store.EVENT_TOOL_ERROR,
            {
                "action_id": row["id"],
                "tool": row["tool_name"],
                "code": Code.EXECUTION_UNKNOWN,
                "message": "后端重启时该动作正在执行，结果未知，需先核对业务状态",
            },
        )

    return {
        "interrupted_tasks": interrupted,
        "unknown_actions": unknown_actions,
    }

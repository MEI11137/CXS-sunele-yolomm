"""Agent 主循环（B-04 ~ B-07）。

一条消息进来后的完整链路：

``接收输入 → 准备材料 → 判断意图 → （复杂任务）提出计划 → 请求工具 →
程序校验权限/参数/确认 → 执行并保存真实结果 → 交回模型 → 继续 / 等待 / 结束``

程序负责"能不能执行、要不要确认"，模型只负责"想做什么、怎么解释结果"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agent import actions, connections, context, memory, operations, permissions
from agent import sessions as session_store
from agent import tasks as task_store
from agent.config import settings
from agent.errors import AgentError, Code, ExecutionState
from agent.identity import ActorContext, ServerContext
from agent.manuals import get_manual, match_manual
from agent.store import dumps
from agent.tools import business, grants
from agent.tools.contract import (
    RISK_CRITICAL,
    RISK_WRITE,
    OperationResult,
    ToolSpec,
    validate_arguments,
)
from agent.tools.registry import (
    ASK_USER,
    PROPOSE_PLAN,
    ToolRegistry,
    get_registry,
)

# 意图分类（规则优先：执行中收到消息要区分询问/变更/停止）
INTENT_NEW = "new"
INTENT_CONTINUE = "continue"
INTENT_PROGRESS = "progress"
INTENT_CHANGE = "change"
INTENT_STOP = "stop"

_STOP_TOKENS = ("停止", "别做了", "取消任务", "不要继续", "停下", "中止", "stop now")
_PROGRESS_TOKENS = ("做到哪", "进度", "怎么样了", "到哪一步", "还要多久", "现在状态", "完成了没")
_CHANGE_TOKENS = ("换成", "改成", "改为", "先不要", "不要用", "换一个", "调整为", "重新来")

_TRANSIENT_REASONS = {"network_or_timeout", "rate_limited"}

# 需要按资源权限核对的参数名
_RESOURCE_ARG_KEYS = {
    "image_refs",
    "record_ids",
    "dataset_id",
    "model_id",
    "parent_model_id",
    "resource_id",
    "candidate_ref",
    "project_id",
}


def classify_intent(text: str, has_active_task: bool) -> str:
    body = (text or "").strip().lower()
    if any(token in body for token in _STOP_TOKENS):
        return INTENT_STOP
    if not has_active_task:
        return INTENT_NEW
    if any(token in body for token in _PROGRESS_TOKENS):
        return INTENT_PROGRESS
    if any(token in body for token in _CHANGE_TOKENS):
        return INTENT_CHANGE
    return INTENT_CONTINUE


@dataclass
class LoopState:
    """一次 handle_message 调用内的可变状态。"""

    started: float = field(default_factory=time.monotonic)
    executed_tool_names: List[str] = field(default_factory=list)
    budget_exceeded: bool = False


class AgentRuntime:
    """单 Agent 运行时。``gateway_factory`` 便于测试注入假模型。"""

    def __init__(
        self,
        gateway_factory: Optional[Callable[[ActorContext], Any]] = None,
        registry: Optional[ToolRegistry] = None,
    ) -> None:
        self._gateway_factory = gateway_factory or connections.model_client
        self._registry = registry

    @property
    def registry(self) -> ToolRegistry:
        return self._registry or get_registry()

    def _resolve_refs(
        self, actor: ActorContext, refs: Optional[List[Any]]
    ) -> List[Dict[str, Any]]:
        """把用户点选的资源解析成受控资源，逐个核对读权限。

        解析不了或无权访问的直接跳过（不把别人的资源塞进上下文），
        并让模型看到"缺少可用对象"，而不是凭空猜测。
        """
        resolved: List[Dict[str, Any]] = []
        for ref in refs or []:
            if isinstance(ref, dict):
                target: Any = ref.get("id") or ref.get("resource_id") or ref.get("ref")
            else:
                target = ref
            if not target:
                continue
            try:
                resource = permissions.resolve_resource(actor, target)
            except AgentError:
                continue
            try:
                permissions.require_access(actor, resource, "read")
            except AgentError:
                continue
            resolved.append(permissions.decorate_with_actions(actor, resource))
        return resolved

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #
    def handle_message(
        self,
        actor: ActorContext,
        session_id: str,
        text: str,
        *,
        resource_refs: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        session = session_store.get_session(actor, session_id)
        project = permissions.get_project(actor, session["project_id"])
        body = (text or "").strip()
        if not body:
            raise AgentError(Code.VALIDATION, "消息内容不能为空")

        resources = self._resolve_refs(actor, resource_refs)
        sequence_before = task_store.latest_sequence(session_id)
        session_store.append_message(
            session_id,
            session_store.ROLE_USER,
            body,
            refs=resource_refs or [],
        )
        task_store.append_event(
            session_id, None, task_store.EVENT_MESSAGE, {"role": "user", "text": body}
        )

        active = task_store.find_active_task(actor, session_id)
        intent = classify_intent(body, active is not None)

        if active and intent == INTENT_STOP:
            response = self.stop_task(actor, active["id"], reason="用户要求停止")
            response["events_after"] = sequence_before
            return response

        if active and intent == INTENT_PROGRESS:
            response = self._progress_reply(active)
            response["events_after"] = sequence_before
            return response

        if active and intent == INTENT_CHANGE:
            self._apply_change(active)
            task = task_store.get_task_by_id(active["id"])
        elif active:
            pending = actions.list_actions(active["id"], [actions.STATUS_PENDING])
            if pending:
                # 还有待确认动作时不重复推进，避免绕过确认
                return {
                    "session_id": session_id,
                    "task": task_store.snapshot(active),
                    "status": active["status"],
                    "reply": "还有一项操作等待你确认，请先处理确认卡片。",
                    "action": pending[0],
                    "plan": active.get("plan"),
                    "events_after": sequence_before,
                }
            task = active
        else:
            matched, score = match_manual(
                actor, body, requested_id=session.get("manual_id") or None
            )
            task = task_store.create_task(
                actor,
                session_id,
                project["id"],
                body,
                manual_id=matched["id"] if matched and score > 0 else None,
                manual_version=matched["version"] if matched and score > 0 else None,
            )

        response = self._run_loop(actor, session, project, task, resources=resources)
        response["events_after"] = sequence_before
        return response

    def confirm_action(
        self,
        actor: ActorContext,
        action_id: str,
        *,
        expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """用户确认一个动作：原子领取执行权，重复点击复用原结果。"""
        sequence_before = task_store.latest_sequence(
            actions.get_action(actor, action_id)["session_id"]
        )
        claimed_info = actions.confirm(actor, action_id, expected_revision=expected_revision)
        action = claimed_info["action"]
        task = task_store.get_task(actor, action["task_id"])
        session = session_store.get_session(actor, task["session_id"])
        project = permissions.get_project(actor, task["project_id"])

        if not claimed_info["claimed"]:
            # 已领取/已完成：不重复执行，直接返回当前真实状态
            return {
                "session_id": session["id"],
                "task": task_store.snapshot(task),
                "status": task["status"],
                "action": action,
                "reply": "该操作已处理过，这里显示的是当时的执行结果。",
                "plan": task.get("plan"),
                "events_after": sequence_before,
            }

        spec, provider, source = self.registry.resolve(action["tool_name"], actor)
        if str(spec.version) != str(action["tool_version"]):
            actions.supersede_pending(task["id"], "工具版本已变化")
            raise AgentError(
                Code.ACTION_STALE,
                "该动作对应的工具版本已经变化，请重新发起以使用最新版本",
                http_status=409,
            )

        try:
            outcome = self._execute_tool(
                actor,
                project,
                task,
                spec,
                provider,
                source,
                action["parameters"],
                action_id=action["id"],
                request_key=action["request_key"],
            )
        except AgentError as exc:
            actions.mark_result(
                action["id"],
                status=actions.STATUS_FAILED,
                result={"error": exc.to_dict()},
                error_code=exc.code,
                execution_state=exc.execution_state,
            )
            raise

        if outcome["long_running"]:
            return {
                "session_id": session["id"],
                "task": task_store.snapshot(task_store.get_task_by_id(task["id"])),
                "status": task_store.STATUS_AWAITING_BUSINESS,
                "action": actions.get_action(actor, action["id"]),
                "reply": outcome["reply"],
                "plan": task.get("plan"),
                "events_after": sequence_before,
            }

        response = self._run_loop(
            actor, session, project, task_store.get_task_by_id(task["id"])
        )
        response["action"] = actions.get_action(actor, action["id"])
        response["events_after"] = sequence_before
        return response

    def confirm_plan(self, actor: ActorContext, task_id: str) -> Dict[str, Any]:
        task = task_store.get_task(actor, task_id)
        session = session_store.get_session(actor, task["session_id"])
        project = permissions.get_project(actor, task["project_id"])
        sequence_before = task_store.latest_sequence(session["id"])
        task = task_store.confirm_plan(task_id, actor.user_id)
        response = self._run_loop(actor, session, project, task)
        response["events_after"] = sequence_before
        return response

    def reject_action(
        self, actor: ActorContext, action_id: str, reason: str = ""
    ) -> Dict[str, Any]:
        action = actions.reject(actor, action_id, reason)
        task = task_store.get_task(actor, action["task_id"])
        session_store.append_message(
            task["session_id"],
            session_store.ROLE_SYSTEM_EVENT,
            f"用户拒绝执行 {action['tool_name']}：{reason or '未说明原因'}",
            task_id=task["id"],
        )
        memory.refresh(task["id"])
        return {
            "session_id": task["session_id"],
            "task": task_store.snapshot(task_store.get_task_by_id(task["id"])),
            "status": task["status"],
            "action": action,
            "reply": "已拒绝该操作，未执行任何写入。你可以调整要求后让我重新规划。",
        }

    def stop_task(
        self, actor: ActorContext, task_id: str, reason: str = "用户要求停止"
    ) -> Dict[str, Any]:
        task = task_store.get_task(actor, task_id)
        superseded = actions.supersede_pending(task_id, reason)
        cancel_notes: List[str] = []
        for pending_action in actions.list_executing(task_id):
            spec, provider, source = self.registry.resolve(pending_action["tool_name"], actor)
            if provider is None or not spec.supports_cancel:
                cancel_notes.append(
                    f"{spec.title} 已经开始执行，该工具不支持撤销，需要你核对其真实状态"
                )
                continue
            try:
                result = provider.cancel_operation(actor, pending_action["id"])
            except AgentError as exc:
                cancel_notes.append(f"{spec.title} 撤销失败：{exc.message}")
                continue
            if result.ok:
                cancel_notes.append(f"{spec.title} 已请求撤销")
            else:
                message = (result.error or {}).get("message") or "撤销未成功"
                cancel_notes.append(f"{spec.title} {message}")

        task_store.append_event(
            task["session_id"],
            task_id,
            task_store.EVENT_STOPPED,
            {"reason": reason, "superseded_actions": superseded, "cancel": cancel_notes},
        )
        task_store.set_status(task_id, task_store.STATUS_STOPPED, summary=reason, force=True)
        session_store.append_message(
            task["session_id"],
            session_store.ROLE_SYSTEM_EVENT,
            reason,
            task_id=task_id,
        )
        memory.refresh(task_id)
        reply = "已停止后续步骤。"
        if superseded:
            reply += f"（作废 {superseded} 项待确认操作）"
        if cancel_notes:
            reply += " " + "；".join(cancel_notes)
        return {
            "session_id": task["session_id"],
            "task": task_store.snapshot(task_store.get_task_by_id(task_id)),
            "status": task_store.STATUS_STOPPED,
            "reply": reply,
        }

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def _run_loop(
        self,
        actor: ActorContext,
        session: Dict[str, Any],
        project: Dict[str, Any],
        task: Dict[str, Any],
        *,
        resources: Optional[List[Dict[str, Any]]] = None,
        resume_reason: str = "",
    ) -> Dict[str, Any]:
        cfg = settings()
        state = LoopState()
        gateway = self._gateway_factory(actor)
        resources = resources or []
        note = memory.refresh(task["id"])
        manual = self._task_manual(actor, task)

        if resume_reason:
            task_store.append_event(
                session["id"], task["id"], task_store.EVENT_RESUMED, {"reason": resume_reason}
            )

        for _ in range(max(1, cfg.max_steps)):
            if time.monotonic() - state.started > cfg.time_budget_seconds:
                state.budget_exceeded = True
                task_store.append_event(
                    session["id"],
                    task["id"],
                    task_store.EVENT_BUDGET_EXCEEDED,
                    {"seconds": cfg.time_budget_seconds},
                )
                return self._finish_with_summary(
                    actor,
                    session,
                    task,
                    "本轮已到达执行预算上限，尚未完成的步骤已保留，可继续让我接着做。",
                    status=task_store.STATUS_RUNNING,
                )

            messages = context.build_messages(
                session,
                task,
                project,
                manual=manual,
                note=note,
                resources=resources,
            )
            tools = self.registry.openai_tools(actor, project["id"])
            turn = gateway.complete(messages, tools)

            if not turn.tool_calls:
                reply = turn.text or "（模型没有返回内容）"
                session_store.append_message(
                    session["id"],
                    session_store.ROLE_ASSISTANT,
                    reply,
                    task_id=task["id"],
                )
                task_store.append_event(
                    session["id"],
                    task["id"],
                    task_store.EVENT_ASSISTANT_MESSAGE,
                    {"text": reply},
                )
                status = self._decide_final_status(task)
                task_store.set_status(task["id"], status, summary=reply[:200])
                memory.refresh(task["id"])
                return {
                    "session_id": session["id"],
                    "task": task_store.snapshot(task_store.get_task_by_id(task["id"])),
                    "status": status,
                    "reply": reply,
                    "action": None,
                    "plan": task.get("plan"),
                }

            for call in turn.tool_calls:
                outcome = self._handle_tool_call(
                    actor, session, project, task, call, state, note
                )
                if outcome is not None:
                    return outcome
                task = task_store.get_task_by_id(task["id"])

        # 达到循环上限
        task_store.append_event(
            session["id"],
            task["id"],
            task_store.EVENT_BUDGET_EXCEEDED,
            {"max_steps": cfg.max_steps},
        )
        return self._finish_with_summary(
            actor,
            session,
            task,
            f"已达到本轮工具调用上限（{cfg.max_steps} 次），未完成的事项已记录，可以让我继续。",
            status=task_store.STATUS_RUNNING,
        )

    def _handle_tool_call(
        self,
        actor: ActorContext,
        session: Dict[str, Any],
        project: Dict[str, Any],
        task: Dict[str, Any],
        call: Any,
        state: LoopState,
        note: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """返回非 None 表示本轮结束（等待确认/计划/人工/追问）。"""
        spec, provider, source = self.registry.resolve(call.name, actor)

        if spec.name == PROPOSE_PLAN:
            arguments = validate_arguments(spec, call.arguments)
            task_store.set_plan(
                task["id"],
                goal=arguments.get("goal"),
                steps=arguments.get("steps") or [],
                expected_outputs=arguments.get("expected_outputs"),
                open_questions=arguments.get("open_questions"),
                manual_id=task.get("manual_id"),
                manual_version=task.get("manual_version"),
            )
            task = task_store.get_task_by_id(task["id"])
            return {
                "session_id": session["id"],
                "task": task_store.snapshot(task),
                "status": task_store.STATUS_AWAITING_PLAN,
                "reply": "我已经整理出整体计划，请确认方向后再继续。",
                "action": None,
                "plan": task.get("plan"),
            }

        if spec.name == ASK_USER:
            arguments = validate_arguments(spec, call.arguments)
            question = str(arguments.get("question") or "").strip()
            if not question:
                raise AgentError(Code.TOOL_ARGS_INVALID, "追问内容不能为空")
            options = arguments.get("options") or []
            text = question
            if options:
                text += "\n可选项：" + "；".join(str(item) for item in options)
            session_store.append_message(
                session["id"], session_store.ROLE_ASSISTANT, text, task_id=task["id"]
            )
            task_store.append_event(
                session["id"],
                task["id"],
                task_store.EVENT_QUESTION,
                {"question": question, "options": options},
            )
            task_store.set_waiting(
                task["id"], kind="input", reason=question, page_hint=None
            )
            task_store.set_status(
                task["id"], task_store.STATUS_AWAITING_INPUT, summary="等待用户补充信息"
            )
            memory.refresh(task["id"])
            return {
                "session_id": session["id"],
                "task": task_store.snapshot(task_store.get_task_by_id(task["id"])),
                "status": task_store.STATUS_AWAITING_INPUT,
                "reply": text,
                "action": None,
                "plan": task.get("plan"),
            }

        arguments = validate_arguments(spec, call.arguments)
        task_store.append_event(
            session["id"],
            task["id"],
            task_store.EVENT_TOOL_REQUESTED,
            {"tool": spec.name, "version": spec.version, "arguments": arguments},
        )

        # 1) 资源权限：能用资源索引核对的先核对
        revisions, unverified = self._guard_arguments(actor, arguments)

        # 2) 计划门禁：多步写入必须先确认整体计划
        gate = self._plan_gate(task, spec)
        if gate is not None:
            kind, payload = gate
            if kind == "stop":
                return payload
            # 把门禁要求作为"工具错误"交回模型，让它在下一轮提出计划
            self._feedback_tool_error(session, task, spec, payload)
            return None

        # 3) 确认策略
        need_confirm = grants.needs_confirmation(
            actor, spec, project_id=project["id"], source=source
        )
        if need_confirm:
            preview: Dict[str, Any] = {}
            if spec.supports_preview and provider is not None:
                try:
                    prepared = provider.prepare(actor, spec, arguments, None)
                    preview = prepared.get("preview", prepared) if prepared else {}
                except AgentError as exc:
                    preview = {"preview_error": exc.message}
            if unverified:
                preview.setdefault("unverified_refs", unverified)
            action = actions.create_action(
                actor,
                task_id=task["id"],
                tool_name=spec.name,
                tool_version=spec.version,
                tool_title=spec.title,
                parameters=arguments,
                preview=preview,
                target_projects=[project["id"]],
                resource_revisions=revisions,
            )
            task = task_store.get_task_by_id(task["id"])
            return {
                "session_id": session["id"],
                "task": task_store.snapshot(task),
                "status": task_store.STATUS_AWAITING_ACTION,
                "reply": f"准备执行「{spec.title}」，请确认卡片中的目标、参数与影响范围。",
                "action": action,
                "plan": task.get("plan"),
            }

        # 4) 直接执行（只读或已放行的具体只读工具）
        outcome = self._execute_tool(
            actor, project, task, spec, provider, source, arguments
        )
        state.executed_tool_names.append(spec.name)

        if outcome["long_running"]:
            task = task_store.get_task_by_id(task["id"])
            return {
                "session_id": session["id"],
                "task": task_store.snapshot(task),
                "status": task_store.STATUS_AWAITING_BUSINESS,
                "reply": outcome["reply"],
                "action": None,
                "plan": task.get("plan"),
            }
        return None

    # ------------------------------------------------------------------ #
    # 执行与结果保存
    # ------------------------------------------------------------------ #
    def _execute_tool(
        self,
        actor: ActorContext,
        project: Dict[str, Any],
        task: Dict[str, Any],
        spec: ToolSpec,
        provider: Any,
        source: str,
        arguments: Dict[str, Any],
        *,
        action_id: Optional[str] = None,
        request_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        if provider is None:
            raise AgentError(
                Code.TOOL_BACKEND_UNAVAILABLE,
                f"工具 {spec.name} 尚未接入，无法执行",
                http_status=409,
            )
        revisions, _ = self._guard_arguments(actor, arguments)
        key = request_key or actions.build_request_key(
            task["id"], spec.name, spec.version, arguments, revisions
        )
        tool_context = ServerContext(
            actor=actor,
            request_id=actor.request_id,
            project_id=project["id"],
            task_id=task["id"],
            authorization={
                "confirmed": bool(action_id),
                "source": source,
                "tool_version": spec.version,
                "stub": spec.source == "stub",
            },
        ).as_tool_context()

        attempt = 0
        cfg = settings()
        while True:
            try:
                result = provider.execute(
                    actor,
                    spec,
                    arguments,
                    request_key=key if attempt == 0 else f"{key}_{attempt}",
                    context=tool_context,
                )
                break
            except AgentError as exc:
                if self._should_retry(spec, exc, attempt, cfg.max_readonly_retries):
                    attempt += 1
                    continue
                raise
            except Exception as exc:  # noqa: BLE001 - 未预期异常也要落库
                transient = self._is_transient(exc)
                if spec.readonly and not spec.billable and attempt < cfg.max_readonly_retries and transient:
                    attempt += 1
                    continue
                if spec.risk in (RISK_WRITE, RISK_CRITICAL) or spec.billable:
                    message = (
                        "请求没有拿到明确结果，可能已经执行也可能没有；"
                        "请先核对真实业务状态，我不会自动重发。"
                    )
                    operation = operations.mark_unknown(
                        actor,
                        task_id=task["id"],
                        action_id=action_id,
                        tool_name=spec.name,
                        request_key=key,
                        message=message,
                    )
                    if action_id:
                        actions.mark_unknown(action_id, message)
                    session_store.append_message(
                        task["session_id"],
                        session_store.ROLE_TOOL,
                        dumps({"status": "unknown", "operation": operation["id"]}),
                        task_id=task["id"],
                        tool_name=spec.name,
                    )
                    task_store.append_event(
                        task["session_id"],
                        task["id"],
                        task_store.EVENT_TOOL_ERROR,
                        {
                            "tool": spec.name,
                            "code": Code.EXECUTION_UNKNOWN,
                            "message": message,
                            "execution_state": ExecutionState.UNKNOWN,
                        },
                    )
                    task_store.set_waiting(
                        task["id"],
                        kind="business",
                        reason=message,
                        refs=[{"kind": "operation", "id": operation["id"]}],
                    )
                    task_store.set_status(
                        task["id"],
                        task_store.STATUS_AWAITING_BUSINESS,
                        summary="执行结果未知，等待用户决定",
                    )
                    return {
                        "long_running": True,
                        "reply": message,
                        "result": None,
                    }
                raise AgentError(
                    Code.TOOL_BACKEND_UNAVAILABLE,
                    f"工具 {spec.name} 执行异常：{str(exc)[:160]}",
                    http_status=502,
                    execution_state=ExecutionState.UNKNOWN,
                ) from exc

        operation = operations.record(
            actor,
            task_id=task["id"],
            action_id=action_id,
            tool_name=spec.name,
            request_key=key,
            result=result,
            attempts=attempt + 1,
        )
        for ref in result.business_refs or []:
            task_store.add_business_ref(task["id"], ref)

        payload = {
            "status": result.status,
            "outputs": result.outputs,
            "business_refs": result.business_refs,
            "item_results": result.item_results,
            "is_stub": result.is_stub,
            "operation_id": operation["id"],
        }
        session_store.append_message(
            task["session_id"],
            session_store.ROLE_TOOL,
            dumps(payload),
            task_id=task["id"],
            tool_name=spec.name,
        )
        task_store.append_event(
            task["session_id"],
            task["id"],
            task_store.EVENT_TOOL_RESULT,
            {"tool": spec.name, "version": spec.version, **payload},
        )

        if action_id:
            actions.mark_result(
                action_id,
                status=self._action_status(result.status),
                result=payload,
                error_code=(result.error or {}).get("code") if result.error else None,
                execution_state=result.execution_state,
                operation_id=operation["id"],
            )
            task_store.append_event(
                task["session_id"],
                task["id"],
                task_store.EVENT_ACTION_EXECUTED,
                {"action_id": action_id, "status": result.status},
            )

        memory.refresh(task["id"])

        if result.status == "running":
            reference = (result.business_refs or [{}])[0]
            task_store.set_waiting(
                task["id"],
                kind="business",
                reason="业务任务已提交，等待其真实状态变化",
                refs=result.business_refs,
                conditions=[{"type": "business_status", "target": "terminal"}],
            )
            task_store.set_status(
                task["id"],
                task_store.STATUS_AWAITING_BUSINESS,
                summary="长任务已提交，等待业务结果",
            )
            return {
                "long_running": True,
                "reply": (
                    f"已提交，业务任务 {reference.get('id')} 正在运行。"
                    "后续我会按真实状态查询，不会重复提交。"
                ),
                "result": payload,
            }

        if result.status == "partial_failed":
            task_store.set_status(
                task["id"],
                task_store.STATUS_RUNNING,
                summary="部分事项失败，独立事项继续",
                force=False,
            )

        return {"long_running": False, "reply": "", "result": payload}

    def _should_retry(
        self, spec: ToolSpec, exc: AgentError, attempt: int, limit: int
    ) -> bool:
        if spec.risk != "read" or spec.billable:
            return False
        if attempt >= limit:
            return False
        reason = (exc.details or {}).get("reason", "")
        return reason in _TRANSIENT_REASONS or exc.execution_state == ExecutionState.UNKNOWN

    @staticmethod
    def _is_transient(exc: BaseException) -> bool:
        return isinstance(exc, (TimeoutError, ConnectionError, OSError)) or "timeout" in str(exc).lower()

    @staticmethod
    def _action_status(result_status: str) -> str:
        if result_status in ("succeeded", "running"):
            return actions.STATUS_SUCCEEDED
        if result_status == "unknown":
            return actions.STATUS_UNKNOWN
        return actions.STATUS_FAILED

    # ------------------------------------------------------------------ #
    # 门禁与辅助
    # ------------------------------------------------------------------ #
    def _guard_arguments(
        self, actor: ActorContext, arguments: Dict[str, Any]
    ) -> tuple[Dict[str, Any], List[str]]:
        """能通过资源索引核对的目标先核对；核对不了的如实标为"未核对"。"""
        revisions: Dict[str, Any] = {}
        unverified: List[str] = []
        for key in _RESOURCE_ARG_KEYS & set(arguments.keys()):
            value = arguments.get(key)
            if value in (None, ""):
                continue
            items = value if isinstance(value, list) else [value]
            for item in items:
                if item in (None, ""):
                    continue
                try:
                    resource = permissions.resolve_resource(actor, item)
                except AgentError as exc:
                    if exc.code == Code.RESOURCE_NOT_FOUND:
                        unverified.append(str(item))
                        continue
                    raise
                revisions[resource["id"]] = resource["revision"]
                if key == "project_id" and resource.get("kind") == "project":
                    continue
                permissions.require_access(actor, resource, "read")
        return revisions, unverified

    def _plan_gate(
        self, task: Dict[str, Any], spec: ToolSpec
    ) -> Optional[tuple[str, Any]]:
        """写入类动作的门禁。

        返回 ``("stop", response)`` 表示本轮结束等用户确认计划；
        返回 ``("feedback", message)`` 表示把要求交回模型，让它先提计划。
        """
        if spec.risk not in (RISK_WRITE, RISK_CRITICAL) and not spec.billable:
            return None
        if task.get("plan_version"):
            if not task.get("plan_confirmed_at"):
                return (
                    "stop",
                    {
                        "session_id": task["session_id"],
                        "task": task_store.snapshot(task),
                        "status": task_store.STATUS_AWAITING_PLAN,
                        "reply": "整体计划还需要你确认后才能执行写入类动作。",
                        "action": None,
                        "plan": task.get("plan"),
                    },
                )
            return None
        # 已经有过一次写入动作（确认或已执行）但从未提出计划 → 必须先规划
        prior = len(actions.list_actions(task["id"])) + len(
            operations.list_for_task(task["id"])
        )
        if prior >= 1:
            return (
                "feedback",
                "该任务已经包含多项写入动作，请先调用 propose_plan 提出整体计划，"
                "等用户确认方向后再逐个请求执行。",
            )
        return None

    def _feedback_tool_error(
        self,
        session: Dict[str, Any],
        task: Dict[str, Any],
        spec: ToolSpec,
        message: str,
    ) -> None:
        """把程序侧的拒绝理由作为工具错误写回对话，模型据此调整。"""
        payload = {
            "status": "failed",
            "error": {"code": Code.PLAN_REQUIRED_FOR_MULTI_WRITE, "message": message},
            "execution_state": ExecutionState.NOT_EXECUTED,
        }
        session_store.append_message(
            task["session_id"],
            session_store.ROLE_TOOL,
            dumps(payload),
            task_id=task["id"],
            tool_name=spec.name,
        )
        task_store.append_event(
            task["session_id"],
            task["id"],
            task_store.EVENT_TOOL_ERROR,
            {
                "tool": spec.name,
                "code": Code.PLAN_REQUIRED_FOR_MULTI_WRITE,
                "message": message,
                "execution_state": ExecutionState.NOT_EXECUTED,
            },
        )

    def _task_manual(self, actor: ActorContext, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        manual_id = task.get("manual_id")
        if not manual_id:
            return None
        try:
            return get_manual(actor, manual_id)
        except AgentError:
            return None

    def _decide_final_status(self, task: Dict[str, Any]) -> str:
        """只在有真实依据时才标记完成；未完成的步骤如实保留。"""
        current = task["status"]
        steps = task.get("steps") or []
        # 目标刚被改动、还没重新规划：既没执行也没计划，不能算完成
        if (
            current == task_store.STATUS_PLANNING
            and not steps
            and (task.get("summary") or "") == task_store.SUMMARY_REPLAN
        ):
            return task_store.STATUS_PLANNING
        if any(step.get("status") == "failed" for step in steps):
            return task_store.STATUS_PARTIAL_FAILED
        if steps and all(step.get("status") in ("done", "skipped") for step in steps):
            return task_store.STATUS_COMPLETED
        if steps:
            # 有计划但还有未完成步骤：不能算完成
            if current in task_store.WAITING_STATUSES:
                return current
            return task_store.STATUS_PARTIAL_FAILED
        if current in task_store.WAITING_STATUSES:
            return current
        return task_store.STATUS_COMPLETED

    def _finish_with_summary(
        self,
        actor: ActorContext,
        session: Dict[str, Any],
        task: Dict[str, Any],
        reply: str,
        *,
        status: str,
    ) -> Dict[str, Any]:
        session_store.append_message(
            session["id"], session_store.ROLE_ASSISTANT, reply, task_id=task["id"]
        )
        task_store.set_status(task["id"], status, summary=reply[:200], force=True)
        memory.refresh(task["id"])
        return {
            "session_id": session["id"],
            "task": task_store.snapshot(task_store.get_task_by_id(task["id"])),
            "status": status,
            "reply": reply,
            "action": None,
            "plan": task.get("plan"),
        }

    def _progress_reply(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """问进度不打断任务：按真实记录回答，不改任务状态。"""
        steps = task.get("steps") or []
        done = [s for s in steps if s.get("status") in ("done", "skipped")]
        pending = [s for s in steps if s.get("status") not in ("done", "skipped")]
        waiting = task.get("waiting_for") or {}
        label = task_store.STATUS_LABELS.get(task["status"], "处理中")
        lines = [f"当前任务状态：{label}"]
        if task.get("plan_version"):
            lines.append(f"计划版本：v{task['plan_version']}，{'已确认' if task.get('plan_confirmed_at') else '待确认'}")
        if done:
            lines.append("已完成：" + "；".join(s["title"] for s in done))
        if pending:
            lines.append("待完成：" + "；".join(s["title"] for s in pending))
        if waiting.get("reason"):
            lines.append(f"正在等待：{waiting['reason']}")
        # 有待确认动作时要说出来，否则用户不知道卡在哪里
        pending_actions = actions.list_actions(task["id"], [actions.STATUS_PENDING])
        pending_action = pending_actions[0] if pending_actions else None
        if pending_action is not None:
            lines.append(f"有一项操作等待你确认：{pending_action['tool_title']}")
        refs = task.get("business_refs") or []
        if refs:
            lines.append("业务引用：" + "；".join(f"{r.get('kind')}:{r.get('id')}" for r in refs))
        lines.append("（任务没有被中断，可以继续推进。）")
        reply = "\n".join(lines)
        task_store.append_event(
            task["session_id"],
            task["id"],
            task_store.EVENT_PROGRESS,
            {"reply": reply},
        )
        session_store.append_message(
            task["session_id"],
            session_store.ROLE_ASSISTANT,
            reply,
            task_id=task["id"],
        )
        return {
            "session_id": task["session_id"],
            "task": task_store.snapshot(task),
            "status": task["status"],
            "reply": reply,
            "action": pending_action,
            "plan": task.get("plan"),
            "intent": INTENT_PROGRESS,
        }

    def _apply_change(self, task: Dict[str, Any]) -> None:
        """目标变更：暂停未开始步骤、作废旧确认、要求重新规划。"""
        superseded = actions.supersede_pending(task["id"], "用户改变了目标或参数")
        steps = task.get("steps") or []
        for step in steps:
            if step.get("status") in ("pending",):
                step["status"] = "paused"
        task_store.update_steps(task["id"], steps)
        task_store.append_event(
            task["session_id"],
            task["id"],
            task_store.EVENT_STATUS_CHANGED,
            {"to": task_store.STATUS_PLANNING, "reason": "目标变更", "superseded": superseded},
        )
        task_store.set_status(
            task["id"],
            task_store.STATUS_PLANNING,
            summary=task_store.SUMMARY_REPLAN,
            emit=False,
            force=True,
        )


def get_runtime() -> AgentRuntime:
    """默认运行时（真实模型客户端 + 已注册工具）。"""
    return AgentRuntime()


def set_business_backend(backend: Any) -> None:
    """第三人接入真实业务工具的入口。"""
    business.set_backend(backend)

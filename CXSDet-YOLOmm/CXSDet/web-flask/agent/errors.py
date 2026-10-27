"""稳定错误码与统一异常。

前端按 ``code`` 分支，不按 ``message`` 文本匹配；``message`` 为中文说明。
``execution_state`` 说明失败时动作是否已经执行，避免前端把"未知"当成"未执行"。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class Code:
    """稳定错误码。新增只能追加，不得改名。"""

    UNAUTHENTICATED = "AGENT_UNAUTHENTICATED"
    FORBIDDEN = "AGENT_FORBIDDEN"
    NOT_FOUND = "AGENT_NOT_FOUND"
    VALIDATION = "AGENT_VALIDATION_FAILED"

    PROJECT_NOT_FOUND = "AGENT_PROJECT_NOT_FOUND"
    PROJECT_EXISTS = "AGENT_PROJECT_EXISTS"
    RESOURCE_NOT_FOUND = "AGENT_RESOURCE_NOT_FOUND"
    RESOURCE_FORBIDDEN = "AGENT_RESOURCE_FORBIDDEN"
    RESOURCE_REVISION_MISMATCH = "AGENT_RESOURCE_REVISION_MISMATCH"

    MODEL_NOT_CONFIGURED = "AGENT_MODEL_NOT_CONFIGURED"
    MODEL_CAPABILITY_MISSING = "AGENT_MODEL_CAPABILITY_MISSING"
    MODEL_CALL_FAILED = "AGENT_MODEL_CALL_FAILED"
    CONNECTION_CHECK_FAILED = "AGENT_CONNECTION_CHECK_FAILED"
    SECRET_STORE_FAILED = "AGENT_SECRET_STORE_FAILED"

    TOOL_UNKNOWN = "AGENT_TOOL_UNKNOWN"
    TOOL_UNAVAILABLE = "AGENT_TOOL_UNAVAILABLE"
    TOOL_ARGS_INVALID = "AGENT_TOOL_ARGS_INVALID"
    TOOL_BACKEND_UNAVAILABLE = "AGENT_TOOL_BACKEND_UNAVAILABLE"
    TOOL_NOT_GRANTED = "AGENT_TOOL_NOT_GRANTED"
    STUB_NOT_ALLOWED = "AGENT_STUB_NOT_ALLOWED"

    ACTION_NOT_FOUND = "AGENT_ACTION_NOT_FOUND"
    ACTION_STALE = "AGENT_ACTION_STALE"
    ACTION_ALREADY_DECIDED = "AGENT_ACTION_ALREADY_DECIDED"
    ACTION_REQUEST_KEY_CONFLICT = "AGENT_ACTION_REQUEST_KEY_CONFLICT"
    ACTION_TARGET_CHANGED = "AGENT_ACTION_TARGET_CHANGED"

    # 业务预检未通过：不得生成"确认后即可执行"的动作
    PREPARE_BLOCKED = "AGENT_PREPARE_BLOCKED"
    PREPARE_FAILED = "AGENT_PREPARE_FAILED"

    PLAN_NOT_CONFIRMED = "AGENT_PLAN_NOT_CONFIRMED"
    PLAN_REQUIRED_FOR_MULTI_WRITE = "AGENT_PLAN_REQUIRED_FOR_MULTI_WRITE"

    TASK_NOT_FOUND = "AGENT_TASK_NOT_FOUND"
    TASK_STATE_CONFLICT = "AGENT_TASK_STATE_CONFLICT"
    LOOP_BUDGET_EXCEEDED = "AGENT_LOOP_BUDGET_EXCEEDED"

    EXECUTION_UNKNOWN = "AGENT_EXECUTION_UNKNOWN"
    NOT_IMPLEMENTED = "AGENT_NOT_IMPLEMENTED"


class ExecutionState:
    """动作/步骤的执行状态，与任务状态分开表达。"""

    NOT_EXECUTED = "not_executed"
    EXECUTED = "executed"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


# 便于前端展示的中文默认说明
DEFAULT_MESSAGES: Dict[str, str] = {
    Code.UNAUTHENTICATED: "登录状态无效，请重新登录",
    Code.FORBIDDEN: "没有权限执行该操作",
    Code.VALIDATION: "请求参数不完整或格式不正确",
    Code.RESOURCE_FORBIDDEN: "无权访问该资源",
    Code.MODEL_NOT_CONFIGURED: "当前账号尚未配置可用的大模型连接",
    Code.MODEL_CAPABILITY_MISSING: "当前模型缺少本任务所需能力",
    Code.TOOL_UNKNOWN: "模型请求了不存在的工具",
    Code.TOOL_ARGS_INVALID: "工具参数不符合约定结构",
    Code.ACTION_STALE: "该确认已失效，请按最新目标重新确认",
    Code.PREPARE_BLOCKED: "业务预检未通过，该操作不能执行",
    Code.PREPARE_FAILED: "业务预检失败，未生成可确认的操作",
    Code.PLAN_NOT_CONFIRMED: "该任务需要先确认整体计划",
    Code.EXECUTION_UNKNOWN: "执行结果未能核实",
}


class AgentError(Exception):
    """带稳定错误码的业务异常。"""

    def __init__(
        self,
        code: str,
        message: Optional[str] = None,
        *,
        http_status: int = 400,
        execution_state: str = ExecutionState.NOT_EXECUTED,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.code = code
        self.message = message or DEFAULT_MESSAGES.get(code, "操作失败")
        self.http_status = http_status
        self.execution_state = execution_state
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "execution_state": self.execution_state,
        }
        if self.details:
            payload["details"] = self.details
        return payload

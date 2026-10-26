"""工具契约（B-05 / B-09）。

与第三人约定的一致语义：``list_tools / prepare_tool / execute_tool /
get_operation / get_resource_state / cancel_operation``。
本模块只定义数据结构与参数校验，具体执行由对方的适配层实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent.errors import AgentError, Code, ExecutionState

RISK_READ = "read"
RISK_WRITE = "write"
RISK_CRITICAL = "critical"

SOURCE_BUSINESS = "business"
SOURCE_MCP = "mcp"
SOURCE_STUB = "stub"
SOURCE_CONTROL = "control"

# 取消范围：业务侧只允许取消"尚未执行"的预备记录
CANCEL_SCOPE_NONE = ""
CANCEL_SCOPE_PREPARED = "prepared_only"
CANCEL_SCOPE_RUNNING = "running"

# 模型不得自行提供的参数：身份、授权、审批标记一律由程序生成
RESERVED_ARGUMENTS = {
    "approved",
    "approval",
    "authorization",
    "user_id",
    "owner_id",
    "actor",
    "roles",
    "request_id",
    "request_key",
}

_JSON_TYPES = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    version: str = "1"
    returns: Dict[str, Any] = field(default_factory=dict)
    risk: str = RISK_READ
    billable: bool = False
    source: str = SOURCE_BUSINESS
    server_id: Optional[str] = None
    requires_confirmation: Optional[bool] = None
    supports_preview: bool = False
    supports_status: bool = True
    supports_cancel: bool = False
    # 取消范围元数据。``prepared_only`` 只表示"预备了还没执行"可以撤回，
    # 不能当作"运行中也能取消"。
    cancel_scope: str = CANCEL_SCOPE_NONE

    @property
    def readonly(self) -> bool:
        return self.risk == RISK_READ

    @property
    def needs_confirmation(self) -> bool:
        """写入/计费/关键动作默认逐次确认；只读查询默认直接执行。"""
        if self.requires_confirmation is not None:
            return bool(self.requires_confirmation)
        return self.risk in (RISK_WRITE, RISK_CRITICAL) or self.billable

    def to_openai_tool(self) -> Dict[str, Any]:
        schema = dict(self.parameters or {"type": "object", "properties": {}})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        schema["additionalProperties"] = False
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "title": self.title,
            "description": self.description,
            "parameters": self.parameters,
            "returns": self.returns,
            "risk": self.risk,
            "billable": self.billable,
            "source": self.source,
            "server_id": self.server_id,
            "requires_confirmation": self.needs_confirmation,
            "supports_preview": self.supports_preview,
            "supports_status": self.supports_status,
            "supports_cancel": self.supports_cancel,
            "cancel_scope": self.cancel_scope,
        }


@dataclass
class OperationResult:
    """第三人返回的统一结果。"""

    status: str = "succeeded"
    operation_id: Optional[str] = None
    business_refs: List[Dict[str, Any]] = field(default_factory=list)
    outputs: Dict[str, Any] = field(default_factory=dict)
    item_results: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[Dict[str, Any]] = None
    is_stub: bool = False
    execution_state: str = ExecutionState.EXECUTED

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "operation_id": self.operation_id,
            "business_refs": self.business_refs,
            "outputs": self.outputs,
            "item_results": self.item_results,
            "error": self.error,
            "is_stub": self.is_stub,
            "execution_state": self.execution_state,
        }


def validate_arguments(spec: ToolSpec, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """按声明的参数结构做严格校验，并剔除保留字段。"""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise AgentError(Code.TOOL_ARGS_INVALID, "工具参数必须是对象")

    cleaned: Dict[str, Any] = {}
    for key, value in arguments.items():
        if key in RESERVED_ARGUMENTS:
            # 模型自报的身份/审批标记一律忽略，不进入执行
            continue
        cleaned[key] = value

    schema = spec.parameters or {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []

    for key in required:
        if key not in cleaned or cleaned[key] in (None, ""):
            raise AgentError(
                Code.TOOL_ARGS_INVALID,
                f"工具 {spec.name} 缺少必需参数：{key}",
                details={"tool": spec.name, "missing": key},
            )

    if properties and schema.get("additionalProperties") is False:
        unknown = [k for k in cleaned if k not in properties]
        if unknown:
            raise AgentError(
                Code.TOOL_ARGS_INVALID,
                f"工具 {spec.name} 不接受参数：{', '.join(unknown)}",
                details={"tool": spec.name, "unknown": unknown},
            )

    for key, value in list(cleaned.items()):
        definition = properties.get(key) or {}
        expected = definition.get("type")
        if expected in _JSON_TYPES and value is not None:
            python_type = _JSON_TYPES[expected]
            if expected == "number" and isinstance(value, bool):
                raise AgentError(Code.TOOL_ARGS_INVALID, f"参数 {key} 应为数字")
            if not isinstance(value, python_type):
                raise AgentError(
                    Code.TOOL_ARGS_INVALID,
                    f"参数 {key} 类型应为 {expected}",
                    details={"tool": spec.name, "parameter": key},
                )
        enum = definition.get("enum")
        if enum and value not in enum:
            raise AgentError(
                Code.TOOL_ARGS_INVALID,
                f"参数 {key} 只能取：{', '.join(map(str, enum))}",
                details={"tool": spec.name, "parameter": key},
            )
    return cleaned


class ToolProvider:
    """第三人适配层需要实现的接口（鸭子类型即可，不强制继承）。

    ``prepare`` 返回的字典约定：

    * ``preview``：给用户看的影响范围；
    * ``prepared_id``：业务侧预备记录 ID（有就必须返回），确认后执行复用它，
      不允许在执行时重新预备一份不同的预览。
    """

    def list_tools(self, actor) -> List[ToolSpec]:  # pragma: no cover - 接口声明
        raise NotImplementedError

    def prepare(self, actor, spec: ToolSpec, arguments: Dict[str, Any], context) -> Dict[str, Any]:
        return {}

    def execute(
        self,
        actor,
        spec: ToolSpec,
        arguments: Dict[str, Any],
        *,
        request_key: str,
        context,
        prepared_id: Optional[str] = None,
    ) -> OperationResult:  # pragma: no cover - 接口声明
        raise NotImplementedError

    def get_operation(self, actor, operation_id: str) -> OperationResult:  # pragma: no cover
        raise NotImplementedError

    def get_resource_state(self, actor, ref: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def cancel_operation(self, actor, operation_id: str) -> OperationResult:
        raise NotImplementedError

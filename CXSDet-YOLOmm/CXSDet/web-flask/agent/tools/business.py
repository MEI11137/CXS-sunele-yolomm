"""第三人业务工具的适配层（B-05 / B-09）。

第三人只要提供一个实现了约定方法的对象并调用 :func:`set_backend` 注入即可：

```python
class MyBusinessTools:
    def list_tools(self): ...                       # -> [ToolSpec]
    def prepare_tool(self, *, actor, spec, arguments, context): ...
    def execute_tool(self, *, actor, spec, arguments, request_key, context): ...
    def get_operation(self, *, actor, operation_id): ...
    def get_resource_state(self, *, actor, ref): ...
    def cancel_operation(self, *, actor, operation_id): ...
```

未注入时不会伪造成功：调用会抛 ``AGENT_TOOL_BACKEND_UNAVAILABLE``，
由上层明确告知"该工具尚未接入"。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.errors import AgentError, Code, ExecutionState
from agent.tools.contract import OperationResult, ToolSpec

_VALID_OPERATION_FIELDS = {
    "status",
    "operation_id",
    "business_refs",
    "outputs",
    "item_results",
    "error",
    "is_stub",
    "execution_state",
}

_backend: Optional[Any] = None


def set_backend(backend: Optional[Any]) -> None:
    """注入真实业务后端；传 None 表示撤下（只用于测试或关闭业务接入）。"""
    global _backend
    _backend = backend
    from agent.tools import registry as registry_module

    registry_module.reset_registry()


def get_backend() -> Optional[Any]:
    return _backend


def _require_backend() -> Any:
    if _backend is None:
        raise AgentError(
            Code.TOOL_BACKEND_UNAVAILABLE,
            "业务工具尚未接入：请联系第三人提供服务，或先使用工具替身开发",
            http_status=409,
        )
    return _backend


def _as_result(raw: Any) -> OperationResult:
    if isinstance(raw, OperationResult):
        return raw
    if isinstance(raw, dict):
        filtered = {k: v for k, v in raw.items() if k in _VALID_OPERATION_FIELDS}
        filtered.setdefault("status", "succeeded")
        return OperationResult(**filtered)
    raise AgentError(Code.TOOL_BACKEND_UNAVAILABLE, "业务工具返回了无法识别的结果")


class BusinessToolProvider:
    def __init__(self, backend: Any) -> None:
        self._backend = backend

    def list_tools(self, actor) -> List[ToolSpec]:
        tools = self._backend.list_tools()
        return [spec for spec in tools if isinstance(spec, ToolSpec)]

    def prepare(self, actor, spec: ToolSpec, arguments: Dict[str, Any], context) -> Dict[str, Any]:
        handler = getattr(self._backend, "prepare_tool", None)
        if handler is None:
            return {}
        raw = handler(actor=actor, spec=spec, arguments=arguments, context=context)
        if isinstance(raw, dict) and "preview" in raw:
            return raw
        return {"preview": raw or {}}

    def execute(
        self,
        actor,
        spec: ToolSpec,
        arguments: Dict[str, Any],
        *,
        request_key: str,
        context,
    ) -> OperationResult:
        backend = _require_backend()
        handler = getattr(backend, "execute_tool", None)
        if handler is None:
            raise AgentError(
                Code.TOOL_BACKEND_UNAVAILABLE,
                "业务工具缺少执行接口，无法执行该动作",
                http_status=409,
            )
        raw = handler(
            actor=actor,
            spec=spec,
            arguments=arguments,
            request_key=request_key,
            context=context,
        )
        return _as_result(raw)

    def get_operation(self, actor, operation_id: str) -> OperationResult:
        backend = _require_backend()
        handler = getattr(backend, "get_operation", None)
        if handler is None:
            raise AgentError(Code.TOOL_BACKEND_UNAVAILABLE, "业务工具不支持状态查询")
        return _as_result(handler(actor=actor, operation_id=operation_id))

    def get_resource_state(self, actor, ref: Dict[str, Any]) -> Dict[str, Any]:
        backend = _require_backend()
        handler = getattr(backend, "get_resource_state", None)
        if handler is None:
            raise AgentError(Code.TOOL_BACKEND_UNAVAILABLE, "业务工具不支持资源状态查询")
        result = handler(actor=actor, ref=ref)
        return result if isinstance(result, dict) else {"status": "unknown", "raw": result}

    def cancel_operation(self, actor, operation_id: str) -> OperationResult:
        backend = _require_backend()
        handler = getattr(backend, "cancel_operation", None)
        if handler is None:
            return OperationResult(
                status="failed",
                operation_id=operation_id,
                error={
                    "code": "AGENT_CANCEL_UNSUPPORTED",
                    "message": "该工具不支持取消，已启动的动作需如实告知用户",
                },
                execution_state=ExecutionState.EXECUTED,
            )
        return _as_result(handler(actor=actor, operation_id=operation_id))

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
    """当前 app 装配的业务后端；无应用上下文时回退模块级实例。

    app 优先是为了让多个 app（或测试库）各自持有自己的实例，不会共享同一个
    业务后端，也不会把 A 库的动作写到 B 库。
    """
    try:
        from flask import current_app

        entry = current_app.extensions.get("haixun_agent") or {}
        backend = entry.get("business_backend")
        if backend is not None:
            return backend
    except Exception:  # noqa: BLE001 - 无应用上下文时走模块级
        pass
    return _backend


def _require_backend() -> Any:
    if _backend is None:
        raise AgentError(
            Code.TOOL_BACKEND_UNAVAILABLE,
            "业务工具尚未接入：请联系第三人提供服务，或先使用工具替身开发",
            http_status=409,
        )
    return _backend


def uses_principal_contract(backend: Any) -> bool:
    """判断后端是本仓库业务适配层（``list_tools(principal)``）还是旧式适配层。

    这里按签名显式区分，不用"捕获 TypeError 再换一种调法"的兜底——那样会把
    真实内部异常也吞掉。签名不匹配就是契约不匹配，必须明确走哪条路。
    """
    import inspect

    handler = getattr(backend, "list_tools", None)
    if not callable(handler):
        return False
    try:
        params = list(inspect.signature(handler).parameters.values())
    except (TypeError, ValueError):  # 内建/无签名可查：按旧式处理
        return False
    positional = [
        p
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                      inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return bool(positional) and positional[0].name == "principal"


def build_provider(backend: Any) -> Optional[Any]:
    """按后端契约返回对应 provider；``None`` 表示没有业务后端。"""
    if backend is None:
        return None
    if uses_principal_contract(backend):
        from agent.business_bridge import BusinessBackendBridge

        return BusinessBackendBridge(backend)
    return BusinessToolProvider(backend)


# 业务端可以返回的执行状态。不在表里的状态一律不认，不能猜成成功。
_ALLOWED_STATUSES = {
    "succeeded",
    "completed",
    "completed_with_warnings",
    "partial",
    "partial_failed",
    "failed",
    "aborted",
    "cancelled",
    "running",
    "pending",
    "unknown",
}

# 每种字段允许的类型；类型不符就是契约不符，不能硬塞进 OperationResult。
_FIELD_TYPES = {
    "operation_id": (str, type(None)),
    "business_refs": (list,),
    "outputs": (dict,),
    "item_results": (list,),
    "error": (dict, type(None)),
    "is_stub": (bool,),
    "execution_state": (str,),
}


def _as_result(raw: Any) -> OperationResult:
    """把业务返回值转成 ``OperationResult``；没有证据的成功一律不接受（R7）。

    空字典、缺少状态、状态不在约定表里、字段类型不符，都不能当成
    "succeeded"——那等于用默认值伪造成功，会让用户在什么都没发生的情况下
    看到"已完成"。
    """
    if isinstance(raw, OperationResult):
        return raw
    if not isinstance(raw, dict):
        raise AgentError(Code.TOOL_BACKEND_UNAVAILABLE, "业务工具返回了无法识别的结果")
    if not raw:
        raise AgentError(
            Code.TOOL_BACKEND_UNAVAILABLE,
            "业务工具返回了空结果，无法确认是否执行成功",
        )

    unknown_fields = set(raw) - _VALID_OPERATION_FIELDS
    if unknown_fields:
        raise AgentError(
            Code.TOOL_BACKEND_UNAVAILABLE,
            "业务工具返回了未约定的字段：" + "、".join(sorted(unknown_fields)),
            details={"fields": sorted(unknown_fields)},
        )

    status = str(raw.get("status") or "").strip().lower()
    if not status:
        raise AgentError(
            Code.TOOL_BACKEND_UNAVAILABLE,
            "业务工具没有返回执行状态，无法确认是否执行成功",
        )
    if status not in _ALLOWED_STATUSES:
        raise AgentError(
            Code.TOOL_BACKEND_UNAVAILABLE,
            f"业务工具返回了无法识别的执行状态：{status}",
            details={"status": status},
        )

    filtered: Dict[str, Any] = {}
    for key, value in raw.items():
        expected = _FIELD_TYPES.get(key)
        if expected is not None and not isinstance(value, expected):
            raise AgentError(
                Code.TOOL_BACKEND_UNAVAILABLE,
                f"业务工具返回的 {key} 字段类型不符合约定",
                details={"field": key, "type": type(value).__name__},
            )
        filtered[key] = value
    filtered["status"] = status
    return OperationResult(**filtered)


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
        prepared_id: Optional[str] = None,
    ) -> OperationResult:
        backend = _require_backend()
        handler = getattr(backend, "execute_tool", None)
        if handler is None:
            raise AgentError(
                Code.TOOL_BACKEND_UNAVAILABLE,
                "业务工具缺少执行接口，无法执行该动作",
                http_status=409,
            )
        kwargs: Dict[str, Any] = {
            "actor": actor,
            "spec": spec,
            "arguments": arguments,
            "request_key": request_key,
            "context": context,
        }
        if prepared_id is not None:
            # 只有业务侧确实预备过才传，避免给旧适配层塞它不认识的关键字
            kwargs["prepared_id"] = prepared_id
        raw = handler(**kwargs)
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

"""个人大模型连接的能力探测与客户端封装（B-02）。

要点：

* 认证、聊天、图片理解、工具调用**分别**检查，未验证的能力显示 ``unchecked``，
  不能因为模型列表可访问就宣称图片或工具已可用；
* 每次检测都会真实发出小规模请求（用户主动点击检查时才会触发）；
* 认证失败与网站登录失效分开处理：这里抛的是 ``AGENT_*`` 错误码，
  不会让用户掉登录。
"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agent.errors import AgentError, Code

CAPABILITY_CHAT = "chat"
CAPABILITY_VISION = "vision"
CAPABILITY_TOOLS = "tools"
CAPABILITY_KEYS = (CAPABILITY_CHAT, CAPABILITY_VISION, CAPABILITY_TOOLS)

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_UNCHECKED = "unchecked"

REASON_AUTH_FAILED = "auth_failed"
REASON_ADDRESS_MISMATCH = "address_mismatch"
REASON_MODEL_UNAVAILABLE = "model_unavailable"
REASON_NETWORK = "network_or_timeout"
REASON_RATE_LIMITED = "rate_limited"
REASON_CAPABILITY_UNSUPPORTED = "capability_unsupported"
REASON_PARSE_ERROR = "response_not_parsable"
REASON_EMPTY_RESPONSE = "empty_response"

# 1x1 白色 PNG，用于图片能力探测，避免把用户真实业务图片发出去
_PROBE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


@dataclass(frozen=True)
class ProviderCredentials:
    provider: str
    api_url: str
    model: str
    api_key: str

    def masked(self) -> Dict[str, str]:
        from agent.crypto import mask_secret

        return {
            "provider": self.provider,
            "api_url": self.api_url,
            "model": self.model,
            "api_key_hint": mask_secret(self.api_key),
        }


@dataclass
class ToolCall:
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
    raw_arguments: str = ""

    def to_message_part(self) -> Dict[str, Any]:
        return {
            "id": self.call_id or self.name,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass
class ModelTurn:
    """模型一次返回的结构化结果。"""

    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


def classify_failure(exc: BaseException) -> tuple[str, str]:
    """把 SDK/网络异常归类成稳定原因码与中文说明。"""
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    text = str(exc)
    lowered = text.lower()

    if status in (401, 403) or "invalid api key" in lowered or "unauthorized" in lowered:
        return REASON_AUTH_FAILED, "认证失败：Key 无效或没有该模型的访问权限"
    if status == 404 and ("model" in lowered or "not found" in lowered):
        return REASON_MODEL_UNAVAILABLE, "模型名称不可用：请检查模型名是否为服务商实际提供"
    if status == 404:
        return REASON_ADDRESS_MISMATCH, "接口地址不匹配：请检查服务地址是否为兼容接口地址"
    if status == 429 or "rate limit" in lowered:
        return REASON_RATE_LIMITED, "请求被限流，请稍后再试"
    if "timeout" in lowered or "timed out" in lowered or "connection" in lowered:
        return REASON_NETWORK, "网络连接或请求超时，请检查网络与地址"
    if status == 400 and any(
        token in lowered for token in ("image", "vision", "content", "tool", "function")
    ):
        return REASON_CAPABILITY_UNSUPPORTED, "该模型不支持本次能力（图片理解或工具调用）"
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return REASON_PARSE_ERROR, "模型返回内容无法按约定解析"
    return REASON_NETWORK, f"调用失败：{text[:160]}"


def _capability_entry(
    status: str,
    reason: str = "",
    message: str = "",
    method: str = "",
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"status": status}
    if reason:
        entry["reason"] = reason
    if message:
        entry["message"] = message
    if method:
        entry["method"] = method
    return entry


class OpenAIGateway:
    """按用户凭据构造的 OpenAI 兼容客户端，供聊天/图片/工具共用。"""

    def __init__(
        self,
        credentials: ProviderCredentials,
        client_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.credentials = credentials
        self._client_factory = client_factory
        self._client = None

    def _get_client(self):
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory(
                    api_key=self.credentials.api_key,
                    base_url=self.credentials.api_url,
                )
            else:
                from openai import OpenAI

                self._client = OpenAI(
                    api_key=self.credentials.api_key,
                    base_url=self.credentials.api_url,
                    timeout=60.0,
                    max_retries=0,
                )
        return self._client

    # -- 对话 ---------------------------------------------------------------
    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        temperature: float = 0.2,
    ) -> ModelTurn:
        payload: Dict[str, Any] = {
            "model": self.credentials.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            response = self._get_client().chat.completions.create(**payload)
        except AgentError:
            raise
        except BaseException as exc:  # noqa: BLE001 - SDK 异常类型不稳定
            reason, message = classify_failure(exc)
            code = (
                Code.MODEL_CAPABILITY_MISSING
                if reason == REASON_CAPABILITY_UNSUPPORTED
                else Code.MODEL_CALL_FAILED
            )
            raise AgentError(
                code,
                message,
                http_status=502,
                details={"reason": reason},
            ) from exc

        choice = (getattr(response, "choices", None) or [None])[0]
        if choice is None:
            raise AgentError(Code.MODEL_CALL_FAILED, "模型没有返回任何结果", http_status=502)
        message_obj = getattr(choice, "message", None)
        text = (getattr(message_obj, "content", None) or "").strip()
        calls: List[ToolCall] = []
        for raw_call in getattr(message_obj, "tool_calls", None) or []:
            function = getattr(raw_call, "function", None)
            name = getattr(function, "name", "") or ""
            raw_args = getattr(function, "arguments", "") or "{}"
            try:
                arguments = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError as exc:
                raise AgentError(
                    Code.MODEL_CALL_FAILED,
                    "模型给出的工具参数不是合法 JSON",
                    http_status=502,
                    details={"tool": name},
                ) from exc
            calls.append(
                ToolCall(
                    name=name,
                    arguments=arguments,
                    call_id=getattr(raw_call, "id", "") or name,
                    raw_arguments=raw_args,
                )
            )
        return ModelTurn(text=text, tool_calls=calls)

    # -- 能力检测 -----------------------------------------------------------
    def probe_chat(self) -> Dict[str, Any]:
        try:
            response = self._get_client().chat.completions.create(
                model=self.credentials.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=8,
            )
        except AgentError as exc:
            return _capability_entry(STATUS_FAILED, exc.details.get("reason", ""), exc.message)
        except BaseException as exc:  # noqa: BLE001
            reason, message = classify_failure(exc)
            return _capability_entry(STATUS_FAILED, reason, message)
        choice = (getattr(response, "choices", None) or [None])[0]
        if choice is None:
            return _capability_entry(STATUS_FAILED, REASON_EMPTY_RESPONSE, "模型无返回")
        return _capability_entry(STATUS_OK, method="real_request")

    def probe_vision(self) -> Dict[str, Any]:
        payload = {
            "model": self.credentials.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "只回复 OK"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(_PROBE_PNG).decode("ascii")
                            },
                        },
                    ],
                }
            ],
            "max_tokens": 8,
        }
        try:
            self._get_client().chat.completions.create(**payload)
        except AgentError as exc:
            return _capability_entry(STATUS_FAILED, exc.details.get("reason", ""), exc.message)
        except BaseException as exc:  # noqa: BLE001
            reason, message = classify_failure(exc)
            if reason in (REASON_AUTH_FAILED, REASON_MODEL_UNAVAILABLE, REASON_NETWORK):
                return _capability_entry(STATUS_FAILED, reason, message)
            return _capability_entry(
                STATUS_FAILED, REASON_CAPABILITY_UNSUPPORTED, "该模型不能接收图片输入"
            )
        return _capability_entry(STATUS_OK, method="real_request")

    def probe_tools(self) -> Dict[str, Any]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "agent_probe_ping",
                    "description": "能力探测用的空操作",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                },
            }
        ]
        try:
            response = self._get_client().chat.completions.create(
                model=self.credentials.model,
                messages=[{"role": "user", "content": "调用 agent_probe_ping，value 传 ok"}],
                tools=tools,
                tool_choice="auto",
                max_tokens=32,
            )
        except AgentError as exc:
            return _capability_entry(STATUS_FAILED, exc.details.get("reason", ""), exc.message)
        except BaseException as exc:  # noqa: BLE001
            reason, message = classify_failure(exc)
            if reason in (REASON_AUTH_FAILED, REASON_MODEL_UNAVAILABLE, REASON_NETWORK):
                return _capability_entry(STATUS_FAILED, reason, message)
            return _capability_entry(
                STATUS_FAILED, REASON_CAPABILITY_UNSUPPORTED, "该模型不能按约定调用工具"
            )
        choice = (getattr(response, "choices", None) or [None])[0]
        message_obj = getattr(choice, "message", None) if choice else None
        if getattr(message_obj, "tool_calls", None):
            return _capability_entry(STATUS_OK, method="real_request")
        # 接受了工具描述但没有真正发起调用：只算"结构可用"，如实标注
        return _capability_entry(
            STATUS_OK,
            method="acceptance_only",
            message="服务接受了工具描述，但本次未实际返回工具调用；首次真实任务时请再核对",
        )

    def probe_all(self) -> Dict[str, Any]:
        return {
            CAPABILITY_CHAT: self.probe_chat(),
            CAPABILITY_VISION: self.probe_vision(),
            CAPABILITY_TOOLS: self.probe_tools(),
        }


def summarize_capabilities(capabilities: Dict[str, Any]) -> Dict[str, Any]:
    """判断整体连接状态：聊天不可用即 failed，其余按最差能力提示。"""
    chat = (capabilities or {}).get(CAPABILITY_CHAT, {}).get("status", STATUS_UNCHECKED)
    if chat == STATUS_OK:
        overall = STATUS_OK
    elif chat == STATUS_FAILED:
        overall = STATUS_FAILED
    else:
        overall = STATUS_UNCHECKED
    missing = [
        key
        for key in CAPABILITY_KEYS
        if (capabilities or {}).get(key, {}).get("status") != STATUS_OK
    ]
    return {"status": overall, "missing_capabilities": missing}


def requires(capabilities: Dict[str, Any], capability: str) -> bool:
    entry = (capabilities or {}).get(capability) or {}
    return entry.get("status") == STATUS_OK


def wrap_image_message(text: str, image_bytes: bytes) -> Dict[str, Any]:
    """构造带图片的用户消息（供图片理解/核查类工具请求使用）。"""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            },
        ],
    }

"""工具注册表与可用性过滤（B-05）。

* 工具按用户/项目/授权过滤后才暴露给模型；
* 同名工具优先使用真实业务适配层，替身只在开发环境兜底且带 ``is_stub`` 标记；
* ``propose_plan`` / ``ask_user`` 是 Agent 自己的控制工具，不落到业务层。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from agent.config import settings
from agent.errors import AgentError, Code
from agent.tools.contract import (
    RISK_READ,
    SOURCE_BUSINESS,
    SOURCE_CONTROL,
    SOURCE_MCP,
    SOURCE_STUB,
    ToolProvider,
    ToolSpec,
)

PROPOSE_PLAN = "propose_plan"
ASK_USER = "ask_user"
CONTROL_TOOL_NAMES = {PROPOSE_PLAN, ASK_USER}

_PLAN_STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "needs_confirmation": {"type": "boolean"},
        "needs_human": {"type": "boolean"},
        "note": {"type": "string"},
    },
    "required": ["title"],
    "additionalProperties": False,
}

CONTROL_PROPOSE_PLAN = ToolSpec(
    name=PROPOSE_PLAN,
    title="提出整体计划",
    description=(
        "当任务需要多步操作（例如整理数据集后再训练）时，先提出整体计划让用户确认方向。"
        "计划只确认方向，不代替后续每个关键动作的具体确认。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "goal": {"type": "string"},
            "steps": {"type": "array", "items": _PLAN_STEP_SCHEMA},
            "expected_outputs": {"type": "array", "items": {"type": "string"}},
            "open_questions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["steps"],
        "additionalProperties": False,
    },
    risk=RISK_READ,
    source=SOURCE_CONTROL,
    requires_confirmation=False,
    supports_status=False,
)

CONTROL_ASK_USER = ToolSpec(
    name=ASK_USER,
    title="向用户追问",
    description="当缺少必要材料、对象不明确或存在多个候选时，先向用户追问或请其选择，不要猜测。",
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
        "required": ["question"],
        "additionalProperties": False,
    },
    risk=RISK_READ,
    source=SOURCE_CONTROL,
    requires_confirmation=False,
    supports_status=False,
)


class ToolRegistry:
    def __init__(self) -> None:
        self._entries: List[Dict[str, Any]] = [
            {"provider": _ControlOnlyProvider(), "source": SOURCE_CONTROL}
        ]
        self.warnings: List[str] = []

    def add_provider(self, provider: ToolProvider, source: str = SOURCE_BUSINESS) -> None:
        self._entries.append({"provider": provider, "source": source})

    # -- 查询 ---------------------------------------------------------------
    def specs(self, actor, project_id: Optional[str] = None) -> List[ToolSpec]:
        """按注册顺序聚合工具，同名只保留第一个（真实业务优先于替身）。"""
        seen: Dict[str, ToolSpec] = {}
        for entry in self._entries:
            try:
                provided = entry["provider"].list_tools(actor) or []
            except AgentError as exc:
                self.warnings.append(f"{entry['source']} 工具清单不可用：{exc.message}")
                continue
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"{entry['source']} 工具清单读取失败：{exc}")
                continue
            for spec in provided:
                if spec.name in CONTROL_TOOL_NAMES:
                    continue
                if spec.name in seen:
                    continue
                if spec.source == SOURCE_STUB and not settings().allow_stub_tools:
                    continue
                seen[spec.name] = spec
        # 控制工具始终可用
        seen[PROPOSE_PLAN] = CONTROL_PROPOSE_PLAN
        seen[ASK_USER] = CONTROL_ASK_USER
        return list(seen.values())

    def openai_tools(self, actor, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return [spec.to_openai_tool() for spec in self.specs(actor, project_id)]

    def resolve(
        self, name: str, actor=None
    ) -> Tuple[ToolSpec, Optional[ToolProvider], str]:
        """返回 ``(spec, provider, source)``。真实业务同名工具优先于替身。"""
        if name == PROPOSE_PLAN:
            return CONTROL_PROPOSE_PLAN, None, SOURCE_CONTROL
        if name == ASK_USER:
            return CONTROL_ASK_USER, None, SOURCE_CONTROL

        stub_match: Optional[Tuple[ToolSpec, ToolProvider]] = None
        for entry in self._entries:
            if entry["source"] == SOURCE_CONTROL:
                continue
            try:
                provided = entry["provider"].list_tools(actor) or []
            except Exception:  # noqa: BLE001 - 单个 provider 故障不影响其他工具
                continue
            for spec in provided:
                if spec.name != name:
                    continue
                if entry["source"] == SOURCE_STUB:
                    if stub_match is None:
                        stub_match = (spec, entry["provider"])
                    continue
                return spec, entry["provider"], entry["source"]

        if stub_match is not None:
            if not settings().allow_stub_tools:
                raise AgentError(
                    Code.STUB_NOT_ALLOWED,
                    "当前配置不允许使用工具替身执行该动作",
                    http_status=409,
                    details={"tool": name},
                )
            return stub_match[0], stub_match[1], SOURCE_STUB

        raise AgentError(Code.TOOL_UNKNOWN, f"模型请求了不存在的工具：{name}", http_status=400)


class _ControlOnlyProvider:
    """仅提供控制工具的占位 provider。"""

    def list_tools(self, actor) -> List[ToolSpec]:
        return [CONTROL_PROPOSE_PLAN, CONTROL_ASK_USER]

    def execute(self, *args, **kwargs):  # pragma: no cover - 控制工具不执行
        raise AgentError(Code.VALIDATION, "控制工具不通过执行接口调用")


_registry: Optional[ToolRegistry] = None


def get_registry() -> ToolRegistry:
    """进程级注册表：真实业务后端优先，工具替身按配置兜底。"""
    global _registry
    if _registry is None:
        from agent.tools import business, stub

        registry = ToolRegistry()
        backend = business.get_backend()
        if backend is not None:
            registry.add_provider(business.BusinessToolProvider(backend), SOURCE_BUSINESS)
        if settings().allow_stub_tools:
            registry.add_provider(stub.StubToolProvider(), SOURCE_STUB)
        _registry = registry
    return _registry


def reset_registry() -> None:
    global _registry
    _registry = None

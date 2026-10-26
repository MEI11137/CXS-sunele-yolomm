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
        # 正式来源清单不可用时，替身一律不出现：否则模型会看到一个和真实工具
        # 同名、却只有假行为的工具（R3）。
        real_source_broken = False
        for entry in self._entries:
            try:
                provided = entry["provider"].list_tools(actor) or []
            except AgentError as exc:
                self.warnings.append(f"{entry['source']} 工具清单不可用：{exc.message}")
                real_source_broken = real_source_broken or entry["source"] != SOURCE_STUB
                continue
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"{entry['source']} 工具清单读取失败：{exc}")
                real_source_broken = real_source_broken or entry["source"] != SOURCE_STUB
                continue
            for spec in provided:
                if spec.name in CONTROL_TOOL_NAMES:
                    continue
                if spec.name in seen:
                    continue
                if spec.source == SOURCE_STUB:
                    if not settings().allow_stub_tools or real_source_broken:
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
        broken_sources: List[str] = []
        for entry in self._entries:
            if entry["source"] == SOURCE_CONTROL:
                continue
            try:
                provided = entry["provider"].list_tools(actor) or []
            except Exception as exc:  # noqa: BLE001 - 单个 provider 故障不影响其他工具
                if entry["source"] != SOURCE_STUB:
                    broken_sources.append(f"{entry['source']}: {exc}")
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
            if broken_sources:
                # 正式工具来源出错时用同名替身顶替，等于用假执行掩盖真故障（R3）
                raise AgentError(
                    Code.TOOL_BACKEND_UNAVAILABLE,
                    "正式工具清单暂时不可用，不会用同名替身代替执行",
                    http_status=503,
                    details={"tool": name, "failed_sources": broken_sources},
                )
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
_registry_key = None
# 持有后端强引用，避免对象被回收后 id() 复用导致命中错误的缓存
_registry_backend = None


def get_registry() -> ToolRegistry:
    """注册表缓存按"当前业务后端 + 替身开关"隔离。

    不同 app/测试库不会命中彼此的缓存。替身是否出现仍由 ``AGENT_ALLOW_STUB``
    决定；"默认关闭替身、且正式工具故障不按同名回落"属于 INT-04 R3。
    """
    global _registry, _registry_key, _registry_backend
    from agent.tools import business, stub

    backend = business.get_backend()
    allow_stub = settings().allow_stub_tools
    key = (id(backend) if backend is not None else 0, allow_stub)
    if _registry is not None and _registry_key == key:
        return _registry

    registry = ToolRegistry()
    if backend is not None:
        # 本仓库业务适配层走桥接（INT-02），旧式适配层保持原样
        registry.add_provider(business.build_provider(backend), SOURCE_BUSINESS)
    if allow_stub:
        registry.add_provider(stub.StubToolProvider(), SOURCE_STUB)
    _registry = registry
    _registry_key = key
    _registry_backend = backend
    return _registry


def reset_registry() -> None:
    global _registry, _registry_key, _registry_backend
    _registry = None
    _registry_key = None
    _registry_backend = None

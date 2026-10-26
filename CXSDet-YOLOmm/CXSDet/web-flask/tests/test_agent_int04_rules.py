"""INT-04 修复项的回归用例（R3–R7）。

上轮的 7 项审查缺陷在 ``tests/test_agent_review_boundaries.py`` 里有复现用例；
那里只证明"缺陷曾经存在"。本文件补的是修复后必须成立的**具体规则**，
防止以后靠默认值或兜底把问题又带回来：

* R3 替身默认关闭，且正式工具来源故障时不能按同名回落；
* R4 外部工具的 prepare 只能在用户确认之后调用；
* R5 写入/训练/发布/计费的确认不能被工具自己的标志位关掉；
* R6 确认卡上的目标项目必须是被核实过的实际项目；
* R7 业务结果没有明确状态时不许当成成功。
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests import test_agent_backend as baseline
from agent import permissions
from agent.config import settings
from agent.errors import AgentError, Code
from agent.identity import ActorContext
from agent.runtime import AgentRuntime
from agent.tools import business, grants
from agent.tools.contract import (
    RISK_CRITICAL,
    RISK_READ,
    RISK_WRITE,
    SOURCE_MCP,
    OperationResult,
    ToolSpec,
)
from agent.tools.registry import ToolRegistry


def _spec(name: str, **overrides) -> ToolSpec:
    payload = {
        "name": name,
        "title": name,
        "description": "review-only spec",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }
    payload.update(overrides)
    return ToolSpec(**payload)


class StubAndConfirmationRules(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = baseline.AgentBackendTest()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    # -- R3 ---------------------------------------------------------------- #
    def test_stub_tools_are_off_unless_explicitly_enabled(self) -> None:
        with patch.dict(os.environ):
            os.environ.pop("AGENT_ALLOW_STUB", None)
            self.assertFalse(settings().allow_stub_tools)

        with patch.dict(os.environ, {"AGENT_ALLOW_STUB": "1"}):
            self.assertTrue(settings().allow_stub_tools)

    def test_broken_business_source_does_not_fall_back_to_same_name_stub(self) -> None:
        from agent.tools import stub

        stub_provider = stub.StubToolProvider()
        name = stub_provider.list_tools(self.fixture.a1)[0].name

        class BrokenProvider:
            def list_tools(self, actor):
                raise RuntimeError("business backend down")

        registry = ToolRegistry()
        registry.add_provider(BrokenProvider(), source="business")
        registry.add_provider(stub_provider, source="stub")

        # 同名替身不能顶替：那会把"业务不可用"演成"执行成功"
        with self.assertRaises(AgentError) as raised:
            registry.resolve(name, self.fixture.a1)
        self.assertEqual(raised.exception.code, Code.TOOL_BACKEND_UNAVAILABLE)

        # 清单里也不该出现替身，否则模型会拿到一个只有假行为的同名工具
        names = {spec.name for spec in registry.specs(self.fixture.a1)}
        self.assertNotIn(name, names)

    # -- R5 ---------------------------------------------------------------- #
    def test_write_and_critical_tools_cannot_waive_confirmation(self) -> None:
        actor = self.fixture.a1
        write_spec = _spec("review_write", risk=RISK_WRITE, requires_confirmation=False)
        critical_spec = _spec(
            "review_publish", risk=RISK_CRITICAL, requires_confirmation=False
        )
        billable_read = _spec(
            "review_billable", risk=RISK_READ, billable=True, requires_confirmation=False
        )
        readonly_spec = _spec(
            "review_read", risk=RISK_READ, requires_confirmation=False
        )

        for spec in (write_spec, critical_spec, billable_read):
            self.assertTrue(spec.needs_confirmation, spec.name)
            self.assertTrue(grants.needs_confirmation(actor, spec), spec.name)

        # 只读且不计费的动作才允许工具自己声明免确认
        self.assertFalse(readonly_spec.needs_confirmation)
        self.assertFalse(grants.needs_confirmation(actor, readonly_spec))

    def test_mcp_grant_cannot_waive_confirmation_for_writes(self) -> None:
        actor = self.fixture.a1
        spec = _spec(
            "review_mcp_write",
            risk=RISK_WRITE,
            source=SOURCE_MCP,
            server_id="review-server",
            supports_preview=True,
        )
        grants.grant_readonly(actor, "review-server", spec.name, spec.version)
        # 放行记录只覆盖只读工具，写工具仍然每次确认
        self.assertTrue(grants.needs_confirmation(actor, spec, source=SOURCE_MCP))

    # -- R7 ---------------------------------------------------------------- #
    def test_business_result_needs_an_explicit_status(self) -> None:
        with self.assertRaises(AgentError):
            business._as_result({})
        with self.assertRaises(AgentError):
            business._as_result({"outputs": {"done": True}})
        with self.assertRaises(AgentError):
            business._as_result({"status": "probably_fine"})
        with self.assertRaises(AgentError):
            business._as_result({"status": "succeeded", "unexpected": 1})
        with self.assertRaises(AgentError):
            business._as_result({"status": "succeeded", "business_refs": "not-a-list"})

        ok = business._as_result({"status": "succeeded", "outputs": {"done": True}})
        self.assertTrue(ok.ok)
        self.assertEqual(ok.outputs, {"done": True})


class _ExternalProvider:
    """带外部预检的假 MCP 供应商：记录 prepare / execute 的真实调用次数。"""

    def __init__(self, spec: ToolSpec) -> None:
        self.spec = spec
        self.prepare_calls = 0
        self.execute_calls = []

    def list_tools(self, actor):
        return [self.spec]

    def prepare(self, actor, spec, arguments, context=None):
        self.prepare_calls += 1
        return {"preview": {"from": "external"}, "prepared_id": "prep-ext-1"}

    def execute(self, actor, spec, arguments, *, request_key, context, prepared_id=None):
        self.execute_calls.append(
            {"request_key": request_key, "prepared_id": prepared_id}
        )
        return OperationResult(status="succeeded", outputs={"ok": True})


class ExternalPrepareAndTargetRules(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = baseline.AgentBackendTest()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def _runtime(self, provider) -> AgentRuntime:
        registry = ToolRegistry()
        registry.add_provider(provider, source=SOURCE_MCP)
        gateway = baseline.ScriptedGateway(baseline.tool_turn(provider.spec.name))
        return AgentRuntime(gateway_factory=lambda actor: gateway, registry=registry)

    # -- R4 ---------------------------------------------------------------- #
    def test_external_prepare_happens_only_after_confirmation(self) -> None:
        provider = _ExternalProvider(
            _spec(
                "review_external_write",
                risk=RISK_WRITE,
                source=SOURCE_MCP,
                server_id="review-server",
                supports_preview=True,
            )
        )
        runtime = self._runtime(provider)
        session = self.fixture._session("外部工具确认前后")
        result = runtime.handle_message(self.fixture.a1, session["id"], "写一条外部记录")

        self.assertEqual(result["status"], "awaiting_action")
        self.assertEqual(provider.prepare_calls, 0, "确认前不许调用外部预检")
        self.assertEqual(provider.execute_calls, [])
        self.assertTrue(result["action"]["preview"].get("prepare_after_approval"))

        confirmed = runtime.confirm_action(
            self.fixture.a1, result["action"]["action_id"]
        )
        self.assertEqual(provider.prepare_calls, 1, "确认后只预检一次")
        self.assertEqual(len(provider.execute_calls), 1)
        self.assertEqual(provider.execute_calls[0]["prepared_id"], "prep-ext-1")
        self.assertEqual(confirmed["action"]["prepared_id"], "prep-ext-1")

    # -- R6 ---------------------------------------------------------------- #
    def test_target_project_is_the_verified_one_not_the_chat_project(self) -> None:
        target = permissions.create_project(self.fixture.a1, "实际输出项目")
        session = baseline.sessions.create_session(
            self.fixture.a1, self.fixture.project["id"], "聊天里的项目"
        )
        self.assertNotEqual(session["project_id"], target["id"])

        gateway = baseline.ScriptedGateway(
            baseline.tool_turn(
                "run_detection",
                project_id=target["id"],
                model_id="review-model",
                image_refs=["review-image.jpg"],
            )
        )
        result = self.fixture._runtime(gateway).handle_message(
            self.fixture.a1, session["id"], "在指定项目里跑检测"
        )
        self.assertEqual(result["action"]["target_projects"], [target["id"]])
        self.assertEqual(result["action"]["parameters"]["project_id"], target["id"])

    def test_unverified_target_project_is_rejected_instead_of_silently_used(self) -> None:
        other = ActorContext(user_id=2, roles=("user",), source="system")
        foreign = permissions.create_project(other, "别人的项目")
        gateway = baseline.ScriptedGateway(
            baseline.tool_turn(
                "run_detection",
                project_id=foreign["id"],
                model_id="review-model",
                image_refs=["review-image.jpg"],
            )
        )
        session = self.fixture._session("越权目标项目")
        with self.assertRaises(AgentError) as raised:
            self.fixture._runtime(gateway).handle_message(
                self.fixture.a1, session["id"], "把结果写到别人的项目里"
            )
        self.assertEqual(raised.exception.code, Code.PROJECT_NOT_FOUND)

    def test_resource_project_is_used_when_no_project_argument_is_given(self) -> None:
        # 模型这类必填 project_id 的工具走不到这条；这里直接校验判断规则本身：
        # 没写明项目、引用的资源都在同一个项目时，目标就是那个项目而不是聊天项目
        project = permissions.create_project(self.fixture.a1, "模型所在项目")
        runtime = self.fixture._runtime(
            baseline.ScriptedGateway(baseline.tool_turn("run_detection"))
        )
        resolved = runtime._target_projects(
            self.fixture.a1, self.fixture.project, {}, [project["id"]]
        )
        self.assertEqual(resolved, [project["id"]])

        # 引用分散在多个项目、又没写明目标时，如实退回聊天项目
        another = permissions.create_project(self.fixture.a1, "另一个项目")
        mixed = runtime._target_projects(
            self.fixture.a1,
            self.fixture.project,
            {},
            [project["id"], another["id"]],
        )
        self.assertEqual(mixed, [self.fixture.project["id"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)

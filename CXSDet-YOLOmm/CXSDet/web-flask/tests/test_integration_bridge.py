"""INT-02 工具契约桥接回归检查。

分三层验证，避免"两边各自注入替身都能过、合起来却不成立"：

1. ``BridgeConversionTest``：纯转换逻辑（安全名、risk、cancel_scope、引用类型）；
2. ``RealAdapterBridgeTest``：真实 ``BusinessToolAdapter`` 上跑桥接，
   描述符数量/结构、名称可逆、预备失败与资源状态核对的真实报错；
3. ``RuntimeBridgeTest``：用符合 C 签名（principal 在前）的假业务后端驱动
   Agent 运行时，验证 ``prepared_id`` 落库并复用、预检失败不生成确认卡、
   重试不改 ``request_key``。

    python -m unittest tests.test_integration_bridge -v
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from flask import Flask

from agent import actions, bootstrap, identity, operations, permissions, sessions
from agent import store
from agent.business_bridge import (
    BusinessBackendBridge,
    KIND_TO_RESOURCE_TYPE,
    descriptor_to_spec,
    business_tool_name,
    safe_tool_name,
)
from agent.errors import AgentError, Code
from agent.llm.provider import ModelTurn, ToolCall
from agent.runtime import AgentRuntime
from agent.tools import business as business_tools
from agent.tools import registry as tool_registry
from agent.tools.contract import (
    CANCEL_SCOPE_NONE,
    CANCEL_SCOPE_PREPARED,
    OperationResult,
    ToolSpec,
)
from agent.registry import register_agent, reset_registration_cache
from utils.db import Database


# --------------------------------------------------------------------------- #
# 1) 纯转换
# --------------------------------------------------------------------------- #
class BridgeConversionTest(unittest.TestCase):
    def test_safe_name_is_prefixed_and_reversible(self) -> None:
        from services.business_tool_specs import TOOL_SPECS

        seen: Dict[str, str] = {}
        for name in TOOL_SPECS:
            safe = safe_tool_name(name)
            self.assertTrue(safe.startswith("business_"))
            self.assertNotIn(".", safe)
            self.assertNotIn(safe, seen, f"安全名冲突：{safe}")
            seen[safe] = name
            self.assertEqual(business_tool_name(safe), name)

    def test_unknown_safe_name_is_not_guessed(self) -> None:
        self.assertIsNone(business_tool_name("business_not_a_real_tool"))
        self.assertIsNone(business_tool_name("query_project_overview"))

    def test_descriptor_maps_schema_effect_and_cancel_scope(self) -> None:
        descriptor = {
            "name": "detection.run",
            "version": "3",
            "description": "跑一次检测",
            "input_schema": {"type": "object", "properties": {"project_id": {"type": "string"}}},
            "output_schema": {"type": "object", "properties": {"status": {"type": "string"}}},
            "effect": "write",
            "billable": False,
            "supports_preview": True,
            "supports_status_query": False,
            "supports_cancel": "prepared_only",
        }
        spec = descriptor_to_spec(descriptor)
        self.assertIsInstance(spec, ToolSpec)
        self.assertEqual(spec.name, "business_detection_run")
        self.assertEqual(spec.version, "3")
        self.assertEqual(spec.risk, "write")
        self.assertEqual(spec.parameters, descriptor["input_schema"])
        self.assertEqual(spec.returns, descriptor["output_schema"])
        self.assertFalse(spec.supports_status)
        # prepared_only 不等于"运行中也能取消"
        self.assertFalse(spec.supports_cancel)
        self.assertEqual(spec.cancel_scope, CANCEL_SCOPE_PREPARED)

    def test_training_and_publish_are_critical(self) -> None:
        for name in ("training.start", "model.publish", "model.unpublish"):
            spec = descriptor_to_spec(
                {"name": name, "effect": "write", "supports_cancel": False}
            )
            self.assertEqual(spec.risk, "critical", name)
            self.assertTrue(spec.needs_confirmation)
            self.assertEqual(spec.cancel_scope, CANCEL_SCOPE_NONE)

    def test_running_cancel_is_kept_only_when_business_says_true(self) -> None:
        spec = descriptor_to_spec(
            {"name": "dataset.import", "effect": "write", "supports_cancel": True}
        )
        self.assertTrue(spec.supports_cancel)
        self.assertEqual(spec.cancel_scope, "running")

    def test_descriptor_without_title_falls_back_to_name(self) -> None:
        spec = descriptor_to_spec({"name": "unknown.tool", "effect": "read"})
        self.assertEqual(spec.title, "unknown.tool")
        self.assertEqual(spec.parameters, {"type": "object", "properties": {}})

    def test_garbage_descriptor_is_skipped_not_fabricated(self) -> None:
        self.assertIsNone(descriptor_to_spec(None))
        self.assertIsNone(descriptor_to_spec({}))
        self.assertIsNone(descriptor_to_spec({"name": ""}))

    def test_flatten_state_exposes_status_for_recovery(self) -> None:
        from agent.business_bridge import _flatten_state

        flat = _flatten_state(
            {
                "resource": {"id": "ds_1", "project_id": "p1", "version": "4"},
                "state": {"status": "ready", "name": "裂缝数据集"},
            },
            "ds_1",
        )
        self.assertEqual(flat["status"], "ready")
        self.assertEqual(flat["resource_id"], "ds_1")
        self.assertEqual(flat["project_id"], "p1")
        self.assertEqual(flat["resource_version"], "4")

    def test_flatten_state_without_business_answer_is_unknown(self) -> None:
        from agent.business_bridge import _flatten_state

        self.assertEqual(_flatten_state(None, "x")["status"], "unknown")
        self.assertEqual(_flatten_state({}, "x")["status"], "unknown")

    def test_principal_only_accepts_server_identity(self) -> None:
        class Actor:
            def __init__(self, user_id):
                self.user_id = user_id

        self.assertEqual(BusinessBackendBridge.principal_of(Actor(7)), 7)
        for bad in (None, 0, -1, "7", True):
            with self.assertRaises(AgentError):
                BusinessBackendBridge.principal_of(Actor(bad))

    def test_provider_selection_follows_declared_contract(self) -> None:
        class CAdapter:
            def list_tools(self, principal):
                return []

        class LegacyAdapter:
            def list_tools(self):
                return []

        self.assertIsInstance(
            business_tools.build_provider(CAdapter()), BusinessBackendBridge
        )
        self.assertNotIsInstance(
            business_tools.build_provider(LegacyAdapter()), BusinessBackendBridge
        )
        self.assertIsNone(business_tools.build_provider(None))

    def test_permission_denied_listing_returns_no_tools(self) -> None:
        class Denied:
            def list_tools(self, principal):
                raise PermissionError("no access")

        bridge = BusinessBackendBridge(Denied())
        self.assertEqual(bridge.list_tools(_actor(1)), [])


def _actor(user_id: int):
    class Actor:
        def __init__(self, uid):
            self.user_id = uid
            self.request_id = "test"

    return Actor(user_id)


# --------------------------------------------------------------------------- #
# 2) 真实业务适配层
# --------------------------------------------------------------------------- #
class RealAdapterBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="int-bridge-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self._env_backup = {
            k: os.environ.get(k)
            for k in ("AGENT_SECRET_PATH", "AGENT_CANDIDATE_ROOT")
        }
        os.environ["AGENT_SECRET_PATH"] = str(self.tmp / "secret.key")
        os.environ["AGENT_CANDIDATE_ROOT"] = str(self.tmp / "candidates")
        for key in self._env_backup:
            self.addCleanup(self._restore_env, key)

        self.db_path = str(self.tmp / "app.sqlite3")
        database = Database()
        database.db_path = self.db_path
        database.initialize_db()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM user")
            conn.execute(
                "INSERT INTO user (id,username,password,role,status) VALUES (1,'admin','x',1,1)"
            )
            conn.commit()
        finally:
            conn.close()

        (self.tmp / "store").mkdir()
        (self.tmp / "temp").mkdir()

        self.app = Flask(__name__)
        self.app.config.update(
            DATABASE=self.db_path,
            TEMP_DIR=str(self.tmp / "temp"),
            FILE_STORE_PATH=str(self.tmp / "store"),
            TESTING=True,
        )
        tool_registry.reset_registry()
        business_tools.set_backend(None)
        reset_registration_cache()
        store.reset_schema_cache()

        self.report = register_agent(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)

        self.adapter = bootstrap.get_business_backend(self.app)
        self.bridge = business_tools.build_provider(self.adapter)
        self.actor = identity.load_actor(1)

    def _restore_env(self, key: str) -> None:
        original = self._env_backup.get(key)
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original

    def test_bridge_is_used_for_real_adapter(self) -> None:
        self.assertIsInstance(self.bridge, BusinessBackendBridge)

    def test_all_listed_tools_are_agent_specs(self) -> None:
        from services.business_tool_specs import TOOL_SPECS

        specs = self.bridge.list_tools(self.actor)
        self.assertGreaterEqual(len(specs), 20, "真实业务工具没有被完整转换")
        for spec in specs:
            self.assertIsInstance(spec, ToolSpec)
            self.assertTrue(spec.name.startswith("business_"))
            self.assertIn(spec.risk, ("read", "write", "critical"))
            self.assertTrue(spec.title)
            # 描述符里的 schema 必须真的带过来
            self.assertEqual(spec.parameters.get("type"), "object")
            self.assertIn("properties", spec.parameters)
            self.assertTrue(business_tool_name(spec.name))

        # 依赖未就绪的工具被隐藏，而不是给一个必然失败的入口
        names = {tool for tool in TOOL_SPECS}
        listed = {business_tool_name(spec.name) for spec in specs}
        self.assertTrue(listed <= names)
        self.assertGreater(len(listed), 0)

    def test_real_risk_and_cancel_metadata(self) -> None:
        specs = {spec.name: spec for spec in self.bridge.list_tools(self.actor)}
        self.assertEqual(specs["business_dataset_get_state"].risk, "read")
        self.assertTrue(specs["business_review_ai"].billable)
        self.assertTrue(specs["business_review_ai"].needs_confirmation)
        self.assertIn("business_training_start", specs)
        self.assertEqual(specs["business_training_start"].risk, "critical")
        # 业务侧只支持撤回预备记录，运行中不可取消
        self.assertFalse(specs["business_detection_run"].supports_cancel)
        self.assertEqual(
            specs["business_detection_run"].cancel_scope, CANCEL_SCOPE_PREPARED
        )

    def test_prepare_failure_has_no_prepared_id(self) -> None:
        spec = next(
            s for s in self.bridge.list_tools(self.actor)
            if s.name == "business_dataset_get_state"
        )
        with self.assertRaises(AgentError) as ctx:
            self.bridge.prepare(self.actor, spec, {"dataset_id": "ds_missing"}, None)
        # 资源不存在/未登记授权：必须报错，不能返回可执行的空预览
        self.assertIn(
            ctx.exception.code,
            (Code.TOOL_NOT_GRANTED, Code.RESOURCE_NOT_FOUND, Code.PREPARE_BLOCKED),
        )

    def test_resource_state_rejects_index_id(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self.bridge.get_resource_state(
                self.actor, {"kind": "dataset", "id": "res_abc123"}
            )
        self.assertEqual(ctx.exception.code, Code.VALIDATION)

    def test_resource_state_rejects_unsupported_kind(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self.bridge.get_resource_state(
                self.actor, {"kind": "universe", "id": "u1"}
            )
        self.assertEqual(ctx.exception.code, Code.TOOL_UNAVAILABLE)

    def test_resource_kind_mapping_covers_detection_records(self) -> None:
        self.assertEqual(KIND_TO_RESOURCE_TYPE["detection_record"], "record")
        self.assertEqual(KIND_TO_RESOURCE_TYPE["record"], "record")


# --------------------------------------------------------------------------- #
# 3) 运行时 + 符合 C 签名的假业务后端
# --------------------------------------------------------------------------- #
class FakeBusinessAdapter:
    """按 C 的真实签名实现最小业务适配层（principal 在前）。"""

    prepared_id_prefix = "cprep_"

    def __init__(self) -> None:
        self.prepared: Dict[str, Dict[str, Any]] = {}
        self.operations: Dict[str, Dict[str, Any]] = {}
        self.prepare_calls: List[Dict[str, Any]] = []
        self.execute_calls: List[Dict[str, Any]] = []
        self.cancelled: List[str] = []
        self.block_prepare = False
        self.prepare_error: Optional[str] = None
        self.read_failures = 0
        self._seq = 0

    # -- 描述符 -------------------------------------------------------------
    def list_tools(self, principal: int) -> List[Dict[str, Any]]:
        return [
            {
                "name": "detection.run",
                "version": "1",
                "description": "对指定图片跑一次检测",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "model_id": {"type": "string"},
                        "image_refs": {"type": "array"},
                    },
                    "required": ["project_id", "model_id"],
                    "additionalProperties": False,
                },
                "output_schema": {"type": "object", "properties": {}},
                "effect": "write",
                "billable": False,
                "supports_preview": True,
                "supports_status_query": True,
                "supports_cancel": "prepared_only",
            },
            {
                "name": "dataset.get_state",
                "version": "1",
                "description": "读取数据集当前状态",
                "input_schema": {
                    "type": "object",
                    "properties": {"dataset_id": {"type": "string"}},
                    "required": ["dataset_id"],
                    "additionalProperties": False,
                },
                "output_schema": {"type": "object", "properties": {}},
                "effect": "read",
                "billable": False,
                "supports_preview": True,
                "supports_status_query": True,
                "supports_cancel": "prepared_only",
            },
        ]

    # -- 预备/执行 ---------------------------------------------------------
    def prepare_tool(self, principal: int, tool_name: str, arguments: dict) -> Dict[str, Any]:
        self.prepare_calls.append(
            {"principal": principal, "tool": tool_name, "arguments": dict(arguments)}
        )
        if self.prepare_error:
            raise ValueError(self.prepare_error)
        self._seq += 1
        prepared_id = f"{self.prepared_id_prefix}{self._seq}"
        self.prepared[prepared_id] = {
            "principal": principal,
            "tool": tool_name,
            "arguments": dict(arguments),
            "status": "prepared",
        }
        preview: Dict[str, Any] = {
            "resource_id": arguments.get("dataset_id") or arguments.get("project_id"),
            "project_id": arguments.get("project_id"),
            "version": "1",
            "summary": f"{tool_name} 预览",
        }
        if self.block_prepare:
            preview["blocked"] = True
            preview["blocked_reasons"] = ["数据集还没有标注"]
        return {"operation_id": prepared_id, "status": "prepared", "preview": preview}

    def execute_tool(self, principal: int, prepared_id: str, request_key: str) -> Dict[str, Any]:
        self.execute_calls.append(
            {"principal": principal, "prepared_id": prepared_id, "request_key": request_key}
        )
        record = self.prepared.get(prepared_id)
        if record is None or record["principal"] != principal:
            raise PermissionError("Operation is not accessible")

        if self.read_failures > 0 and record["tool"] == "dataset.get_state":
            self.read_failures -= 1
            raise TimeoutError("业务读取超时")

        if request_key in self.operations:
            return dict(self.operations[request_key])

        self._seq += 1
        operation_id = f"cop_{self._seq}"
        result = {
            "operation_id": operation_id,
            "status": "succeeded",
            "business_refs": [
                {"type": "record", "id": "rec_1", "version": "1"},
                {"type": "dataset", "id": "ds_1", "version": "2"},
            ],
            "outputs": {"sum": 1},
            "item_results": [],
            "error": None,
        }
        self.operations[request_key] = result
        record["status"] = "succeeded"
        return dict(result)

    def get_operation(self, principal: int, operation_id: str) -> Dict[str, Any]:
        for payload in self.operations.values():
            if payload["operation_id"] == operation_id:
                return dict(payload)
        raise FileNotFoundError("Operation is not registered")

    def get_resource_state(self, principal: int, resource_type: str, resource_id: str) -> Dict[str, Any]:
        return {
            "resource": {"id": resource_id, "project_id": "p1", "version": "2"},
            "state": {"status": "ready"},
        }

    def cancel_operation(self, principal: int, operation_id: str) -> Dict[str, Any]:
        self.cancelled.append(operation_id)
        record = self.prepared.get(operation_id)
        if record is None:
            raise FileNotFoundError("Operation is not registered")
        if record["status"] != "prepared":
            from services.business_tools import ToolConflict

            raise ToolConflict("Only an unclaimed preparation can be cancelled")
        record["status"] = "cancelled"
        return {
            "operation_id": operation_id,
            "status": "cancelled",
            "business_refs": [],
            "outputs": {},
            "item_results": [],
            "error": None,
        }


class ScriptedGateway:
    def __init__(self, *turns: ModelTurn) -> None:
        self.turns: List[ModelTurn] = list(turns)

    def complete(self, messages, tools=None, **kwargs) -> ModelTurn:
        if not self.turns:
            return ModelTurn(text="好的，本轮结束。")
        return self.turns.pop(0)


def tool_turn(name: str, **arguments: Any) -> ModelTurn:
    return ModelTurn(tool_calls=[ToolCall(name=name, arguments=arguments, call_id=name)])


class RuntimeBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="int-bridge-rt-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self._env_backup = {
            k: os.environ.get(k)
            for k in ("AGENT_SECRET_PATH", "AGENT_ALLOW_STUB", "AGENT_MAX_STEPS")
        }
        os.environ["AGENT_SECRET_PATH"] = str(self.tmp / "secret.key")
        os.environ["AGENT_ALLOW_STUB"] = "0"
        os.environ["AGENT_MAX_STEPS"] = "4"
        for key in self._env_backup:
            self.addCleanup(self._restore_env, key)

        self.db_path = str(self.tmp / "rt.sqlite3")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS user (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username VARCHAR(50) NOT NULL UNIQUE,
                    password VARCHAR(100) NOT NULL,
                    real_name VARCHAR(50),
                    phone VARCHAR(20),
                    email VARCHAR(100),
                    avatar_bucket VARCHAR(50),
                    avatar_object_key VARCHAR(255),
                    role INTEGER NOT NULL DEFAULT 0,
                    status INTEGER NOT NULL DEFAULT 1,
                    create_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.execute(
                "INSERT INTO user (id,username,password,role,status) VALUES (1,'u1','x',0,1)"
            )
            conn.commit()
        finally:
            conn.close()

        self.app = Flask(__name__)
        self.app.config.update(DATABASE=self.db_path, TESTING=True)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)

        store.reset_schema_cache()
        tool_registry.reset_registry()
        reset_registration_cache()

        self.backend = FakeBusinessAdapter()
        business_tools.set_backend(self.backend)
        tool_registry.reset_registry()

        self.actor = identity.load_actor(1)
        self.project = permissions.ensure_default_project(self.actor)

    def _restore_env(self, key: str) -> None:
        original = self._env_backup.get(key)
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original

    def _session(self, title: str = "桥接会话") -> Dict[str, Any]:
        return sessions.create_session(self.actor, self.project["id"], title)

    def _run(self, session: Dict[str, Any], *turns: ModelTurn) -> Dict[str, Any]:
        runtime = AgentRuntime(gateway_factory=lambda actor: ScriptedGateway(*turns))
        return runtime.handle_message(self.actor, session["id"], "继续")

    # -- 工具清单 -----------------------------------------------------------
    def test_runtime_sees_business_tools_through_bridge(self) -> None:
        registry = tool_registry.get_registry()
        names = {spec.name for spec in registry.specs(self.actor)}
        self.assertIn("business_detection_run", names)
        self.assertIn("business_dataset_get_state", names)
        self.assertIn("propose_plan", names)
        self.assertFalse(
            [name for name in names if name.startswith("stub_")],
            "关闭替身后不应再出现替身工具",
        )

    # -- 写入：预备 → 确认 → 执行 -------------------------------------------
    def test_write_action_persists_prepared_id_and_executes_with_it(self) -> None:
        session = self._session()
        result = self._run(
            session,
            tool_turn(
                "business_detection_run",
                project_id=self.project["id"],
                model_id="m1",
                image_refs=["a.jpg"],
            ),
        )
        action = result.get("action") or {}
        self.assertTrue(action, f"没有生成确认卡：{result}")
        self.assertEqual(action["status"], actions.STATUS_PENDING)
        prepared_id = action.get("prepared_id")
        self.assertTrue(prepared_id, "确认卡没有绑定业务预备记录")
        self.assertIn(prepared_id, self.backend.prepared)
        self.assertEqual(action["preview"].get("prepared_id"), prepared_id)
        # 确认卡上应带可读标题
        self.assertEqual(action["tool_title"], "运行检测")

        runtime = AgentRuntime(gateway_factory=lambda actor: ScriptedGateway(ModelTurn(text="完成。")))
        runtime.confirm_action(self.actor, action["id"])

        self.assertEqual(len(self.backend.execute_calls), 1)
        call = self.backend.execute_calls[0]
        self.assertEqual(call["prepared_id"], prepared_id, "执行没有复用绑定的预备记录")
        self.assertEqual(call["request_key"], action["request_key"])
        self.assertEqual(call["principal"], 1)

        stored = actions.get_action(self.actor, action["id"])
        self.assertEqual(stored["status"], actions.STATUS_SUCCEEDED)
        self.assertTrue(stored["operation_id"])
        # 业务引用 record → detection_record
        op = operations.get_by_action(action["id"])
        kinds = {ref["kind"] for ref in op["business_refs"]}
        self.assertIn("detection_record", kinds)
        self.assertIn("dataset", kinds)

    def test_prepare_blocked_creates_no_action(self) -> None:
        self.backend.block_prepare = True
        session = self._session("预检被拦截")
        result = self._run(
            session,
            tool_turn(
                "business_detection_run",
                project_id=self.project["id"],
                model_id="m1",
                image_refs=["a.jpg"],
            ),
        )
        self.assertIsNone(result.get("action"))
        self.assertNotEqual(result.get("status"), "awaiting_action")
        self.assertEqual(actions.list_actions(result["task"]["id"]), [])
        self.assertEqual(self.backend.execute_calls, [])

    def test_prepare_error_creates_no_action(self) -> None:
        self.backend.prepare_error = "数据集缺少标注"
        session = self._session("预检报错")
        result = self._run(
            session,
            tool_turn(
                "business_detection_run",
                project_id=self.project["id"],
                model_id="m1",
                image_refs=["a.jpg"],
            ),
        )
        self.assertIsNone(result.get("action"))
        self.assertEqual(actions.list_actions(result["task"]["id"]), [])

    # -- 只读：服务端补预备，仍走业务幂等 -----------------------------------
    def test_read_tool_prepares_server_side_and_reuses_request_key(self) -> None:
        session = self._session("只读")
        result = self._run(
            session,
            tool_turn("business_dataset_get_state", dataset_id="ds_1"),
            ModelTurn(text="读完了。"),
        )
        self.assertIsNone(result.get("action"))
        self.assertEqual(len(self.backend.execute_calls), 1)
        prepared_id = self.backend.execute_calls[0]["prepared_id"]
        self.assertTrue(prepared_id.startswith(FakeBusinessAdapter.prepared_id_prefix))

    def test_retry_keeps_same_request_key(self) -> None:
        self.backend.read_failures = 1
        session = self._session("重试")
        self._run(
            session,
            tool_turn("business_dataset_get_state", dataset_id="ds_1"),
            ModelTurn(text="读完了。"),
        )
        self.assertEqual(len(self.backend.execute_calls), 2, "只读工具应重试一次")
        keys = {call["request_key"] for call in self.backend.execute_calls}
        self.assertEqual(len(keys), 1, "重试不能更换 request_key，否则绕过业务幂等")

    def test_same_request_key_returns_same_business_operation(self) -> None:
        bridge = BusinessBackendBridge(self.backend)
        spec = next(
            s for s in bridge.list_tools(self.actor)
            if s.name == "business_detection_run"
        )
        prepared = bridge.prepare(
            self.actor, spec, {"project_id": self.project["id"], "model_id": "m1"}, None
        )
        first = bridge.execute(
            self.actor,
            spec,
            {"project_id": self.project["id"], "model_id": "m1"},
            request_key="req_fixed",
            context=None,
            prepared_id=prepared["prepared_id"],
        )
        second = bridge.execute(
            self.actor,
            spec,
            {"project_id": self.project["id"], "model_id": "m1"},
            request_key="req_fixed",
            context=None,
            prepared_id=prepared["prepared_id"],
        )
        self.assertEqual(first.operation_id, second.operation_id)
        self.assertEqual(len(self.backend.operations), 1)

    # -- 取消范围 -----------------------------------------------------------
    def test_cancelling_pending_action_cancels_business_preparation(self) -> None:
        session = self._session("取消")
        result = self._run(
            session,
            tool_turn(
                "business_detection_run",
                project_id=self.project["id"],
                model_id="m1",
                image_refs=["a.jpg"],
            ),
        )
        action = result["action"]
        prepared_id = action["prepared_id"]
        runtime = AgentRuntime(gateway_factory=lambda actor: ScriptedGateway(ModelTurn(text="好的。")))
        runtime.reject_action(self.actor, action["id"], "先不做")
        self.assertIn(prepared_id, self.backend.cancelled)
        self.assertEqual(
            self.backend.prepared[prepared_id]["status"], "cancelled"
        )

    def test_cancel_after_execution_fails_instead_of_pretending(self) -> None:
        bridge = BusinessBackendBridge(self.backend)
        spec = next(
            s for s in bridge.list_tools(self.actor)
            if s.name == "business_detection_run"
        )
        prepared = bridge.prepare(
            self.actor, spec, {"project_id": self.project["id"], "model_id": "m1"}, None
        )
        bridge.execute(
            self.actor,
            spec,
            {"project_id": self.project["id"], "model_id": "m1"},
            request_key="req_cancel",
            context=None,
            prepared_id=prepared["prepared_id"],
        )
        result = bridge.cancel_operation(self.actor, prepared["prepared_id"])
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "BUSINESS_CANCEL_FAILED")

    def test_resource_changed_at_execution_requires_new_confirmation(self) -> None:
        """业务判"资源已变"必须映射成 ACTION_STALE，让用户重新预览确认。"""
        from services.business_tools import ToolConflict

        class ConflictAdapter(FakeBusinessAdapter):
            def execute_tool(self, principal, prepared_id, request_key):
                raise ToolConflict("Resource changed since preparation; prepare again")

        bridge = BusinessBackendBridge(ConflictAdapter())
        spec = ToolSpec(
            name="business_detection_run", title="运行检测", description="x",
            risk="write",
        )
        with self.assertRaises(AgentError) as ctx:
            bridge.execute(
                self.actor, spec, {"project_id": "p1"}, request_key="req_x",
                context=None, prepared_id="cprep_1",
            )
        self.assertEqual(ctx.exception.code, Code.ACTION_STALE)

    def test_prepare_blocked_carries_reasons(self) -> None:
        self.backend.block_prepare = True
        bridge = BusinessBackendBridge(self.backend)
        spec = next(
            s for s in bridge.list_tools(self.actor)
            if s.name == "business_detection_run"
        )
        with self.assertRaises(AgentError) as ctx:
            bridge.prepare(
                self.actor, spec, {"project_id": self.project["id"], "model_id": "m1"}, None
            )
        self.assertEqual(ctx.exception.code, Code.PREPARE_BLOCKED)
        self.assertIn("标注", ctx.exception.message)

    # -- 资源状态 -----------------------------------------------------------
    def test_resource_state_is_flattened_for_recovery(self) -> None:
        bridge = BusinessBackendBridge(self.backend)
        state = bridge.get_resource_state(
            self.actor, {"kind": "detection_record", "id": "rec_1"}
        )
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["resource_id"], "rec_1")

    def test_agent_operation_id_is_mapped_to_business_id(self) -> None:
        bridge = BusinessBackendBridge(self.backend)
        spec = next(
            s for s in bridge.list_tools(self.actor)
            if s.name == "business_detection_run"
        )
        prepared = bridge.prepare(
            self.actor, spec, {"project_id": self.project["id"], "model_id": "m1"}, None
        )
        result = bridge.execute(
            self.actor,
            spec,
            {"project_id": self.project["id"], "model_id": "m1"},
            request_key="req_map",
            context=None,
            prepared_id=prepared["prepared_id"],
        )
        record = operations.record(
            self.actor,
            task_id=None,
            action_id=None,
            tool_name=spec.name,
            request_key="req_map",
            result=result,
        )
        fetched = bridge.get_operation(self.actor, record["id"])
        self.assertEqual(fetched.operation_id, result.operation_id)


if __name__ == "__main__":
    unittest.main()

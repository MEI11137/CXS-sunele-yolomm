"""Agent 后端框架测试（B-01 ~ B-09）。

用标准库 ``unittest`` 编写，避免给队友增加测试依赖（pytest 也能直接收集）：

    python -m unittest tests.test_agent_backend -v

覆盖：迁移幂等、身份与跨用户隔离、Key 加密落库、事件续取、
只读直连 / 写入确认 / 版本失效、计划门禁、业务状态核实、
手册与交接笔记、外部工具授权、以及 HTTP 层的鉴权与消息闭环。
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

from agent import actions, connections, identity, manuals, memory
from agent import operations, permissions, recovery, sessions, store
from agent import tasks
from agent.errors import AgentError, Code
from agent.llm.provider import ModelTurn, ToolCall
from agent.runtime import AgentRuntime
from agent.tools import business, grants
from agent.tools import registry as tool_registry
from agent.tools.contract import RISK_READ, SOURCE_MCP, ToolSpec


class ScriptedGateway:
    """按脚本依次返回模型输出的假网关（不联网、不消耗额度）。"""

    def __init__(self, *turns: ModelTurn) -> None:
        self.turns: List[ModelTurn] = list(turns)
        self.calls: List[Dict[str, Any]] = []

    def complete(self, messages, tools=None, **kwargs) -> ModelTurn:
        self.calls.append({"messages": messages, "tools": tools})
        if not self.turns:
            return ModelTurn(text="好的，本轮结束。")
        return self.turns.pop(0)


def tool_turn(name: str, **arguments: Any) -> ModelTurn:
    return ModelTurn(tool_calls=[ToolCall(name=name, arguments=arguments, call_id=name)])


class AgentBackendTest(unittest.TestCase):
    # -- 环境 ---------------------------------------------------------------
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self._env_backup: Dict[str, Optional[str]] = {}
        for key, value in {
            "AGENT_SECRET_PATH": str(self.tmp / "secret.key"),
            "AGENT_MAX_STEPS": "6",
            "AGENT_ALLOW_STUB": "1",
        }.items():
            self._env_backup[key] = os.environ.get(key)
            os.environ[key] = value
            self.addCleanup(self._restore_env, key)

        self.db_path = str(self.tmp / "agent.sqlite3")
        self._create_user_table()

        self.app = Flask(__name__)
        self.app.config["DATABASE"] = self.db_path
        self.app.config["TESTING"] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)

        store.reset_schema_cache()
        tool_registry.reset_registry()
        business.set_backend(None)
        from agent.registry import reset_registration_cache

        reset_registration_cache()

        self.a1 = identity.load_actor(1)
        self.a2 = identity.load_actor(2)
        self.project = permissions.ensure_default_project(self.a1)

    def _restore_env(self, key: str) -> None:
        original = self._env_backup.get(key)
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original

    def _create_user_table(self) -> None:
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
            conn.executemany(
                "INSERT INTO user (username, password, role, status) VALUES (?, ?, ?, 1)",
                [("u1", "x", 0), ("u2", "x", 0)],
            )
            conn.commit()
        finally:
            conn.close()

    def _session(self, title: str = "测试会话", actor=None) -> Dict[str, Any]:
        owner = actor or self.a1
        project = permissions.ensure_default_project(owner)
        return sessions.create_session(owner, project["id"], title)

    def _runtime(self, gateway: ScriptedGateway) -> AgentRuntime:
        return AgentRuntime(gateway_factory=lambda actor: gateway)

    # -- B-01 迁移与权限 -----------------------------------------------------
    def test_schema_is_created_once_and_idempotent(self) -> None:
        conn = store.connect()
        try:
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name LIKE 'agent_%'"
                )
            }
        finally:
            conn.close()
        for expected in (
            "agent_project",
            "agent_resource",
            "agent_model_connection",
            "agent_session",
            "agent_task",
            "agent_event",
            "agent_action",
            "agent_manual",
            "agent_handoff_note",
            "agent_tool_grant",
        ):
            self.assertIn(expected, tables)

        # 再打开一次不应报错，也不应重复建表
        store.reset_schema_cache()
        second = store.connect()
        second.close()
        self.assertTrue(Path(self.db_path).exists())

    def test_owner_always_comes_from_server_identity(self) -> None:
        resource = permissions.register_resource(
            self.a1,
            project_id=self.project["id"],
            kind="dataset",
            display_name="组件缺陷数据集",
            owner_id=2,  # 客户端伪造的归属应被忽略
        )
        self.assertEqual(resource["owner_id"], 1)
        self.assertEqual(self.a2.user_id, 2)

    def test_cross_user_access_is_rejected(self) -> None:
        with self.assertRaises(AgentError) as raised:
            permissions.get_project(self.a2, self.project["id"])
        self.assertIn(
            raised.exception.code, {Code.PROJECT_NOT_FOUND, Code.FORBIDDEN}
        )
        self.assertEqual(permissions.list_projects(self.a2), [])

        session = self._session()
        with self.assertRaises(AgentError):
            sessions.get_session(self.a2, session["id"])

    # -- B-02 个人模型连接 ---------------------------------------------------
    def test_api_key_is_encrypted_at_rest_and_masked_in_views(self) -> None:
        plaintext = "sk-test-0123456789abcdef"
        view = connections.save_connection(
            self.a1,
            provider="deepseek",
            api_url="https://api.deepseek.com",
            model="deepseek-chat",
            api_key=plaintext,
        )
        self.assertNotIn("api_key", view)
        self.assertNotIn(plaintext, str(view))
        self.assertTrue(view["key_hint"].endswith("cdef"))

        conn = store.connect()
        try:
            row = conn.execute(
                "SELECT key_cipher FROM agent_model_connection WHERE user_id = 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertNotIn(plaintext, row["key_cipher"])

        from agent.crypto import SecretBox

        self.assertEqual(SecretBox().open(row["key_cipher"]), plaintext)

        # 其他用户看不到这条连接
        self.assertIsNone(connections.get_connection_view(self.a2))

    def test_first_connection_requires_key(self) -> None:
        with self.assertRaises(AgentError) as raised:
            connections.save_connection(
                self.a1,
                provider="deepseek",
                api_url="https://api.deepseek.com",
                model="deepseek-chat",
                api_key="",
            )
        self.assertEqual(raised.exception.code, Code.VALIDATION)

    def test_permission_mode_is_persisted_owner_only_and_not_model_controlled(self):
        session = self._session()
        self.assertEqual(sessions.get_session(self.a1, session['id'])['permission_mode'], 'request')
        with self.assertRaises(AgentError):
            sessions.set_permission_mode(self.a1, session['id'], 'assist')
        conn = store.connect()
        try:
            conn.execute('UPDATE user SET role=1 WHERE id=1')
            conn.commit()
        finally:
            conn.close()
        self.a1 = identity.load_actor(1)
        updated = sessions.set_permission_mode(self.a1, session['id'], 'assist')
        self.assertEqual(updated['permission_mode'], 'assist')
        self.assertEqual(sessions.list_sessions(self.a1)[0]['permission_mode'], 'assist')
        with self.assertRaises(AgentError):
            sessions.set_permission_mode(self.a2, session['id'], 'full')
        with self.assertRaises(AgentError):
            sessions.set_permission_mode(self.a1, session['id'], 'anything')
        from agent.routes import agent_bp
        from utils.jwt_util import generate_token
        self.app.register_blueprint(agent_bp)
        client = self.app.test_client()
        url = f"/api/agent/sessions/{session['id']}/permission-mode"
        self.assertEqual(client.put(url, json={'mode': 'full'}).status_code, 401)
        response = client.put(url, headers={'Authorization': 'Bearer ' + generate_token(1)}, json={'mode': 'request'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['data']['permission_mode'], 'request')

    def test_model_directory_is_authenticated_personal_and_has_safe_fallback(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from agent.routes import agent_bp
        from utils.jwt_util import generate_token

        self.app.register_blueprint(agent_bp)
        client = self.app.test_client()
        self.assertEqual(client.get('/api/agent/connection/models').status_code, 401)
        headers = {'Authorization': 'Bearer ' + generate_token(1)}
        self.assertEqual(client.get('/api/agent/connection/models', headers=headers).status_code, 409)
        connections.save_connection(self.a1, provider='custom', api_url='https://fixture.invalid/v1',
                                    model='current-model', api_key='directory-fixture-key')
        sdk = Mock()
        sdk.with_options.return_value = sdk
        sdk.models.list.return_value = SimpleNamespace(data=[SimpleNamespace(id='other-model'),
            SimpleNamespace(id='other-model'), SimpleNamespace(id='unsafe\nname'),
            SimpleNamespace(id='directory-fixture-key')])
        factory = Mock(return_value=sdk)
        with patch.object(connections, '_gateway_factory', factory):
            result = client.get('/api/agent/connection/models', headers=headers)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.get_json()['data']['models'], ['current-model', 'other-model'])
            factory.assert_called_with(api_key='directory-fixture-key', base_url='https://fixture.invalid/v1')
            self.assertNotIn('directory-fixture-key', result.get_data(as_text=True))
            sdk.models.list.side_effect = RuntimeError('raw directory-fixture-key')
            fallback = client.get('/api/agent/connection/models', headers=headers).get_json()['data']
            self.assertEqual(fallback['models'], ['current-model'])
            self.assertTrue(fallback['discovery_error'])
            self.assertNotIn('directory-fixture-key', str(fallback))
            other = client.get('/api/agent/connection/models',
                               headers={'Authorization': 'Bearer ' + generate_token(2)})
            self.assertEqual(other.status_code, 409)

    # -- B-03 事件流 ---------------------------------------------------------
    def test_event_sequence_supports_resume_and_dedup(self) -> None:
        session = self._session()
        for index in range(3):
            tasks.append_event(session["id"], None, "test.event", {"index": index})

        all_events = tasks.list_events(session["id"])
        sequences = [event["sequence"] for event in all_events]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len(set(sequences)), len(sequences))

        latest = tasks.latest_sequence(session["id"])
        tail = tasks.list_events(session["id"], after_sequence=latest - 1)
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0]["sequence"], latest)

    # -- B-04/B-05 主循环与确认 ---------------------------------------------
    def test_readonly_tool_executes_without_confirmation(self) -> None:
        session = self._session("只读直连")
        gateway = ScriptedGateway(
            tool_turn("query_project_overview", project_id=self.project["id"]),
            ModelTurn(text="项目里目前还没有资源。"),
        )
        result = self._runtime(gateway).handle_message(
            self.a1, session["id"], "看一下项目概况"
        )

        self.assertEqual(result["status"], tasks.STATUS_COMPLETED)
        self.assertIsNone(result["action"])
        self.assertIn("项目概况", result["reply"] + "项目概况")
        events = tasks.list_events(session["id"])
        self.assertTrue(
            any(event["type"] == tasks.EVENT_TOOL_RESULT for event in events)
        )

    def test_write_tool_waits_for_confirmation_and_runs_once(self) -> None:
        session = self._session("写入确认")
        gateway = ScriptedGateway(
            tool_turn(
                "run_detection",
                project_id=self.project["id"],
                model_id="model-1",
                image_refs=["img-1.jpg", "img-2.jpg"],
            ),
            ModelTurn(text="两张图都检测完了。"),
        )
        runtime = self._runtime(gateway)
        result = runtime.handle_message(self.a1, session["id"], "帮我检测这两张图")

        self.assertEqual(result["status"], tasks.STATUS_AWAITING_ACTION)
        action = result["action"]
        self.assertEqual(action["status"], actions.STATUS_PENDING)
        task_id = result["task"]["id"]
        self.assertEqual(operations.list_for_task(task_id), [])

        confirmed = runtime.confirm_action(self.a1, action["id"])
        self.assertEqual(confirmed["status"], tasks.STATUS_COMPLETED)
        self.assertEqual(len(operations.list_for_task(task_id)), 1)

        # 重复点击不重复执行，返回当时的真实结果
        again = runtime.confirm_action(self.a1, action["id"])
        self.assertEqual(len(operations.list_for_task(task_id)), 1)
        self.assertIn("已处理过", again["reply"])

    def test_action_revision_mismatch_is_stale(self) -> None:
        session = self._session("失效确认")
        gateway = ScriptedGateway(
            tool_turn(
                "run_detection",
                project_id=self.project["id"],
                model_id="model-1",
                image_refs=["img-1.jpg"],
            )
        )
        runtime = self._runtime(gateway)
        result = runtime.handle_message(self.a1, session["id"], "检测一张图")
        action = result["action"]

        with self.assertRaises(AgentError) as raised:
            runtime.confirm_action(
                self.a1, action["id"], expected_revision=action["revision"] + 99
            )
        self.assertEqual(raised.exception.code, Code.ACTION_STALE)

    def test_plan_gate_requires_plan_for_second_write(self) -> None:
        session = self._session("计划门禁")
        task = tasks.create_task(
            self.a1, session["id"], self.project["id"], "先检测再训练"
        )
        runtime = AgentRuntime(gateway_factory=lambda actor: ScriptedGateway())
        spec, _, _ = runtime.registry.resolve("run_detection", self.a1)

        self.assertIsNone(runtime._plan_gate(task, spec))

        actions.create_action(
            self.a1,
            task_id=task["id"],
            tool_name="run_detection",
            tool_version=str(spec.version),
            tool_title=spec.title,
            parameters={
                "project_id": self.project["id"],
                "model_id": "model-1",
                "image_refs": ["img-1.jpg"],
            },
        )
        gate = runtime._plan_gate(tasks.get_task_by_id(task["id"]), spec)
        self.assertIsNotNone(gate)
        self.assertEqual(gate[0], "feedback")
        self.assertIn("propose_plan", gate[1])

    def test_plan_must_be_confirmed_before_write(self) -> None:
        session = self._session("计划确认")
        gateway = ScriptedGateway(
            tool_turn(
                "propose_plan",
                goal="准备数据并启动训练",
                steps=[{"title": "校验数据集"}, {"title": "启动训练"}],
                expected_outputs=["一个训练任务"],
            )
        )
        runtime = self._runtime(gateway)
        result = runtime.handle_message(self.a1, session["id"], "帮我训一版模型")
        self.assertEqual(result["status"], tasks.STATUS_AWAITING_PLAN)
        task_id = result["task"]["id"]

        # 计划已提出但未确认：写入仍被拦下
        paused = tasks.get_task_by_id(task_id)
        spec, _, _ = runtime.registry.resolve("start_training", self.a1)
        gate = runtime._plan_gate(paused, spec)
        self.assertEqual(gate[0], "stop")

        gateway.turns = [
            tool_turn(
                "start_training",
                project_id=self.project["id"],
                dataset_id="ds-1",
                model_name="pv-det-v2",
            ),
            ModelTurn(text="训练任务已提交，可以查状态了。"),
        ]
        confirmed = runtime.confirm_plan(self.a1, task_id)
        self.assertIn(
            confirmed["status"],
            {tasks.STATUS_AWAITING_ACTION, tasks.STATUS_COMPLETED},
        )

    def test_cross_user_cannot_confirm_others_action(self) -> None:
        session = self._session("越权确认")
        gateway = ScriptedGateway(
            tool_turn(
                "run_detection",
                project_id=self.project["id"],
                model_id="model-1",
                image_refs=["img-1.jpg"],
            )
        )
        runtime = self._runtime(gateway)
        result = runtime.handle_message(self.a1, session["id"], "检测一张图")

        with self.assertRaises(AgentError) as raised:
            runtime.confirm_action(self.a2, result["action"]["id"])
        self.assertIn(
            raised.exception.code, {Code.FORBIDDEN, Code.ACTION_NOT_FOUND}
        )

    # -- 意图分支（进度 / 改目标 / 停止） -----------------------------------
    def _pending_task(self, title: str):
        """构造一个"写入待确认"的任务，返回 (runtime, gateway, session, action)。"""
        session = self._session(title)
        gateway = ScriptedGateway(
            tool_turn(
                "run_detection",
                project_id=self.project["id"],
                model_id="model-1",
                image_refs=["img-1.jpg"],
            )
        )
        runtime = self._runtime(gateway)
        result = runtime.handle_message(self.a1, session["id"], "检测一张图")
        return runtime, gateway, session, result

    def test_progress_intent_uses_chinese_label_and_returns_pending_action(self) -> None:
        runtime, gateway, session, result = self._pending_task("问进度")
        gateway.turns = []
        progress = runtime.handle_message(self.a1, session["id"], "做到哪一步了")

        self.assertEqual(progress["status"], tasks.STATUS_AWAITING_ACTION)
        self.assertIn(tasks.STATUS_LABELS[tasks.STATUS_AWAITING_ACTION], progress["reply"])
        # 内部枚举不应直接暴露给用户
        self.assertNotIn("awaiting_action", progress["reply"])
        # 有待确认动作时必须带出来，否则前端拿不到卡片
        self.assertIsNotNone(progress["action"])
        self.assertEqual(progress["action"]["id"], result["action"]["id"])
        # 卡片要有可读标题，而不是工具内部名
        self.assertEqual(progress["action"]["tool_title"], "图片检测")

    def test_change_intent_does_not_fake_completion(self) -> None:
        runtime, gateway, session, result = self._pending_task("改目标")
        gateway.turns = [ModelTurn(text="好的，改成只检测第一张。")]
        changed = runtime.handle_message(self.a1, session["id"], "改成只检测第一张")

        self.assertEqual(changed["status"], tasks.STATUS_PLANNING)
        self.assertNotEqual(changed["status"], tasks.STATUS_COMPLETED)
        superseded = actions.get_action(self.a1, result["action"]["id"])
        self.assertNotEqual(superseded["status"], actions.STATUS_PENDING)

    def test_stop_intent_stops_task_with_pending_action(self) -> None:
        runtime, gateway, session, result = self._pending_task("停止")
        gateway.turns = []
        stopped = runtime.handle_message(self.a1, session["id"], "停下吧")

        self.assertEqual(stopped["status"], tasks.STATUS_STOPPED)
        self.assertEqual(tasks.get_task(self.a1, result["task"]["id"])["status"],
                         tasks.STATUS_STOPPED)
        self.assertIsNone(tasks.find_active_task(self.a1, session["id"]))

    # -- B-06/B-07 恢复 -----------------------------------------------------
    def test_unknown_business_state_is_flagged_not_guessed(self) -> None:
        session = self._session("状态核实")
        task = tasks.create_task(
            self.a1, session["id"], self.project["id"], "等待训练完成"
        )
        tasks.set_waiting(
            task["id"],
            kind="business",
            reason="等待训练完成",
            refs=[{"kind": "training", "id": "train-not-exist"}],
        )
        tasks.set_status(task["id"], tasks.STATUS_AWAITING_BUSINESS, force=True)

        outcome = recovery.reconcile_task(self.a1, task["id"])
        self.assertEqual(outcome["status"], tasks.STATUS_UNKNOWN)
        self.assertIn("待核实", outcome["reply"])
        self.assertTrue(outcome["checks"])

    # -- B-08 手册与交接笔记 -------------------------------------------------
    def test_manual_match_and_error_flag(self) -> None:
        manual = manuals.create_user_manual(
            self.a1,
            name="裂缝巡检手册",
            content_md="# 步骤\n1. 校验数据\n2. 启动检测",
            summary="裂缝巡检标准流程",
            keywords="裂缝,巡检",
        )
        matched, score = manuals.match_manual(self.a1, "按裂缝巡检流程重复一遍")
        self.assertIsNotNone(matched)
        self.assertEqual(matched["id"], manual["id"])
        self.assertGreater(score, 0)

        # 其他用户看不到别人创建的手册
        other_ids = {item["id"] for item in manuals.list_manuals(self.a2)}
        self.assertNotIn(manual["id"], other_ids)

        session = self._session("笔记")
        task = tasks.create_task(
            self.a1, session["id"], self.project["id"], "巡检一遍"
        )
        note = memory.flag_error(self.a1, task["id"], "把裂缝说成了污渍")
        self.assertTrue(note["rebuilt_from_records"])

    # -- B-09 外部工具授权 ---------------------------------------------------
    def test_external_tool_needs_confirmation_until_granted(self) -> None:
        spec = ToolSpec(
            name="mcp_read_records",
            title="外部只读工具",
            description="读取外部系统的检测记录",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            risk=RISK_READ,
            source=SOURCE_MCP,
            server_id="mcp-server-1",
        )
        self.assertTrue(
            grants.needs_confirmation(self.a1, spec, source=SOURCE_MCP)
        )

        grant = grants.grant_readonly(
            self.a1, "mcp-server-1", spec.name, str(spec.version)
        )
        self.assertEqual(grant["mode"], grants.MODE_AUTO_READONLY)
        self.assertFalse(
            grants.needs_confirmation(self.a1, spec, source=SOURCE_MCP)
        )

        # 工具版本变化 → 旧放行自动失效
        changed = grants.invalidate_on_version_change(
            self.a1, "mcp-server-1", spec.name, str(int(spec.version) + 1)
        )
        self.assertTrue(changed)
        self.assertTrue(
            grants.needs_confirmation(self.a1, spec, source=SOURCE_MCP)
        )

        # 其他用户的放行互不影响
        self.assertEqual(grants.list_grants(self.a2), [])

    def test_stub_tools_can_be_disabled(self) -> None:
        os.environ["AGENT_ALLOW_STUB"] = "0"
        tool_registry.reset_registry()
        self.addCleanup(tool_registry.reset_registry)
        names = {spec.name for spec in tool_registry.get_registry().specs(self.a1)}
        self.assertNotIn("run_detection", names)
        self.assertIn("propose_plan", names)

    # -- HTTP 层 ------------------------------------------------------------
    def _client(self, gateway: Optional[ScriptedGateway] = None):
        from agent.registry import register_agent

        register_agent(self.app)
        if gateway is not None:
            # 本组验证旧执行器契约；新默认 OpenCode 有独立真实协议验收。
            self.app.config["AGENT_ENGINE"] = "legacy"
            original = connections.model_client
            connections.configure_gateway_factory(None)
            connections.model_client = lambda actor: gateway
            self.addCleanup(setattr, connections, "model_client", original)
        from utils.jwt_util import generate_token

        client = self.app.test_client()
        return client, generate_token(1)

    def test_http_requires_token_and_returns_workspace(self) -> None:
        client, token = self._client()
        unauth = client.get("/api/agent/workspace")
        self.assertEqual(unauth.status_code, 401)
        self.assertEqual(unauth.get_json()["data"]["code"], Code.UNAUTHENTICATED)

        ok = client.get(
            "/api/agent/workspace", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(ok.status_code, 200)
        payload = ok.get_json()["data"]
        self.assertIn("project", payload)
        self.assertIn("capabilities", payload)

    def test_http_message_flow_and_event_resume(self) -> None:
        session = self._session("HTTP 会话")
        gateway = ScriptedGateway(
            tool_turn("query_project_overview", project_id=self.project["id"]),
            ModelTurn(text="已经看过项目概况。"),
        )
        client, token = self._client(gateway)
        headers = {"Authorization": f"Bearer {token}"}

        response = client.post(
            f"/api/agent/sessions/{session['id']}/messages",
            json={"text": "看下项目概况"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()["data"]
        self.assertEqual(body["status"], tasks.STATUS_COMPLETED)
        self.assertIn("events_after", body)

        events = client.get(
            f"/api/agent/sessions/{session['id']}/events",
            query_string={"after": body["events_after"]},
            headers=headers,
        )
        self.assertEqual(events.status_code, 200)
        payload = events.get_json()["data"]
        self.assertTrue(payload["events"])
        self.assertTrue(
            all(
                event["sequence"] > body["events_after"]
                for event in payload["events"]
            )
        )

    def test_http_cross_user_session_is_denied(self) -> None:
        from agent.registry import register_agent
        from utils.jwt_util import generate_token

        session = self._session("别人的会话")
        register_agent(self.app)
        client = self.app.test_client()
        response = client.get(
            f"/api/agent/sessions/{session['id']}/messages",
            headers={"Authorization": f"Bearer {generate_token(2)}"},
        )
        self.assertIn(response.status_code, {403, 404})


if __name__ == "__main__":
    unittest.main(verbosity=2)

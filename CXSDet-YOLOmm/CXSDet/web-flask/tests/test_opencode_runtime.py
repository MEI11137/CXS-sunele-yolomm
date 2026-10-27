"""真实 OpenCode/MCP + 本地脚本模型；不使用用户 Key、图片或付费服务。"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from flask import Flask
from werkzeug.serving import make_server, WSGIRequestHandler

from agent import actions, connections, identity, permissions, sessions, store, tasks
from agent.errors import AgentError
from agent.llm.provider import ProviderCredentials
from agent.opencode_engine import OpenCodeEngine, local_origin
from agent.opencode_runtime import OpenCodeRuntime, opencode_bp, NATIVE_TOOL
from agent.tools.contract import ToolSpec, RISK_READ, RISK_WRITE, OperationResult
from agent.tools.registry import ToolRegistry


class EngineConfigurationTest(unittest.TestCase):
    def engine(self, admin=True):
        actor = identity.ActorContext(1, ("admin",) if admin else ())
        return OpenCodeEngine(actor, {"id": "site-session", "project_id": "project"},
            ProviderCredentials("custom", "https://example.invalid/v1", "test-model", "fixture-secret"),
            Path(tempfile.gettempdir()) / "opencode-config-only", "http://127.0.0.1:5020")

    def test_only_personal_model_is_enabled(self):
        config = self.engine().configuration()
        self.assertEqual(config["enabled_providers"], ["haixun"])
        self.assertEqual(config["model"], "haixun/test-model")
        self.assertNotIn("opencode", config["provider"])

    def test_regular_user_cannot_enable_shell_or_writes(self):
        config = self.engine(False).configuration()
        self.assertEqual(config["permission"]["bash"], "deny")
        self.assertEqual(config["permission"]["edit"], "deny")
        self.assertEqual(config["permission"]["external_directory"], "deny")

    def test_admin_commands_require_each_confirmation(self):
        config = self.engine().configuration()
        self.assertEqual(config["permission"]["bash"], "ask")
        self.assertEqual(config["permission"]["edit"], "ask")

    def test_bridge_origin_cannot_redirect_credentials(self):
        for value in ("https://example.com", "http://127.0.0.1@evil.invalid", "http://127.0.0.1:5020/path"):
            with self.assertRaises(AgentError):
                local_origin(value)

    def test_nested_key_token_password_redaction(self):
        engine = self.engine()
        result = engine.redact({"x": [engine.credentials.api_key, engine.password, engine.token]})
        self.assertEqual(result, {"x": ["[redacted]"] * 3})


class _Provider:
    def __init__(self, path):
        self.path = path
        self.writes = 0

    def list_tools(self, actor):
        return [ToolSpec(name="project_overview", title="查看真实项目", description="读取当前项目",
            parameters={"type": "object", "properties": {"project_id": {"type": "string"}},
                        "required": ["project_id"], "additionalProperties": False}, risk=RISK_READ),
            ToolSpec(name="save_review_note", title="保存复核笔记", description="保存用户确认的测试笔记",
                parameters={"type": "object", "properties": {"text": {"type": "string"}},
                            "required": ["text"], "additionalProperties": False}, risk=RISK_WRITE)]

    def prepare(self, actor, spec, arguments, context):
        return {"preview": {"text": arguments["text"]}}

    def execute(self, actor, spec, arguments, **kwargs):
        if spec.name == "project_overview":
            return OperationResult(outputs={"project": permissions.get_project(actor, arguments["project_id"])})
        self.writes += 1
        self.path.write_text(arguments["text"], encoding="utf-8")
        return OperationResult(outputs={"file": self.path.name})


class _ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        step = self.server.steps.pop(0) if self.server.steps else {"text": "测试模型完成。"}
        delta = {"role": "assistant"}
        if "tool" in step:
            tools = body.get("tools", [])
            name = next((tool["function"]["name"] for tool in tools
                         if tool["function"]["name"] == step["tool"] or
                         tool["function"]["name"].endswith("_" + step["tool"])), None)
            if name is None:
                delta["content"] = "TEST_MISSING_TOOL:" + step["tool"]
            else:
                delta["tool_calls"] = [{"index": 0, "id": "call_" + str(len(self.server.requests)),
                    "type": "function", "function": {"name": name, "arguments": json.dumps(step["args"])}}]
        else:
            delta["content"] = step["text"]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for value, finish in ((delta, None), ({}, "tool_calls" if "tool_calls" in delta else "stop")):
            chunk = {"id": "chatcmpl-local", "object": "chat.completion.chunk", "created": int(time.time()),
                "model": "fixture-model", "choices": [{"index": 0, "delta": value, "finish_reason": finish}]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class _QuietRequestHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        pass


@unittest.skipUnless(os.environ.get("RUN_OPENCODE_INTEGRATION") == "1", "显式启用本机 OpenCode 协议验收")
class OpenCodeProtocolTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="haixun-opencode-", dir=os.environ.get("OPENCODE_TEST_TMP"))
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        env = patch.dict(os.environ, {"AGENT_SECRET_PATH": str(self.root / "fixture.key")})
        env.start()
        self.addCleanup(env.stop)
        self.app = Flask(__name__)
        self.app.config.update(DATABASE=str(self.root / "fixture.sqlite3"), TEMP_DIR=str(self.root), TESTING=True)
        with sqlite3.connect(self.app.config["DATABASE"]) as conn:
            conn.execute("CREATE TABLE user (id INTEGER PRIMARY KEY, username TEXT, role INTEGER, status INTEGER)")
            conn.executemany("INSERT INTO user VALUES (?,?,?,1)", [(1, "fixture-admin", 1), (2, "fixture-user", 0)])
        conn.close()  # sqlite3 上下文只提交/回滚，Windows 下必须显式释放文件句柄。
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        store.reset_schema_cache()
        self.actor = identity.load_actor(1)
        self.other = identity.load_actor(2)
        self.project = permissions.ensure_default_project(self.actor)
        self.session = sessions.create_session(self.actor, self.project["id"], "OpenCode 本地协议测试")
        self.provider = _Provider(self.root / "confirmed-note.txt")
        registry = ToolRegistry()
        registry.add_provider(self.provider)
        self.runtime = OpenCodeRuntime(self.app, registry=registry)
        self.app.extensions["haixun_opencode"] = self.runtime
        self.app.register_blueprint(opencode_bp)
        from agent.routes import agent_bp
        self.app.register_blueprint(agent_bp)
        self.http = make_server("127.0.0.1", 0, self.app, threaded=True, request_handler=_QuietRequestHandler)
        self.app.config["OPENCODE_BRIDGE_ORIGIN"] = "http://127.0.0.1:" + str(self.http.server_port)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.model = ThreadingHTTPServer(("127.0.0.1", 0), _ModelHandler)
        self.model.steps, self.model.requests = [], []
        threading.Thread(target=self.model.serve_forever, daemon=True).start()
        self.addCleanup(self.model.server_close)
        self.addCleanup(self.model.shutdown)
        connections.save_connection(self.actor, provider="custom", api_url=f"http://127.0.0.1:{self.model.server_port}/v1",
                                    model="fixture-model", api_key="fixture-key-not-real")
        self.addCleanup(self.runtime.pool.close)

    def wait(self, predicate, seconds=45):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.15)
        messages = [item["content"] for item in sessions.list_messages(self.session["id"])][-4:]
        self.fail("OpenCode 未达到预期状态；" + repr(messages) + "; 模型请求数=" + str(len(self.model.requests)))

    def idle(self):
        engine = self.runtime.pool.find(self.actor, self.session["id"])
        return engine is not None and not engine.busy

    def test_real_http_chat_and_session_isolation(self):
        self.model.steps = [{"text": "这是独立的普通问答。"}]
        from utils.jwt_util import generate_token
        response = self.app.test_client().post(f"/api/agent/sessions/{self.session['id']}/messages",
            headers={"Authorization": "Bearer " + generate_token(1)}, json={"text": "普通问题也请回答"})
        self.assertEqual(response.status_code, 200, response.get_json())
        result = response.get_json()["data"]
        self.assertEqual(result["engine"], "opencode")
        self.wait(self.idle)
        messages = sessions.list_messages(self.session["id"])
        self.assertEqual(messages[-1]["content"], "这是独立的普通问答。")
        with self.assertRaises(AgentError):
            self.runtime.handle_message(self.other, self.session["id"], "窃取另一用户会话")
        self.assertEqual(len(self.model.requests), 1)

    def test_real_stdio_mcp_reads_business_database(self):
        self.model.steps = [{"tool": "project_overview", "args": {"project_id": self.project["id"]}},
                            {"text": "已读取真实项目。"}]
        result = self.runtime.handle_message(self.actor, self.session["id"], "查看当前项目")
        self.wait(self.idle)
        messages = sessions.list_messages(self.session["id"])
        self.assertTrue(any(item["role"] == "tool" and self.project["id"] in item["content"] for item in messages))
        self.assertEqual(messages[-1]["content"], "已读取真实项目。")
        self.assertGreaterEqual(len(self.model.requests), 2)

    def test_business_write_confirmation_does_not_block_unrelated_chat(self):
        self.model.steps = [{"tool": "save_review_note", "args": {"text": "人工确认的笔记"}},
                            {"text": "等待你确认保存。"}]
        result = self.runtime.handle_message(self.actor, self.session["id"], "保存笔记")
        self.wait(self.idle)
        pending = actions.list_actions(result["task"]["id"], [actions.STATUS_PENDING])
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.provider.writes, 0)
        self.model.steps = [{"text": "可以，先回答另一个问题。"}]
        self.runtime.handle_message(self.actor, self.session["id"], "我还有一个无关的问题")
        self.wait(self.idle)
        self.assertEqual(sessions.list_messages(self.session["id"])[-1]["content"], "可以，先回答另一个问题。")
        self.assertEqual(self.provider.writes, 0)
        self.model.steps = [{"text": "网站已经返回真实保存结果。"}]
        self.runtime.confirm_action(self.actor, pending[0]["id"])
        self.wait(self.idle)
        self.runtime.confirm_action(self.actor, pending[0]["id"])
        self.assertEqual(self.provider.writes, 1)
        self.assertEqual(self.provider.path.read_text(encoding="utf-8"), "人工确认的笔记")

    def test_native_file_write_requires_confirm_and_records_real_result(self):
        engine = self.runtime._engine(self.actor, self.session)
        target = engine.workspace / "native.txt"
        self.model.steps = [{"tool": "write", "args": {"filePath": str(target), "content": "OpenCode 真正写入"}},
                            {"text": "文件已保存。"}]
        result = self.runtime.handle_message(self.actor, self.session["id"], "在工作目录创建 native.txt")
        self.wait(lambda: bool(actions.list_actions(result["task"]["id"], [actions.STATUS_PENDING])))
        pending = actions.list_actions(result["task"]["id"], [actions.STATUS_PENDING])[0]
        self.assertEqual(pending["tool_name"], NATIVE_TOOL)
        self.assertFalse(target.exists())
        self.runtime.confirm_action(self.actor, pending["id"])
        self.wait(self.idle)
        self.assertEqual(target.read_text(encoding="utf-8"), "OpenCode 真正写入")
        self.assertEqual(actions.get_action(self.actor, pending["id"])["status"], actions.STATUS_SUCCEEDED)

    def test_bridge_rejects_missing_token_and_cross_user_resource(self):
        client = self.app.test_client()
        self.assertEqual(client.get("/api/agent/opencode/bridge/tools").status_code, 401)
        engine = self.runtime._engine(self.actor, self.session)
        task = tasks.create_task(self.actor, self.session["id"], self.project["id"], "边界测试")
        engine.busy, engine.task_id = True, task["id"]
        other_project = permissions.ensure_default_project(self.other)
        response = client.post("/api/agent/opencode/bridge/call", headers={"Authorization": "Bearer " + engine.token},
            json={"name": "project_overview", "arguments": {"project_id": other_project["id"], "user_id": 2}})
        self.assertIn("error", response.get_json())
        self.assertEqual(self.provider.writes, 0)

    @unittest.skipUnless(os.name == "nt", "本机 PowerShell 验收")
    def test_native_powershell_requires_confirmation(self):
        self.model.steps = [{"tool": "bash", "args": {
            "command": 'powershell.exe -NoProfile -NonInteractive -Command "Write-Output POWERSHELL_OK"',
            "description": "验证 PowerShell 本机调用"}}, {"text": "PowerShell 已实际运行。"}]
        result = self.runtime.handle_message(self.actor, self.session["id"], "请执行无副作用的 PowerShell 检查")
        self.wait(lambda: bool(actions.list_actions(result["task"]["id"], [actions.STATUS_PENDING])))
        pending = actions.list_actions(result["task"]["id"], [actions.STATUS_PENDING])[0]
        self.runtime.confirm_action(self.actor, pending["id"])
        self.wait(self.idle)
        confirmed = actions.get_action(self.actor, pending["id"])
        self.assertEqual(confirmed["status"], actions.STATUS_SUCCEEDED)
        self.assertIn("POWERSHELL_OK", dumps_for_test(confirmed["result"]))

    def test_reject_cannot_create_native_file(self):
        engine = self.runtime._engine(self.actor, self.session)
        target = engine.workspace / "must-not-exist.txt"
        self.model.steps = [{"tool": "write", "args": {"filePath": str(target), "content": "未批准"}},
                            {"text": "已取消。"}]
        result = self.runtime.handle_message(self.actor, self.session["id"], "创建待拒绝的文件")
        self.wait(lambda: bool(actions.list_actions(result["task"]["id"], [actions.STATUS_PENDING])))
        pending = actions.list_actions(result["task"]["id"], [actions.STATUS_PENDING])[0]
        self.runtime.reject_action(self.actor, pending["id"], "测试拒绝")
        self.wait(self.idle)
        self.assertFalse(target.exists())
        self.assertEqual(actions.get_action(self.actor, pending["id"])["status"], actions.STATUS_REJECTED)
        with self.assertRaises(AgentError):
            self.runtime.confirm_action(self.actor, pending["id"])

    def test_stop_pending_native_write_cannot_be_resumed_by_old_confirmation(self):
        engine = self.runtime._engine(self.actor, self.session)
        target = engine.workspace / "stopped.txt"
        self.model.steps = [{"tool": "write", "args": {"filePath": str(target), "content": "不能写入"}},
                            {"text": "已停止。"}]
        result = self.runtime.handle_message(self.actor, self.session["id"], "创建待停止的文件")
        task_id = result["task"]["id"]
        self.wait(lambda: bool(actions.list_actions(task_id, [actions.STATUS_PENDING])))
        pending = actions.list_actions(task_id, [actions.STATUS_PENDING])[0]
        self.runtime.stop_task(self.actor, task_id)
        engine.worker.join(timeout=5)
        self.assertEqual(tasks.get_task(self.actor, task_id)["status"], tasks.STATUS_STOPPED)
        self.assertFalse(target.exists())
        with self.assertRaises(AgentError):
            self.runtime.confirm_action(self.actor, pending["id"])

    def test_tool_catalogue_change_reconnects_and_revokes_old_bridge_token(self):
        old = self.runtime._engine(self.actor, self.session)
        original_list = self.provider.list_tools
        self.provider.list_tools = lambda actor: original_list(actor) + [ToolSpec(
            name="new_project_reader", title="新工具", description="新增加的只读工具", risk=RISK_READ)]
        new = self.runtime._engine(self.actor, self.session)
        self.assertIsNot(new, old)
        self.assertIsNotNone(old.process.poll())
        client = self.app.test_client()
        self.assertEqual(client.get('/api/agent/opencode/bridge/tools',
            headers={'Authorization': 'Bearer ' + old.token}).status_code, 401)
        response = client.get('/api/agent/opencode/bridge/tools', headers={'Authorization': 'Bearer ' + new.token})
        self.assertIn('new_project_reader', [item['name'] for item in response.get_json()['tools']])

    def test_unknown_business_state_blocks_another_write(self):
        engine = self.runtime._engine(self.actor, self.session)
        task = tasks.create_task(self.actor, self.session['id'], self.project['id'], '核实未知结果')
        tasks.set_status(task['id'], tasks.STATUS_UNKNOWN, force=True)
        engine.busy, engine.task_id = True, task['id']
        response = self.app.test_client().post('/api/agent/opencode/bridge/call',
            headers={'Authorization': 'Bearer ' + engine.token},
            json={'name': 'save_review_note', 'arguments': {'text': '不能重复写入'}})
        self.assertEqual(response.get_json()['status'], tasks.STATUS_UNKNOWN)
        self.assertEqual(self.provider.writes, 0)
        self.assertFalse(actions.list_actions(task['id']))

    @unittest.skipUnless(os.name == 'nt', '本机 PowerShell 验收')
    def test_nonzero_command_exit_is_not_reported_as_success(self):
        self.model.steps = [{'tool': 'bash', 'args': {
            'command': 'powershell.exe -NoProfile -NonInteractive -Command "Write-Output EXPECTED_EXIT_7; exit 7"',
            'description': '验证非零退出码'}}, {'text': '命令失败，需要核实。'}]
        result = self.runtime.handle_message(self.actor, self.session['id'], '验证失败的无副作用命令')
        task_id = result['task']['id']
        self.wait(lambda: bool(actions.list_actions(task_id, [actions.STATUS_PENDING])))
        pending = actions.list_actions(task_id, [actions.STATUS_PENDING])[0]
        self.runtime.confirm_action(self.actor, pending['id'])
        self.wait(self.idle)
        self.assertEqual(actions.get_action(self.actor, pending['id'])['status'], actions.STATUS_FAILED)
        self.assertEqual(tasks.get_task(self.actor, task_id)['status'], tasks.STATUS_UNKNOWN)


def dumps_for_test(value):
    return json.dumps(value, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()

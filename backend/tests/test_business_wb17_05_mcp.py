"""WB17-05：外部 MCP（stdio + http、发现、逐项授权、撤销、超时、脱敏）。

被测的是**真的**协议交互：stdio 用例启动一个真子进程跑 JSON-RPC，
http 用例起一个真的 ``HTTPServer`` 监听 127.0.0.1。没有 mock 传输层，
所以"能不能连上、超时会不会真的断、凭据会不会回显"都是实际行为。

安全边界用"能不能做到"来验，而不是用文案：

* 未授权 → 403 ``MCP_NOT_AUTHORIZED``，即使把权限模式调到 ``full`` 也一样；
* 远端 ``readOnlyHint`` 只是展示标记，**不构成任何授权**：新工具一律 pending，
  授权只能是用户核对后的显式决定（"只读自动放行"已删除）；
* stdio 服务会在评委机器上运行宿主程序：创建/更新/发现/调用一律要求
  管理员角色，member 注册任意宿主程序 → 403 ``MCP_ADMIN_REQUIRED``；
* 凭据只以掩码出现在接口里，错误信息与返回正文里的原值会被替换成 ``***``；
* 模型侧没有"新建服务/改配置"的工具，只有"看"和"调已授权工具"。
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from backend.errors import ApiError
from backend.services import mcp as mcp_service

from backend.tests.support import body, csrf_headers, login
from backend.tests.test_business_wb17_04 import ReviewBase

TOKEN = "s3cret-token-value-9f2c"

FAKE_STDIO = r'''
import json, os, sys, time

TOOLS = [
    {
        "name": "read_note",
        "description": "读取现场备注（只读）",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
        },
    },
    {
        "name": "write_note",
        "description": "写一条备注（会改数据）",
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {"name": "slow_note", "description": "故意很慢", "inputSchema": {"type": "object"}},
]

def send(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()

token = os.environ.get("FAKE_TOKEN", "")

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = message.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-stdio", "version": "1.0"},
        }})
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = message["params"]["name"]
        args = message["params"].get("arguments") or {}
        if name == "read_note":
            send({"jsonrpc": "2.0", "id": message["id"], "result": {"content": [
                {"type": "text", "text": "备注:%s env_len=%d" % (args.get("topic"), len(token))}
            ]}})
        elif name == "write_note":
            send({"jsonrpc": "2.0", "id": message["id"], "result": {"content": [
                {"type": "text", "text": "已写入:%s" % args.get("text")}
            ]}})
        elif name == "slow_note":
            time.sleep(60)
            send({"jsonrpc": "2.0", "id": message["id"], "result": {"content": []}})
        elif name == "boom":
            send({"jsonrpc": "2.0", "id": message["id"], "result": {
                "isError": True,
                "content": [{"type": "text", "text": "失败 token=%s" % token}],
            }})
        elif name == "echo_secret":
            send({"jsonrpc": "2.0", "id": message["id"], "result": {"content": [
                {"type": "text", "text": "token=%s" % token}
            ]}})
        else:
            send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "未知工具"}})
    elif method == "no_such_method":
        send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "bad"}})
'''

_HTTP_TOOLS = [
    {
        "name": "read_note",
        "description": "读取现场备注（只读）",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
        },
    },
    {
        "name": "echo_secret",
        "description": "把收到的 token 原样吐出来（用于验证脱敏）",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {"type": "object"},
    },
    {
        "name": "boom",
        "description": "总是报错，并在错误里带上 token",
        "inputSchema": {"type": "object"},
    },
]


class _FakeHttpChannel:
    """真的 HTTP 服务，记录收到的 auth header，按请求返回 JSON-RPC。"""

    def __init__(self) -> None:
        self.headers: list[dict] = []
        self.bodies: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # 安静
                return

            def do_POST(self):  # noqa: N802 - http.server 约定
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                outer.headers.append({key: value for key, value in self.headers.items()})
                try:
                    message = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    message = {}
                outer.bodies.append(message)
                method = message.get("method")
                token = (self.headers.get("X-Test-Token") or "")
                if method == "initialize":
                    payload = {"jsonrpc": "2.0", "id": message.get("id"), "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-http", "version": "1.0"},
                    }}
                elif method == "notifications/initialized":
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                elif method == "tools/list":
                    payload = {"jsonrpc": "2.0", "id": message.get("id"), "result": {"tools": _HTTP_TOOLS}}
                elif method == "tools/call":
                    name = message["params"]["name"]
                    if name == "read_note":
                        result = {"content": [{"type": "text", "text": "http:%s" % (
                            message["params"].get("arguments") or {}).get("topic")}]}
                    elif name == "echo_secret":
                        result = {"content": [{"type": "text", "text": "token=%s" % token}]}
                    else:
                        result = {"isError": True, "content": [
                            {"type": "text", "text": "失败 token=%s" % token}]}
                    payload = {"jsonrpc": "2.0", "id": message.get("id"), "result": result}
                else:
                    payload = {"jsonrpc": "2.0", "id": message.get("id"),
                               "error": {"code": -32601, "message": "bad"}}
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class McpBase(ReviewBase):
    def setUp(self):
        super().setUp()
        self.server_script = Path(self.harness.data_dir) / "fake_stdio_mcp.py"
        self.server_script.write_text(FAKE_STDIO, encoding="utf-8")
        self._http: _FakeHttpChannel | None = None

    def tearDown(self):
        if self._http is not None:
            self._http.close()
        super().tearDown()

    # ------------------------------------------------------------------ 帮助

    def http_channel(self) -> _FakeHttpChannel:
        if self._http is None:
            self._http = _FakeHttpChannel()
        return self._http

    def create_server(self, **kwargs):
        payload = {
            "name": kwargs.pop("name", f"本地服务 {secrets.token_hex(2)}"),
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(self.server_script)],
            "enabled": True,
        }
        payload.update(kwargs)
        response = self.client.post(
            f"/api/projects/{self.project['id']}/mcp",
            json=payload,
            headers=csrf_headers(self.client),
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        return body(response)["data"]["server"]

    def discover(self, server_id: str):
        response = self.client.post(
            f"/api/projects/{self.project['id']}/mcp/{server_id}/discover",
            headers=csrf_headers(self.client),
        )
        return response

    def authorize(self, server_id: str, tool: str, decision: str):
        return self.client.post(
            f"/api/projects/{self.project['id']}/mcp/{server_id}/authorize",
            json={"tool": tool, "decision": decision},
            headers=csrf_headers(self.client),
        )

    def call(self, server_id: str, tool: str, arguments=None, request_key=None):
        payload = {"tool": tool, "arguments": arguments or {}}
        if request_key:
            payload["request_key"] = request_key
        return self.client.post(
            f"/api/projects/{self.project['id']}/mcp/{server_id}/call",
            json=payload,
            headers=csrf_headers(self.client),
        )

    def tool_map(self, server: dict) -> dict:
        return {item["name"]: item for item in server["tools"]}


# --------------------------------------------------------------------------- 校验


class McpConfigTest(McpBase):
    def test_command_and_url_boundaries(self):
        conn_bad = self.client.post(
            f"/api/projects/{self.project['id']}/mcp",
            json={"name": "拼串", "transport": "stdio", "command": "python -c print(1)", "args": []},
            headers=csrf_headers(self.client),
        )
        self.assertGreaterEqual(conn_bad.status_code, 400)

        shell_chars = self.client.post(
            f"/api/projects/{self.project['id']}/mcp",
            json={"name": "管道", "transport": "stdio", "command": "cat | tee x", "args": []},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(shell_chars.status_code, 400, shell_chars.get_data(as_text=True))

        missing = self.client.post(
            f"/api/projects/{self.project['id']}/mcp",
            json={"name": "不存在", "transport": "stdio", "command": "no-such-binary-xyz", "args": []},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(missing.status_code, 404, missing.get_data(as_text=True))
        self.assertEqual(body(missing)["error"], "MCP_COMMAND_NOT_FOUND")

        insecure = self.client.post(
            f"/api/projects/{self.project['id']}/mcp",
            json={"name": "明文", "transport": "http", "url": "http://example.com/mcp"},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(insecure.status_code, 400, insecure.get_data(as_text=True))
        self.assertEqual(body(insecure)["error"], "MCP_INSECURE_URL")

        userinfo = self.client.post(
            f"/api/projects/{self.project['id']}/mcp",
            json={"name": "带账号", "transport": "http", "url": "http://user:pass@127.0.0.1/mcp"},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(userinfo.status_code, 400, userinfo.get_data(as_text=True))
        self.assertEqual(body(userinfo)["error"], "MCP_BAD_URL")

        local = self.client.post(
            f"/api/projects/{self.project['id']}/mcp",
            json={"name": "本机", "transport": "http", "url": "http://127.0.0.1:9/mcp"},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(local.status_code, 200, local.get_data(as_text=True))

    def test_same_name_is_rejected_in_one_project(self):
        self.create_server(name="重名服务")
        clash = self.client.post(
            f"/api/projects/{self.project['id']}/mcp",
            json={"name": "重名服务", "transport": "stdio", "command": sys.executable},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(clash.status_code, 409, clash.get_data(as_text=True))
        self.assertEqual(body(clash)["error"], "MCP_NAME_TAKEN")

    def test_secrets_are_masked_and_never_returned(self):
        server = self.create_server(env={"FAKE_TOKEN": TOKEN})
        listed = body(self.client.get(f"/api/projects/{self.project['id']}/mcp"))["data"]
        self.assertNotIn(TOKEN, json.dumps(listed, ensure_ascii=False))
        item = listed["servers"][0]
        self.assertEqual(item["secret_keys"], ["FAKE_TOKEN"])
        self.assertNotEqual(item["secret_hints"]["FAKE_TOKEN"], TOKEN)

        conn = self.conn()
        try:
            row = conn.execute("SELECT secrets_cipher FROM mcp_servers WHERE id = ?", (server["id"],)).fetchone()
        finally:
            conn.close()
        self.assertTrue(row["secrets_cipher"].startswith("gcm1."))
        self.assertNotIn(TOKEN, row["secrets_cipher"])


# --------------------------------------------------------------------------- 发现与授权


class McpDiscoveryTest(McpBase):
    def test_discovery_defaults_to_pending_and_records_status(self):
        server = self.create_server(env={"FAKE_TOKEN": TOKEN})
        response = self.discover(server["id"])
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = body(response)["data"]
        self.assertTrue(data["ok"], data)
        tools = self.tool_map(data["server"])
        self.assertEqual(sorted(tools), ["read_note", "slow_note", "write_note"])
        self.assertTrue(all(item["authorization"] == "pending" for item in tools.values()))
        self.assertEqual(data["server"]["status"], "connected")
        self.assertEqual(sorted(data["new_tools"]), ["read_note", "slow_note", "write_note"])
        self.assertTrue(tools["read_note"]["read_only"])
        self.assertFalse(tools["write_note"]["read_only"])

    def test_read_only_hint_is_a_label_not_authorization(self):
        """远端 readOnlyHint 不构成授权：即使带着旧开关字段请求，新工具也一律 pending。"""
        # 旧客户端可能还发 auto_allow_read_only 字段：后端必须忽略它，而不是照做。
        server = self.create_server(auto_allow_read_only=True)
        data = body(self.discover(server["id"]))["data"]
        tools = self.tool_map(data["server"])
        self.assertTrue(all(item["authorization"] == "pending" for item in tools.values()))
        # read_only 仍如实记录（页面展示用），但授权必须显式：
        self.assertTrue(tools["read_note"]["read_only"])
        self.assertFalse(tools["write_note"]["read_only"])
        # 用户核对后的具体授权仍然有效：显式 allow 一个工具。
        self.assertEqual(self.authorize(server["id"], "read_note", "allow").status_code, 200)
        tools = self.tool_map(body(self.discover(server["id"]))["data"]["server"])
        self.assertEqual(tools["read_note"]["authorization"], "allowed")
        self.assertEqual(tools["write_note"]["authorization"], "pending")

    def test_discovery_drops_tools_that_disappeared(self):
        server = self.create_server()
        self.discover(server["id"])
        conn = self.conn()
        try:
            conn.execute(
                "INSERT INTO mcp_tools (server_id, name, authorization, discovered_at)"
                " VALUES (?, 'ghost_tool', 'allowed', '2026-11-05T00:00:00Z')",
                (server["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        data = body(self.discover(server["id"]))["data"]
        self.assertIn("ghost_tool", data["dropped_tools"])
        self.assertNotIn("ghost_tool", self.tool_map(data["server"]))

    def test_failure_is_reported_without_retry(self):
        server = self.create_server()
        # 创建时已校验，这里直接改库模拟"配置后来失效"
        conn = self.conn()
        try:
            conn.execute("UPDATE mcp_servers SET command = ? WHERE id = ?", ("C:/nope/none.exe", server["id"]))
            conn.commit()
        finally:
            conn.close()
        data = body(self.discover(server["id"]))["data"]
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"]["code"], "MCP_SPAWN_FAILED")
        self.assertEqual(data["server"]["status"], "error")
        self.assertIn("不会自动重试", data["note"])


# --------------------------------------------------------------------------- 角色边界


class McpAdminBoundaryTest(McpBase):
    """普通评委（member）的服务器边界：本机 stdio 宿主程序只有管理员能碰。"""

    def _member_project(self):
        bob = self.harness.create_user("bob", role="member")
        bob_root = self.harness.make_dir("bob-root")
        self.harness.add_root(bob_root, grant_to=["bob"])
        bob_project = self.harness.create_project(bob["id"], "评委项目")
        self.harness.bind_project(bob_project["id"], bob_root, "bob")
        return bob, bob_project

    def test_member_cannot_register_stdio_host_program(self):
        """QA 反例修复：member 注册未经审核的宿主程序 → 403，而不是 200。"""
        bob, project = self._member_project()
        login(self.client, "bob")
        response = self.client.post(
            f"/api/projects/{project['id']}/mcp",
            json={
                "name": "评委自装",
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(self.server_script)],
                "enabled": True,
            },
            headers=csrf_headers(self.client),
        )
        self.assertEqual(response.status_code, 403, response.get_data(as_text=True))
        self.assertEqual(body(response)["error"], "MCP_ADMIN_REQUIRED")
        # 403 拒绝发生在解析命令/落库之前：库里没有留下任何行
        conn = self.conn()
        try:
            count = conn.execute(
                "SELECT count(*) FROM mcp_servers WHERE owner_id = ?", (int(bob["id"]),)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_member_can_still_register_http_service(self):
        """http(s) 服务不在本机跑程序：member 自助配置不因此被一刀切。"""
        _bob, project = self._member_project()
        login(self.client, "bob")
        response = self.client.post(
            f"/api/projects/{project['id']}/mcp",
            json={"name": "云服务", "transport": "http", "url": "http://127.0.0.1:9/mcp"},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def test_demoted_admin_cannot_update_discover_or_call_stdio(self):
        """创建时是 admin、后来被降级：更新/发现/调用一律 403——发现阶段会运行程序。"""
        server = self.create_server()
        conn = self.conn()
        try:
            conn.execute("UPDATE users SET role = 'member' WHERE id = ?", (int(self.alice["id"]),))
            conn.commit()
        finally:
            conn.close()
        response = self.discover(server["id"])
        self.assertEqual(response.status_code, 403, response.get_data(as_text=True))
        self.assertEqual(body(response)["error"], "MCP_ADMIN_REQUIRED")
        # 服务状态没有被改成 error（拒绝 ≠ 连接失败）
        listed = body(self.client.get(f"/api/projects/{self.project['id']}/mcp"))["data"]
        self.assertEqual(listed["servers"][0]["status"], "unknown")

        patch = self.client.patch(
            f"/api/projects/{self.project['id']}/mcp/{server['id']}",
            json={"enabled": False},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(patch.status_code, 403, patch.get_data(as_text=True))

        call = self.call(server["id"], "read_note", {"topic": "x"})
        self.assertEqual(call.status_code, 403, call.get_data(as_text=True))


# --------------------------------------------------------------------------- 调用


class McpCallTest(McpBase):
    def test_call_requires_per_tool_authorization(self):
        server = self.create_server(env={"FAKE_TOKEN": TOKEN})
        self.discover(server["id"])
        denied = self.call(server["id"], "read_note", {"topic": "热斑"})
        self.assertEqual(denied.status_code, 403, denied.get_data(as_text=True))
        self.assertEqual(body(denied)["error"], "MCP_NOT_AUTHORIZED")

        self.assertEqual(self.authorize(server["id"], "read_note", "allow").status_code, 200)
        allowed = self.call(server["id"], "read_note", {"topic": "热斑"})
        self.assertEqual(allowed.status_code, 200, allowed.get_data(as_text=True))
        data = body(allowed)["data"]
        self.assertTrue(data["ok"])
        self.assertIn("备注:热斑", data["text"])
        # env 真的传给了子进程，但值不会被回显
        self.assertIn(f"env_len={len(TOKEN)}", data["text"])
        self.assertNotIn(TOKEN, data["text"])

    def test_full_mode_does_not_relax_external_authorization(self):
        from backend.agent import identity as agent_identity
        from backend.agent import store as agent_store

        conn = self.conn()
        try:
            agent_store.save_prefs(conn, self.alice["id"], mode="full", model=None)
            conn.commit()
            self.assertEqual(agent_identity.effective_mode(conn, self.alice["id"]), "full")
        finally:
            conn.close()

        server = self.create_server()
        self.discover(server["id"])
        denied = self.call(server["id"], "read_note", {"topic": "x"})
        self.assertEqual(denied.status_code, 403, denied.get_data(as_text=True))
        self.assertEqual(body(denied)["error"], "MCP_NOT_AUTHORIZED")

    def test_revoke_stops_further_calls(self):
        server = self.create_server()
        self.discover(server["id"])
        self.authorize(server["id"], "read_note", "allow")
        self.assertEqual(self.call(server["id"], "read_note", {"topic": "a"}).status_code, 200)

        revoked = self.authorize(server["id"], "read_note", "revoke")
        self.assertEqual(revoked.status_code, 200, revoked.get_data(as_text=True))
        self.assertEqual(body(revoked)["data"]["tool"]["authorization"], "pending")
        blocked = self.call(server["id"], "read_note", {"topic": "a"})
        self.assertEqual(blocked.status_code, 403, blocked.get_data(as_text=True))

    def test_request_key_replays_without_calling_again(self):
        server = self.create_server()
        self.discover(server["id"])
        self.authorize(server["id"], "read_note", "allow")
        first = self.call(server["id"], "read_note", {"topic": "一次性"}, request_key="mcp-call-1")
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        second = self.call(server["id"], "read_note", {"topic": "一次性"}, request_key="mcp-call-1")
        self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
        data = body(second)["data"]
        self.assertTrue(data["replayed"])
        self.assertIn("没有再次请求外部服务", data["note"])

        conn = self.conn()
        try:
            rows = conn.execute(
                "SELECT COUNT(*) AS total FROM mcp_calls WHERE server_id = ?", (server["id"],)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(int(rows["total"]), 1)

    def test_arguments_are_validated_against_the_declared_schema(self):
        server = self.create_server()
        self.discover(server["id"])
        self.authorize(server["id"], "read_note", "allow")
        missing = self.call(server["id"], "read_note", {})
        self.assertEqual(missing.status_code, 400, missing.get_data(as_text=True))
        self.assertEqual(body(missing)["error"], "MCP_ARGS_MISSING")
        wrong = self.call(server["id"], "read_note", {"topic": 12})
        self.assertEqual(wrong.status_code, 400, wrong.get_data(as_text=True))
        self.assertEqual(body(wrong)["error"], "MCP_ARGS_TYPE")

    def test_secrets_are_redacted_from_results_and_errors(self):
        server = self.create_server(env={"FAKE_TOKEN": TOKEN})
        self.discover(server["id"])
        conn = self.conn()
        try:
            # 假服务端会额外暴露两个"故意泄露"的工具：手工登记以便授权
            for name in ("echo_secret", "boom"):
                conn.execute(
                    "INSERT INTO mcp_tools (server_id, name, authorization, discovered_at)"
                    " VALUES (?, ?, 'allowed', '2026-11-05T00:00:00Z')",
                    (server["id"], name),
                )
            conn.commit()
        finally:
            conn.close()
        echoed = body(self.call(server["id"], "echo_secret", {}))["data"]
        self.assertIn("token=", echoed["text"])
        self.assertNotIn(TOKEN, echoed["text"])
        self.assertIn("***", echoed["text"])

        failed = body(self.call(server["id"], "boom", {}))["data"]
        self.assertFalse(failed["ok"])
        self.assertNotIn(TOKEN, json.dumps(failed, ensure_ascii=False))

    def test_timeout_is_bounded_and_reported(self):
        server = self.create_server()
        self.discover(server["id"])
        self.authorize(server["id"], "slow_note", "allow")
        started = time.time()
        with mock.patch.object(mcp_service, "CALL_TIMEOUT", 1.0):
            data = body(self.call(server["id"], "slow_note", {}))["data"]
        elapsed = time.time() - started
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"]["code"], "MCP_TIMEOUT")
        self.assertLess(elapsed, 10.0, "超时必须真的中断，不能干等")

    def test_disabled_server_cannot_be_called(self):
        server = self.create_server()
        self.discover(server["id"])
        self.authorize(server["id"], "read_note", "allow")
        self.client.patch(
            f"/api/projects/{self.project['id']}/mcp/{server['id']}",
            json={"enabled": False},
            headers=csrf_headers(self.client),
        )
        blocked = self.call(server["id"], "read_note", {"topic": "a"})
        self.assertEqual(blocked.status_code, 409, blocked.get_data(as_text=True))
        self.assertEqual(body(blocked)["error"], "MCP_DISABLED")

    def test_calls_are_audited(self):
        server = self.create_server()
        self.discover(server["id"])
        self.authorize(server["id"], "read_note", "allow")
        self.call(server["id"], "read_note", {"topic": "审计"})
        listed = body(
            self.client.get(f"/api/projects/{self.project['id']}/mcp/{server['id']}/calls")
        )["data"]
        self.assertEqual(len(listed["calls"]), 1)
        self.assertTrue(listed["calls"][0]["ok"])
        self.assertEqual(listed["calls"][0]["tool"], "read_note")


# --------------------------------------------------------------------------- HTTP 传输


class McpHttpTest(McpBase):
    def test_http_transport_round_trip_and_header_secret(self):
        channel = self.http_channel()
        server = self.create_server(
            name="网络服务",
            transport="http",
            command="",
            args=[],
            url=channel.url,
            headers={"X-Test-Token": TOKEN},
        )
        self.assertEqual(server["transport"], "http")
        self.assertNotIn(TOKEN, json.dumps(server, ensure_ascii=False))

        data = body(self.discover(server["id"]))["data"]
        self.assertTrue(data["ok"], data)
        self.assertEqual(sorted(self.tool_map(data["server"])), ["boom", "echo_secret", "read_note"])
        # header 真的发出去了（服务端看到了原值），但接口里只有掩码
        self.assertEqual(channel.headers[-1].get("X-Test-Token"), TOKEN)

        self.authorize(server["id"], "read_note", "allow")
        called = body(self.call(server["id"], "read_note", {"topic": "网络"}))["data"]
        self.assertTrue(called["ok"], called)
        self.assertIn("http:网络", called["text"])

    def test_http_error_is_reported_honestly(self):
        server = self.client.post(
            f"/api/projects/{self.project['id']}/mcp",
            json={"name": "连不上", "transport": "http", "url": "http://127.0.0.1:9/mcp", "enabled": True},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(server.status_code, 200, server.get_data(as_text=True))
        server_id = body(server)["data"]["server"]["id"]
        data = body(self.discover(server_id))["data"]
        self.assertFalse(data["ok"])
        self.assertIn(data["error"]["code"], {"MCP_UNREACHABLE", "MCP_HTTP_STATUS"})
        self.assertEqual(data["server"]["status"], "error")


# --------------------------------------------------------------------------- 隔离与工具层


class McpScopeTest(McpBase):
    def test_other_account_and_project_cannot_touch_the_server(self):
        server = self.create_server()
        other_project = self.harness.create_project(self.alice["id"], "另一个项目")
        self.harness.bind_project(other_project["id"], self.root, "alice")
        crossed = self.client.post(
            f"/api/projects/{other_project['id']}/mcp/{server['id']}/discover",
            headers=csrf_headers(self.client),
        )
        self.assertEqual(crossed.status_code, 404, crossed.get_data(as_text=True))

        bob = self.harness.create_user("bob")
        bob_root = self.harness.make_dir("bob-root")
        self.harness.add_root(bob_root, grant_to=["bob"])
        bob_project = self.harness.create_project(bob["id"], "bob 的项目")
        self.harness.bind_project(bob_project["id"], bob_root, "bob")
        bob_client = self.harness.client()
        login(bob_client, "bob")
        stolen = bob_client.post(
            f"/api/projects/{bob_project['id']}/mcp/{server['id']}/discover",
            headers=csrf_headers(bob_client),
        )
        self.assertEqual(stolen.status_code, 404, stolen.get_data(as_text=True))
        bob_list = body(bob_client.get(f"/api/projects/{bob_project['id']}/mcp"))["data"]
        self.assertEqual(bob_list["servers"], [])

    def test_delete_removes_tools_and_blocks_calls(self):
        server = self.create_server()
        self.discover(server["id"])
        self.authorize(server["id"], "read_note", "allow")
        removed = self.client.delete(
            f"/api/projects/{self.project['id']}/mcp/{server['id']}",
            headers=csrf_headers(self.client),
        )
        self.assertEqual(removed.status_code, 200, removed.get_data(as_text=True))
        listed = body(self.client.get(f"/api/projects/{self.project['id']}/mcp"))["data"]
        self.assertEqual(listed["servers"], [])
        blocked = self.call(server["id"], "read_note", {"topic": "a"})
        self.assertEqual(blocked.status_code, 404, blocked.get_data(as_text=True))


class McpToolTest(McpBase):
    def _ctx(self, conn, args: dict):
        from backend.agent import identity as agent_identity
        from backend.agent.context import Actor
        from backend.agent.tools import ToolContext

        connection = agent_identity.connection_for(conn, self.alice["id"])
        actor = Actor(
            user_id=int(self.alice["id"]),
            username=str(self.alice["username"]),
            project_id=str(self.project["id"]),
            project_name=str(self.project["name"]),
            session_id=str(self.session["id"]),
            turn_id=f"trn_{secrets.token_hex(8)}",
            mode=agent_identity.effective_mode(conn, self.alice["id"]),
            root_path=self.root,
            connection_fingerprint=agent_identity.connection_fingerprint(connection),
            provider=str(connection["provider"]),
            model=agent_identity.effective_model(conn, self.alice["id"], connection),
        )
        return ToolContext(conn=conn, actor=actor, args=args, emit=lambda *_args: None, jobs={})

    def test_agent_tools_can_list_and_call_but_not_configure(self):
        from backend.agent import tools as agent_tools
        from backend.agent.context import ToolError

        self.assertFalse([name for name in agent_tools.TOOLS if "mcp_" in name and "server" in name and name
                          not in {"hx_mcp_servers", "hx_mcp_call"}])
        server = self.create_server(env={"FAKE_TOKEN": TOKEN})
        self.discover(server["id"])
        conn = self.conn()
        try:
            listed = agent_tools.TOOLS["hx_mcp_servers"].handler(self._ctx(conn, {}))
            self.assertEqual(len(listed["servers"]), 1)
            self.assertNotIn(TOKEN, json.dumps(listed, ensure_ascii=False))
            with self.assertRaises(ToolError) as blocked:
                agent_tools.TOOLS["hx_mcp_call"].handler(
                    self._ctx(conn, {"server_id": server["id"], "tool": "read_note", "arguments": {"topic": "t"}})
                )
            self.assertEqual(blocked.exception.code, "MCP_NOT_AUTHORIZED")
            self.authorize(server["id"], "read_note", "allow")
            called = agent_tools.TOOLS["hx_mcp_call"].handler(
                self._ctx(conn, {"server_id": server["id"], "tool": "read_note", "arguments": {"topic": "t"}})
            )
            self.assertTrue(called["ok"])
            self.assertIn("备注:t", called["text"])
        finally:
            conn.close()

    def test_risk_level_follows_the_tool_declaration(self):
        from backend.agent import tools as agent_tools

        server = self.create_server()
        self.discover(server["id"])
        self.authorize(server["id"], "read_note", "allow")
        conn = self.conn()
        try:
            read_ctx = self._ctx(conn, {"server_id": server["id"], "tool": "read_note"})
            write_ctx = self._ctx(conn, {"server_id": server["id"], "tool": "write_note"})
            spec = agent_tools.TOOLS["hx_mcp_call"]
            self.assertEqual(spec.effective_risk(read_ctx), "read")
            self.assertEqual(spec.effective_risk(write_ctx), "write")
        finally:
            conn.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

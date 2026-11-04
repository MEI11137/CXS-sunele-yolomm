"""个人模型连接：加密、掩码、隔离、能力检查与失效。

所有探测都用注入的假传输，**不会发起真实网络请求，也不产生任何费用**。
"""

from __future__ import annotations

import base64
import sqlite3
import struct
import unittest
import zlib
from unittest import mock

from backend import crypto
from backend.services import connections
from backend.services.connections import ProbeResponse
from backend.tests.support import Harness, api, body, login

API_KEY = "sk-test-1234567890abcdef"


def _reply(content: str = "ok", *, tool_calls: bool = False) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "hx_probe", "arguments": "{}"},
            }
        ]
    return {"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}


def _color_of(payload: dict) -> str:
    """从探测请求里解出纯色 PNG 的颜色，模拟“真的看到了图”的模型。"""
    content = payload["messages"][0]["content"]
    url = next(item["image_url"]["url"] for item in content if item["type"] == "image_url")
    data = base64.b64decode(url.split(",", 1)[1])
    pos, idat = 8, b""
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        tag = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if tag == b"IDAT":
            idat += body
        pos += 12 + length
    raw = zlib.decompress(idat)
    rgb = tuple(raw[1:4])
    for name, expected in connections.COLOR_PALETTE.items():
        if tuple(expected) == rgb:
            return name
    return "不知道"


class FakeTransport:
    """按顺序返回预置响应，并记录每次调用，用于证明没有重试。"""

    def __init__(self, responses: list[ProbeResponse]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
        if not self.responses:
            return ProbeResponse(500, None, "unexpected extra call", 1)
        item = self.responses.pop(0)
        return item(payload) if callable(item) else item


class ConnectionCrudTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.alice = self.harness.create_user("alice")
        self.bob = self.harness.create_user("bob")
        self.client = self.harness.client()
        login(self.client, "alice")

    def tearDown(self):
        self.harness.close()

    def _save(self, **overrides):
        payload = {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "display_name": "我的 DeepSeek",
            "api_key": API_KEY,
        }
        payload.update(overrides)
        return api(self.client, "PUT", "/api/connections/me", payload)

    def test_providers_do_not_claim_capabilities(self):
        providers = body(api(self.client, "GET", "/api/connections/providers"))["data"]["providers"]
        deepseek = next(item for item in providers if item["id"] == "deepseek")
        self.assertEqual(deepseek["base_url"], "https://api.deepseek.com/v1")
        self.assertIn("实测", deepseek["note"])
        # 预设里不能出现“多模态/支持视觉”这类未经检查的断言。
        self.assertNotIn("多模态", str(providers))

    def test_create_masks_key_and_never_returns_plaintext(self):
        response = self._save()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        raw = response.get_data(as_text=True)
        self.assertNotIn(API_KEY, raw)
        data = body(response)["data"]["connection"]
        self.assertNotIn("api_key", data)
        self.assertNotIn("key_cipher", data)
        self.assertEqual(data["key_hint"], "sk-****cdef")
        self.assertEqual(data["model"], "deepseek-chat")
        self.assertEqual(data["display_name"], "我的 DeepSeek")
        # 从未检查过 → 能力结论必须视为过期，不能默认放行。
        self.assertTrue(data["capabilities_stale"])

    def test_key_is_encrypted_at_rest(self):
        self._save()
        connection = self.harness.conn()
        try:
            row = connection.execute("SELECT * FROM connections WHERE user_id = ?", (self.alice["id"],)).fetchone()
        finally:
            connection.close()
        self.assertNotIn(API_KEY, str(dict(row)))
        self.assertTrue(row["key_cipher"].startswith("gcm1."))
        key = self.harness.app.config["HX17_CONNECTION_KEY"]
        self.assertEqual(
            crypto.decrypt_text(row["key_cipher"], key, aad=f"hx17.connection.user:{self.alice['id']}".encode()),
            API_KEY,
        )
        # 主密钥与数据库分文件存放。
        self.assertTrue((self.harness.settings.secret_dir / "connection.key").is_file())
        self.assertNotEqual(
            (self.harness.settings.secret_dir / "connection.key").resolve(),
            (self.harness.settings.db_path).resolve(),
        )

    def test_database_file_does_not_contain_plaintext_key(self):
        self._save()
        # WAL 模式下新数据可能还在 -wal 文件里，所以整个数据目录一起查。
        for path in self.harness.data_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(API_KEY.encode(), path.read_bytes(), str(path))

    def test_update_without_key_keeps_existing(self):
        first = body(self._save())["data"]["connection"]
        response = self._save(api_key="", display_name="改名")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        second = body(response)["data"]["connection"]
        self.assertEqual(second["key_hint"], first["key_hint"])
        self.assertEqual(second["display_name"], "改名")

    def test_first_save_requires_key(self):
        response = api(
            self.client,
            "PUT",
            "/api/connections/me",
            {"provider": "custom", "base_url": "https://example.com/v1", "model": "m1"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "BAD_API_KEY")

    def test_invalid_inputs_rejected(self):
        self.assertEqual(self._save(base_url="api.deepseek.com").status_code, 400)
        self.assertEqual(self._save(model="").status_code, 400)
        self.assertEqual(self._save(api_key="short").status_code, 400)
        self.assertEqual(self._save(base_url="https://user:pw@example.com/v1").status_code, 400)

    def test_full_endpoint_is_normalized(self):
        data = body(self._save(base_url="https://api.deepseek.com/v1/chat/completions"))["data"]["connection"]
        self.assertEqual(data["base_url"], "https://api.deepseek.com/v1")

    def test_connection_is_per_user(self):
        self._save()
        bob = self.harness.client()
        login(bob, "bob")
        self.assertIsNone(body(api(bob, "GET", "/api/connections/me"))["data"]["connection"])
        self.assertEqual(api(bob, "DELETE", "/api/connections/me").status_code, 200)
        # bob 的删除不影响 alice。
        self.assertIsNotNone(body(api(self.client, "GET", "/api/connections/me"))["data"]["connection"])

    def test_admin_cannot_read_other_users_connection(self):
        self._save()
        admin = self.harness.create_user("admin", role="admin")
        admin_client = self.harness.client()
        login(admin_client, "admin")
        self.assertIsNone(body(api(admin_client, "GET", "/api/connections/me"))["data"]["connection"])
        _ = admin

    def test_delete(self):
        self._save()
        self.assertEqual(api(self.client, "DELETE", "/api/connections/me").status_code, 200)
        self.assertIsNone(body(api(self.client, "GET", "/api/connections/me"))["data"]["connection"])

    def test_check_without_connection_is_404(self):
        response = api(self.client, "POST", "/api/connections/me/check", {})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "CONNECTION_NOT_FOUND")


class ConnectionCheckTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.alice = self.harness.create_user("alice")
        self.client = self.harness.client()
        login(self.client, "alice")
        self._save()

    def tearDown(self):
        self.harness.close()

    def _save(self, **overrides):
        payload = {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "api_key": API_KEY,
        }
        payload.update(overrides)
        response = api(self.client, "PUT", "/api/connections/me", payload)
        assert response.status_code == 200, response.get_data(as_text=True)

    def _check(self, transport: FakeTransport):
        with mock.patch("backend.services.connections.http_transport", transport):
            return api(self.client, "POST", "/api/connections/me/check", {})

    def test_all_capabilities_true_are_recorded_with_evidence(self):
        transport = FakeTransport(
            [
                ProbeResponse(200, _reply("ok"), "", 12),
                lambda payload: ProbeResponse(200, _reply(_color_of(payload)), "", 20),
                ProbeResponse(200, _reply("", tool_calls=True), "", 18),
            ]
        )
        response = self._check(transport)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = body(response)["data"]["connection"]
        capabilities = data["capabilities"]
        for name in ("auth", "chat", "vision", "tools"):
            self.assertTrue(capabilities[name], name)
        self.assertFalse(data["capabilities_stale"])
        self.assertTrue(capabilities["evidence"]["vision"]["status"] == 200)
        self.assertTrue(capabilities["evidence"]["tools"]["tool_calls"])
        self.assertEqual(len(transport.calls), 3)
        # 认证头是 Bearer，但响应里不会带出 Key。
        self.assertTrue(transport.calls[0]["headers"]["Authorization"].startswith("Bearer "))
        self.assertNotIn(API_KEY, response.get_data(as_text=True))

    def test_auth_failure_stops_after_one_request(self):
        transport = FakeTransport([ProbeResponse(401, {"error": {"message": "invalid key"}}, "", 8)])
        response = self._check(transport)
        self.assertEqual(response.status_code, 200)
        data = body(response)["data"]["connection"]
        self.assertFalse(data["capabilities"]["auth"])
        self.assertFalse(data["capabilities"]["chat"])
        self.assertFalse(data["capabilities"]["vision"])
        self.assertIn("认证失败", data["summary"])
        # 不重试：只发了一次请求。
        self.assertEqual(len(transport.calls), 1)

    def test_network_failure_is_reported_not_raised(self):
        transport = FakeTransport([ProbeResponse(0, None, "ConnectTimeout: timed out", 5000)])
        data = body(self._check(transport))["data"]["connection"]
        self.assertFalse(data["capabilities"]["auth"])
        self.assertIn("网络不可达", data["summary"])

    def test_chat_failure_skips_vision_and_tools(self):
        transport = FakeTransport([ProbeResponse(400, {"error": {"message": "model not found"}}, "", 10)])
        data = body(self._check(transport))["data"]["connection"]
        # 400 没有正向证据，不能算“凭据已被接受”。
        self.assertFalse(data["capabilities"]["auth"])
        self.assertEqual(data["capabilities"]["auth_state"], "unverified")
        self.assertFalse(data["capabilities"]["chat"])
        self.assertFalse(data["capabilities"]["vision"])
        self.assertEqual(len(transport.calls), 1)

    def test_changing_model_invalidates_capabilities(self):
        transport = FakeTransport(
            [
                ProbeResponse(200, _reply("ok"), "", 10),
                lambda payload: ProbeResponse(200, _reply(_color_of(payload)), "", 10),
                ProbeResponse(200, _reply("", tool_calls=True), "", 10),
            ]
        )
        self._check(transport)
        self.assertFalse(body(api(self.client, "GET", "/api/connections/me"))["data"]["connection"]["capabilities_stale"])
        self._save(model="deepseek-reasoner")
        after = body(api(self.client, "GET", "/api/connections/me"))["data"]["connection"]
        self.assertTrue(after["capabilities_stale"])
        self.assertEqual(after["capabilities"], {})
        connection = self._conn()
        try:
            self.assertFalse(connections.has_capability(connection, self.alice["id"], "vision"))
        finally:
            connection.close()

    def test_changing_key_invalidates_capabilities(self):
        transport = FakeTransport(
            [
                ProbeResponse(200, _reply("ok"), "", 10),
                lambda payload: ProbeResponse(200, _reply(_color_of(payload)), "", 10),
                ProbeResponse(200, _reply("", tool_calls=True), "", 10),
            ]
        )
        self._check(transport)
        self._save(api_key="sk-test-rotated-000000")
        after = body(api(self.client, "GET", "/api/connections/me"))["data"]["connection"]
        self.assertTrue(after["capabilities_stale"])

    def test_server_credentials_expose_full_key_only_server_side(self):
        connection = self._conn()
        try:
            credentials = connections.server_credentials(
                connection, self.alice["id"], key=self.harness.app.config["HX17_CONNECTION_KEY"]
            )
        finally:
            connection.close()
        self.assertEqual(credentials["api_key"], API_KEY)
        self.assertEqual(credentials["model"], "deepseek-chat")

    def _conn(self):
        return self.harness.conn()


class ProbeSafetyTest(unittest.TestCase):
    """上游回显、非 2xx 误判、探测期间改配置 —— 这三个反例都曾真实发生。"""

    def setUp(self):
        self.harness = Harness()
        self.alice = self.harness.create_user("alice")
        self.admin = self.harness.create_user("admin", role="admin")
        self.client = self.harness.client()
        login(self.client, "alice")
        response = api(
            self.client,
            "PUT",
            "/api/connections/me",
            {
                "provider": "custom",
                "base_url": "https://gateway.example/v1",
                "model": "vision-1",
                "api_key": API_KEY,
            },
        )
        assert response.status_code == 200, response.get_data(as_text=True)

    def tearDown(self):
        self.harness.close()

    def _check(self, transport):
        with mock.patch("backend.services.connections.http_transport", transport):
            return api(self.client, "POST", "/api/connections/me/check", {})

    def test_upstream_reflected_key_never_reaches_response_db_or_audit(self):
        leaked = f"Invalid header: Authorization: Bearer {API_KEY}"

        def leaky(url, headers, payload, timeout):
            return connections.ProbeResponse(
                401, {"error": {"message": leaked, "hint": leaked}}, leaked, 11
            )

        response = self._check(leaky)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertNotIn(API_KEY, response.get_data(as_text=True))
        data = body(response)["data"]["connection"]
        self.assertFalse(data["capabilities"]["auth"])
        self.assertEqual(data["capabilities"]["auth_state"], "rejected")
        # 证据只要状态/耗时/环节，不要上游正文。
        self.assertNotIn(leaked, str(data["capabilities"]))
        self.assertNotIn("Invalid header", str(data["capabilities"]))

        # 数据库（含 WAL）与审计都不能留下明文 Key。
        for path in self.harness.data_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(API_KEY.encode(), path.read_bytes(), str(path))
        admin_client = self.harness.client()
        login(admin_client, "admin")
        audit_text = str(body(api(admin_client, "GET", "/api/admin/audit"))["data"]["entries"])
        self.assertNotIn(API_KEY, audit_text)
        self.assertIn("connection.check", audit_text)

    def test_transport_exception_that_echoes_key_is_redacted(self):
        def boom(url, headers, payload, timeout):
            raise RuntimeError(f"proxy failed with Bearer {API_KEY}")

        response = self._check(boom)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(API_KEY, response.get_data(as_text=True))
        data = body(response)["data"]["connection"]
        self.assertEqual(data["capabilities"]["auth_state"], "network_error")
        self.assertFalse(data["capabilities"]["auth"])

    def test_5xx_is_upstream_error_not_auth_success(self):
        for status in (500, 502, 503, 504):
            transport = FakeTransport([ProbeResponse(status, {"error": "down"}, "", 20)])
            data = body(self._check(transport))["data"]["connection"]
            self.assertFalse(data["capabilities"]["auth"], status)
            self.assertEqual(data["capabilities"]["auth_state"], "upstream_error", status)
            self.assertIn("未验证", data["summary"])
            self.assertEqual(len(transport.calls), 1)

    def test_402_and_429_do_not_claim_key_accepted(self):
        for status, state in ((402, "payment_required"), (429, "rate_limited")):
            transport = FakeTransport([ProbeResponse(status, None, "", 15)])
            data = body(self._check(transport))["data"]["connection"]
            self.assertFalse(data["capabilities"]["auth"], status)
            self.assertEqual(data["capabilities"]["auth_state"], state, status)
            self.assertIn("无法确认认证", data["summary"])
            self.assertEqual(len(transport.calls), 1)

    def test_404_and_html_proxy_error_are_unverified(self):
        for status, body_value in ((404, {"error": {"message": "model not found"}}), (200, {"html": "<html>"})):
            transport = FakeTransport([ProbeResponse(status, body_value, "<html>bad gateway</html>", 9)])
            data = body(self._check(transport))["data"]["connection"]
            self.assertFalse(data["capabilities"]["auth"], status)
            self.assertEqual(data["capabilities"]["auth_state"], "unverified", status)
            self.assertFalse(data["capabilities"]["vision"])
            self.assertEqual(len(transport.calls), 1)

    def test_connection_changed_during_probe_discards_result(self):
        changed = {"done": False}

        def mutate(url, headers, payload, timeout):
            if not changed["done"]:
                changed["done"] = True
                connection = self.harness.conn()
                try:
                    connections.save(
                        connection,
                        int(self.alice["id"]),
                        {
                            "provider": "custom",
                            "base_url": "https://gateway.example/v1",
                            "model": "text-only-2",
                        },
                        key=self.harness.app.config["HX17_CONNECTION_KEY"],
                    )
                finally:
                    connection.close()
            return ProbeResponse(200, _reply("ok"), "", 10)

        data = body(self._check(mutate))["data"]["connection"]
        self.assertFalse(data["applied"])
        self.assertEqual(data["auth_state"], "superseded")
        self.assertEqual(data["model"], "text-only-2")
        # 旧配置的检查结论不能写进新配置。
        self.assertEqual(data["capabilities"], {})
        self.assertTrue(data["capabilities_stale"])
        connection = self.harness.conn()
        try:
            self.assertFalse(connections.has_capability(connection, self.alice["id"], "vision"))
            row = connection.execute(
                "SELECT capabilities, checked_fingerprint FROM connections WHERE user_id = ?",
                (self.alice["id"],),
            ).fetchone()
            self.assertEqual(row["capabilities"], "{}")
            self.assertIsNone(row["checked_fingerprint"])
        finally:
            connection.close()


class EncryptionUnitTest(unittest.TestCase):
    def test_roundtrip_and_tamper_detection(self):
        key = b"0" * 32
        sealed = crypto.encrypt_text("sk-abc", key, aad=b"user:1")
        self.assertTrue(sealed.startswith("gcm1."))
        self.assertEqual(crypto.decrypt_text(sealed, key, aad=b"user:1"), "sk-abc")
        with self.assertRaises(Exception):
            crypto.decrypt_text(sealed, key, aad=b"user:2")
        with self.assertRaises(Exception):
            crypto.decrypt_text("gcm1.zzz.zzz", key)


if __name__ == "__main__":
    unittest.main()

"""deploy/serve.py 的服务管理接口：没有令牌不暴露、令牌错不给、停止要有钩子。

这些不是业务接口，只有本机启动/停止脚本会用；对外站点上它们应当等同于不存在。
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from backend.routes.service import TOKEN_HEADER, instance_fingerprint
from backend.tests.support import Harness


class ServiceRouteTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.client = self.harness.app.test_client()

    def tearDown(self):
        self.harness.close()

    def test_absent_without_token(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HX17_SERVICE_TOKEN", None)
            response = self.client.get("/api/service/status")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "NOT_FOUND")

    def test_wrong_token_is_rejected(self):
        with patch.dict(os.environ, {"HX17_SERVICE_TOKEN": "s3cret-token"}):
            missing = self.client.get("/api/service/status")
            wrong = self.client.get(
                "/api/service/status", headers={TOKEN_HEADER: "not-the-token"}
            )
        self.assertEqual(missing.status_code, 403)
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(wrong.get_json()["error"], "SERVICE_TOKEN_INVALID")

    def test_status_identifies_this_process(self):
        token = "s3cret-token"
        with patch.dict(os.environ, {"HX17_SERVICE_TOKEN": token}):
            response = self.client.get(
                "/api/service/status", headers={TOKEN_HEADER: token}
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()["data"]
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(data["instance"], instance_fingerprint(token))
        self.assertNotIn(token, response.get_data(as_text=True), "不能回显原始令牌")

    def test_shutdown_requires_a_hook(self):
        token = "s3cret-token"
        with patch.dict(os.environ, {"HX17_SERVICE_TOKEN": token}):
            blocked = self.client.post(
                "/api/service/shutdown", headers={TOKEN_HEADER: token}
            )
            called: list[bool] = []
            self.harness.app.config["HX17_STOP"] = lambda: called.append(True)
            try:
                allowed = self.client.post(
                    "/api/service/shutdown", headers={TOKEN_HEADER: token}
                )
            finally:
                self.harness.app.config.pop("HX17_STOP", None)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.get_json()["error"], "NO_STOP_HOOK")
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(called, [True])

    def test_forwarded_requests_are_rejected(self):
        """反代/隧道默认会加这些头：带了就不接，哪怕令牌正确。"""
        token = "s3cret-token"
        with patch.dict(os.environ, {"HX17_SERVICE_TOKEN": token}):
            for header in ("X-Forwarded-For", "Forwarded", "X-Real-IP", "CF-Connecting-IP"):
                response = self.client.get(
                    "/api/service/status",
                    headers={TOKEN_HEADER: token, header: "203.0.113.9"},
                )
                self.assertEqual(response.status_code, 403, header)
                self.assertEqual(response.get_json()["error"], "SERVICE_PROXY_REJECTED")
            proxied_stop = self.client.post(
                "/api/service/shutdown",
                headers={TOKEN_HEADER: token, "X-Forwarded-Proto": "https"},
            )
        self.assertEqual(proxied_stop.status_code, 403)
        self.assertEqual(proxied_stop.get_json()["error"], "SERVICE_PROXY_REJECTED")

    def test_non_loopback_peer_is_rejected(self):
        token = "s3cret-token"
        with patch.dict(os.environ, {"HX17_SERVICE_TOKEN": token}):
            response = self.client.get(
                "/api/service/status",
                headers={TOKEN_HEADER: token},
                environ_base={"REMOTE_ADDR": "10.8.0.7"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "SERVICE_LOCAL_ONLY")

    def test_token_never_leaks_into_public_payloads(self):
        token = "s3cret-token"
        with patch.dict(os.environ, {"HX17_SERVICE_TOKEN": token}):
            public = (
                self.client.get("/api/health").get_data(as_text=True)
                + self.client.get("/api/meta").get_data(as_text=True)
            )
            status = self.client.get(
                "/api/service/status", headers={TOKEN_HEADER: token}
            ).get_data(as_text=True)
        self.assertNotIn(token, public, "健康/meta 不能回显服务令牌")
        self.assertNotIn(token, status, "状态接口只给指纹，不给原始令牌")


if __name__ == "__main__":
    unittest.main()

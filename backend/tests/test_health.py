"""公开接口与配置快照：不泄露路径，且运行期不因环境变量切库。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from backend.app import create_app
from backend.tests.support import Harness


class PublicSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.client = self.harness.client()

    def tearDown(self):
        self.harness.close()

    def test_health_reports_status_without_paths(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["service"], "haixun-1.7-backend")
        self.assertEqual(data["version"], "1.7.0")
        self.assertEqual(data["stage"], "WB17-02")
        # 绝对路径不应出现在匿名可读的响应里。
        self.assertNotIn("data_dir", data)
        self.assertNotIn(str(self.harness.data_dir), response.get_data(as_text=True))

    def test_meta_has_no_absolute_path(self):
        response = self.client.get("/api/meta")
        self.assertEqual(response.status_code, 200)
        raw = response.get_data(as_text=True)
        data = response.get_json()["data"]
        self.assertNotIn("data_dir", data)
        self.assertNotIn("port", data)
        self.assertNotIn(str(self.harness.data_dir), raw)
        self.assertFalse(data["initialized"])

    def test_meta_flags_match_implemented_scope(self):
        features = self.client.get("/api/meta").get_json()["data"]["features"]
        self.assertTrue(features["auth"])
        self.assertTrue(features["projects"])
        self.assertTrue(features["personal_connection"])
        # WB17-02 起运行时已接入，但命令执行仍不开放。
        self.assertTrue(features["agent_runtime"])
        self.assertFalse(features["agent_shell"])
        # 未实现的必须保持 False，界面据此显示“未实现”。
        self.assertFalse(features["detection"])
        self.assertFalse(features["training"])

    def test_meta_reports_initialized_after_account_created(self):
        self.harness.create_user("alice", role="admin")
        self.assertTrue(self.client.get("/api/meta").get_json()["data"]["initialized"])

    def test_unknown_api_returns_json_404(self):
        response = self.client.get("/api/not-implemented")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["code"], 404)
        self.assertEqual(response.get_json()["error"], "HTTP_404")

    def test_anonymous_private_api_is_401(self):
        for path in ("/api/projects", "/api/auth/me", "/api/connections/me"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 401, path)
            self.assertEqual(response.get_json()["error"], "AUTH_REQUIRED")


class SettingsSnapshotTest(unittest.TestCase):
    def test_data_dir_is_frozen_at_factory_time(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            old = os.environ.get("HX17_DATA_DIR")
            os.environ["HX17_DATA_DIR"] = second
            try:
                app = create_app(data_dir=first, serve_dist=False)
                client = app.test_client()
                # 运行期改环境变量：不能换库，也不能换密钥目录。
                os.environ["HX17_DATA_DIR"] = second
                self.assertEqual(client.get("/api/health").status_code, 200)
                settings = app.config["HX17"]
                self.assertEqual(Path(settings.db_path).resolve(), (Path(first) / "haixun.db").resolve())
                self.assertTrue((Path(first) / "haixun.db").is_file())
                self.assertFalse((Path(second) / "haixun.db").exists())
            finally:
                if old is None:
                    os.environ.pop("HX17_DATA_DIR", None)
                else:
                    os.environ["HX17_DATA_DIR"] = old


if __name__ == "__main__":
    unittest.main()

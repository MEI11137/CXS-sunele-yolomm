"""管理员接口：账号创建/启停、根目录管理、越权边界。"""

from __future__ import annotations

import unittest
from pathlib import Path

from backend.tests.support import Harness, api, body, login


class AdminUserTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.admin = self.harness.create_user("admin", role="admin")
        self.member = self.harness.create_user("alice")
        self.admin_client = self.harness.client()
        login(self.admin_client, "admin")

    def tearDown(self):
        self.harness.close()

    def test_member_cannot_use_admin_api(self):
        member_client = self.harness.client()
        login(member_client, "alice")
        for method, path in (
            ("GET", "/api/admin/users"),
            ("GET", "/api/admin/roots"),
            ("GET", "/api/admin/audit"),
        ):
            response = api(member_client, method, path)
            self.assertEqual(response.status_code, 403, path)
            self.assertEqual(response.get_json()["error"], "FORBIDDEN")

    def test_member_cannot_create_users(self):
        member_client = self.harness.client()
        login(member_client, "alice")
        response = api(
            member_client, "POST", "/api/admin/users", {"username": "eve", "password": "long-enough-1"}
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_creates_member_and_role_is_forced(self):
        response = api(
            self.admin_client,
            "POST",
            "/api/admin/users",
            {"username": "bob", "password": "long-enough-1", "display_name": "小鲍", "role": "admin"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        user = body(response)["data"]["user"]
        self.assertEqual(user["role"], "member")
        self.assertEqual(user["display_name"], "小鲍")
        listed = body(api(self.admin_client, "GET", "/api/admin/users"))["data"]["users"]
        self.assertEqual(len(listed), 3)
        self.assertNotIn("password_hash", str(listed))

    def test_duplicate_username_conflicts(self):
        response = api(
            self.admin_client,
            "POST",
            "/api/admin/users",
            {"username": "alice", "password": "long-enough-1"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "USERNAME_TAKEN")

    def test_disable_user_kills_their_session(self):
        member_client = self.harness.client()
        login(member_client, "alice")
        self.assertEqual(member_client.get("/api/auth/me").status_code, 200)
        response = api(
            self.admin_client,
            "PUT",
            f"/api/admin/users/{self.member['id']}/status",
            {"status": "disabled"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(member_client.get("/api/auth/me").status_code, 401)

    def test_admin_cannot_disable_self(self):
        response = api(
            self.admin_client,
            "PUT",
            f"/api/admin/users/{self.admin['id']}/status",
            {"status": "disabled"},
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_cannot_kick_own_status_via_member_path(self):
        # 自己改自己密码必须走 /auth/password，管理接口不允许重置自己的密码。
        response = api(
            self.admin_client,
            "POST",
            f"/api/admin/users/{self.admin['id']}/reset-password",
            {"new_password": "another-strong-2"},
        )
        self.assertEqual(response.status_code, 400)

    def test_admin_reset_member_password(self):
        member_client = self.harness.client()
        login(member_client, "alice")
        response = api(
            self.admin_client,
            "POST",
            f"/api/admin/users/{self.member['id']}/reset-password",
            {"new_password": "another-strong-2"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(member_client.get("/api/auth/me").status_code, 401)


class AdminRootTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.admin = self.harness.create_user("admin", role="admin")
        self.alice = self.harness.create_user("alice")
        self.root = self.harness.make_dir("allowed")
        self.admin_client = self.harness.client()
        login(self.admin_client, "admin")
        self.client = self.harness.client()
        login(self.client, "alice")

    def tearDown(self):
        self.harness.close()

    def test_add_and_list_root(self):
        response = api(
            self.admin_client,
            "POST",
            "/api/admin/roots",
            {"path": str(self.root), "label": "训练数据"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        roots = body(api(self.admin_client, "GET", "/api/admin/roots"))["data"]["roots"]
        self.assertEqual([item["label"] for item in roots], ["训练数据"])
        self.assertEqual(Path(roots[0]["path"]), self.root.resolve())
        # 默认不共享：新加的根目录不会自动授予任何账号。
        self.assertEqual(roots[0]["grants"], [])

    def test_duplicate_root_conflicts(self):
        api(self.admin_client, "POST", "/api/admin/roots", {"path": str(self.root)})
        response = api(self.admin_client, "POST", "/api/admin/roots", {"path": str(self.root)})
        self.assertEqual(response.status_code, 409)

    def test_drive_root_cannot_be_authorized(self):
        drive = Path(self.root.resolve().anchor)
        response = api(self.admin_client, "POST", "/api/admin/roots", {"path": str(drive)})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "TOO_BROAD")

    def test_remove_root_blocked_while_project_bound(self):
        api(
            self.admin_client,
            "POST",
            "/api/admin/roots",
            {"path": str(self.root), "grant_to": ["alice"]},
        )
        project = self.harness.create_project(self.alice["id"], "绑定中")
        bind = api(
            self.client,
            "PUT",
            f"/api/projects/{project['id']}/root",
            {"path": str(self.root)},
        )
        self.assertEqual(bind.status_code, 200, bind.get_data(as_text=True))
        root_id = body(api(self.admin_client, "GET", "/api/admin/roots"))["data"]["roots"][0]["id"]
        response = api(self.admin_client, "DELETE", f"/api/admin/roots/{root_id}")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "ROOT_IN_USE")
        # 解除绑定后可以移除。
        api(self.client, "DELETE", f"/api/projects/{project['id']}/root")
        self.assertEqual(api(self.admin_client, "DELETE", f"/api/admin/roots/{root_id}").status_code, 200)

    def test_audit_log_records_sensitive_actions(self):
        api(self.admin_client, "POST", "/api/admin/roots", {"path": str(self.root)})
        entries = body(api(self.admin_client, "GET", "/api/admin/audit"))["data"]["entries"]
        actions = [item["action"] for item in entries]
        self.assertIn("root.add", actions)
        self.assertIn("auth.login", actions)
        self.assertNotIn("correct-horse-9", str(entries))


if __name__ == "__main__":
    unittest.main()

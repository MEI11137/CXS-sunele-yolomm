"""按账号授予根目录：默认不共享、撤销立即生效。

对应独立审查复现的问题：普通账号能看到/绑定管理员给其他人准备的目录。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend import db, security
from backend.tests.support import Harness, api, body, login


class RootGrantTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.admin = self.harness.create_user("admin", role="admin")
        self.alice = self.harness.create_user("alice")
        self.bob = self.harness.create_user("bob")
        self.root_a = self.harness.make_dir("root-a")
        self.root_b = self.harness.make_dir("root-b")
        (self.root_a / "site-1").mkdir()
        (self.root_b / "site-2").mkdir()
        self.grant_a = self.harness.add_root(self.root_a, "A 的数据", grant_to=["alice"])
        self.grant_b = self.harness.add_root(self.root_b, "B 的数据", grant_to=["bob"])
        self.project_a = self.harness.create_project(self.alice["id"], "爱丽丝项目")
        self.project_b = self.harness.create_project(self.bob["id"], "鲍勃项目")
        self.client = self.harness.client()
        login(self.client, "alice")

    def tearDown(self):
        self.harness.close()

    def _roots(self, client) -> list[str]:
        return [item["path"] for item in body(api(client, "GET", "/api/directories/roots"))["data"]["roots"]]

    def test_member_only_lists_granted_roots(self):
        self.assertEqual(self._roots(self.client), [str(self.root_a.resolve())])

    def test_member_browse_other_users_root_is_rejected(self):
        response = api(
            self.client, "GET", f"/api/directories/browse?path={self.root_b}"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "OUTSIDE_ALLOWED_ROOTS")

    def test_browse_top_level_only_contains_own_root(self):
        data = body(api(self.client, "GET", "/api/directories/browse"))["data"]
        self.assertEqual([item["path"] for item in data["entries"]], [str(self.root_a.resolve())])
        self.assertEqual(data["roots"], [str(self.root_a.resolve())])

    def test_member_cannot_bind_other_users_root(self):
        response = api(
            self.client,
            "PUT",
            f"/api/projects/{self.project_a['id']}/root",
            {"path": str(self.root_b)},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "OUTSIDE_ALLOWED_ROOTS")
        project = body(api(self.client, "GET", f"/api/projects/{self.project_a['id']}"))["data"]["project"]
        self.assertIsNone(project["root_path"])

    def test_member_without_any_grant_cannot_bind(self):
        carol = self.harness.create_user("carol")
        project = self.harness.create_project(carol["id"], "无授权")
        carol_client = self.harness.client()
        login(carol_client, "carol")
        self.assertEqual(self._roots(carol_client), [])
        response = api(
            carol_client,
            "PUT",
            f"/api/projects/{project['id']}/root",
            {"path": str(self.root_a)},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "NO_ALLOWED_ROOTS")

    def test_admin_can_share_one_root_with_two_accounts(self):
        admin_client = self.harness.client()
        login(admin_client, "admin")
        response = api(
            admin_client,
            "POST",
            f"/api/admin/roots/{self.grant_a['id']}/grants",
            {"username": "bob"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        names = [item["username"] for item in body(response)["data"]["root"]["grants"]]
        self.assertEqual(sorted(names), ["alice", "bob"])

        bob_client = self.harness.client()
        login(bob_client, "bob")
        self.assertEqual(
            sorted(self._roots(bob_client)),
            sorted([str(self.root_a.resolve()), str(self.root_b.resolve())]),
        )
        bind = api(
            bob_client,
            "PUT",
            f"/api/projects/{self.project_b['id']}/root",
            {"path": str(self.root_a / "site-1")},
        )
        self.assertEqual(bind.status_code, 200, bind.get_data(as_text=True))

    def test_revoke_blocks_existing_binding_immediately(self):
        bind = api(
            self.client,
            "PUT",
            f"/api/projects/{self.project_a['id']}/root",
            {"path": str(self.root_a / "site-1")},
        )
        self.assertEqual(bind.status_code, 200, bind.get_data(as_text=True))
        self.assertEqual(
            api(self.client, "GET", f"/api/projects/{self.project_a['id']}/root/entries").status_code,
            200,
        )

        admin_client = self.harness.client()
        login(admin_client, "admin")
        revoke = api(
            admin_client,
            "DELETE",
            f"/api/admin/roots/{self.grant_a['id']}/grants/{self.alice['id']}",
        )
        self.assertEqual(revoke.status_code, 200)
        self.assertTrue(body(revoke)["data"]["removed"])

        # 列表、项目浏览、再次绑定都必须拒绝。
        self.assertEqual(self._roots(self.client), [])
        browse = api(
            self.client, "GET", f"/api/projects/{self.project_a['id']}/root/entries"
        )
        self.assertEqual(browse.status_code, 403)
        self.assertEqual(browse.get_json()["error"], "GRANT_REVOKED")
        rebind = api(
            self.client,
            "PUT",
            f"/api/projects/{self.project_a['id']}/root",
            {"path": str(self.root_a / "site-1"), "replace": True},
        )
        self.assertEqual(rebind.status_code, 403)
        self.assertEqual(rebind.get_json()["error"], "GRANT_REVOKED")
        # 原绑定仍在库里，但访问被拒；恢复授予后应能重新访问。
        admin_grant = api(
            admin_client,
            "POST",
            f"/api/admin/roots/{self.grant_a['id']}/grants",
            {"username": "alice"},
        )
        self.assertEqual(admin_grant.status_code, 200)
        self.assertEqual(
            api(self.client, "GET", f"/api/projects/{self.project_a['id']}/root/entries").status_code,
            200,
        )

    def test_new_root_is_not_shared_by_default(self):
        admin_client = self.harness.client()
        login(admin_client, "admin")
        extra = self.harness.make_dir("extra")
        response = api(admin_client, "POST", "/api/admin/roots", {"path": str(extra)})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(body(response)["data"]["root"]["grants"], [])
        self.assertNotIn(str(extra.resolve()), self._roots(self.client))

    def test_unknown_username_grant_is_404(self):
        admin_client = self.harness.client()
        login(admin_client, "admin")
        response = api(
            admin_client,
            "POST",
            f"/api/admin/roots/{self.grant_a['id']}/grants",
            {"username": "nobody"},
        )
        self.assertEqual(response.status_code, 404)


class MigrationTest(unittest.TestCase):
    """v1 升级到 v2 只建关系表，不把历史全局授权自动发给任何账号。"""

    def test_v1_to_v2_does_not_auto_grant(self):
        with tempfile.TemporaryDirectory(prefix="hx17-migrate-") as tmp:
            path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            try:
                conn.executescript(db.SCHEMA_V1)
                conn.execute("PRAGMA user_version = 1")
                conn.execute(
                    "INSERT INTO allowed_roots (path, label, created_at) VALUES (?, ?, ?)",
                    (str(Path(tmp) / "legacy-root"), "历史目录", security.now_iso()),
                )
                conn.execute(
                    """
                    INSERT INTO users (username, display_name, password_hash, role, status,
                                       token_version, created_at, updated_at)
                    VALUES ('legacy', 'legacy', 'x', 'member', 'active', 1, ?, ?)
                    """,
                    (security.now_iso(), security.now_iso()),
                )
                conn.commit()

                db.ensure_schema(conn)
                # 迁移只向前追加：v3 加的是 Agent 相关空表，同样不发放任何目录授权。
                self.assertEqual(
                    conn.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS)
                )
                granted = conn.execute("SELECT COUNT(*) FROM root_grants").fetchone()[0]
                self.assertEqual(granted, 0)
                # 旧授权根仍然存在，只是没有归属。
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM allowed_roots").fetchone()[0], 1
                )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

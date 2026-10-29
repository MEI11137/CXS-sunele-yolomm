"""真实临时目录：绑定、权限、重叠、旧项目兼容与 HTTP 本机边界。"""
from pathlib import Path
import sqlite3
import os
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from agent import identity, permissions, store
from agent.errors import AgentError
from agent.project_folders import browse, checked_project_root, make_directory
from agent.routes import agent_bp


class ProjectFoldersTest(unittest.TestCase):
    def setUp(self):
        staging = Path(os.environ.get("OPENCODE_TEST_TMP") or Path(__file__).resolve().parents[4] / ".integration-checks")
        staging.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="haixun-folders-", dir=staging)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.app = Flask(__name__)
        self.app.config.update(DATABASE=str(self.root / "index.sqlite3"), TESTING=True)
        conn = sqlite3.connect(self.app.config["DATABASE"])
        conn.execute("CREATE TABLE user (id INTEGER PRIMARY KEY, username TEXT, role INTEGER, status INTEGER)")
        conn.executemany("INSERT INTO user VALUES (?,?,?,1)", [(1, "admin", 1), (2, "user", 0), (3, "other-admin", 1)])
        conn.commit()
        conn.close()
        self.app.register_blueprint(agent_bp)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        store.reset_schema_cache()
        self.admin = identity.load_actor(1)
        self.user = identity.load_actor(2)
        self.other = identity.load_actor(3)
        self.folder = self.root / "光伏项目"
        self.folder.mkdir()

    def test_binding_preserves_existing_files_and_roundtrips(self):
        target = self.folder / "人工标注.txt"
        target.write_text("保留原数据", encoding="utf-8")
        project = permissions.create_project(self.admin, "光伏", root_directory=str(self.folder))
        self.assertEqual(checked_project_root(self.admin, project), self.folder)
        self.assertEqual(permissions.list_projects(self.admin)[0]["root_directory"], str(self.folder))
        self.assertEqual(target.read_text(encoding="utf-8"), "保留原数据")
        self.assertEqual(list(self.folder.iterdir()), [target])
        with self.assertRaises(AgentError):
            permissions.get_project(self.other, project["id"])

    def test_old_project_can_bind_once_without_changing_business_data(self):
        project = permissions.create_project(self.admin, "历史")
        self.assertIsNone(checked_project_root(self.admin, project))
        result = permissions.update_project(self.admin, project["id"], root_directory=str(self.folder))
        self.assertEqual(result["root_directory"], str(self.folder))
        with self.assertRaises(AgentError):
            permissions.update_project(self.admin, project["id"], root_directory=str(self.root))

    def test_ordinary_account_and_invalid_paths_are_rejected(self):
        with self.assertRaises(AgentError):
            permissions.create_project(self.user, "拒绝", root_directory=str(self.folder))
        for path in ("relative", str(self.root / "missing"), str(Path(self.root.anchor)), "", 12):
            with self.subTest(path=path), self.assertRaises(AgentError):
                permissions.create_project(self.admin, "拒绝", root_directory=path)

    def test_nested_or_duplicate_roots_are_rejected_across_users(self):
        permissions.create_project(self.admin, "已绑定", root_directory=str(self.folder))
        child = self.folder / "child"
        child.mkdir()
        for path in (self.folder, child, self.root):
            with self.subTest(path=path), self.assertRaises(AgentError):
                permissions.create_project(self.other, "不允许重叠", root_directory=str(path))

    def test_new_folder_never_overwrites_and_refuses_traversal(self):
        result = make_directory(self.admin, str(self.folder), "训练数据")
        self.assertTrue(Path(result["path"]).is_dir())
        for name in ("训练数据", "../escape", "CON", "a/b", "..", "bad."):
            with self.subTest(name=name), self.assertRaises(AgentError):
                make_directory(self.admin, str(self.folder), name)
        self.assertEqual(browse(self.admin, str(self.folder))["directories"][0]["name"], "训练数据")

    def test_missing_root_is_not_silently_recreated(self):
        project = permissions.create_project(self.admin, "待移除", root_directory=str(self.folder))
        self.folder.rmdir()
        with self.assertRaises(AgentError):
            checked_project_root(self.admin, project)
        self.assertFalse(self.folder.exists())

    def test_http_needs_login_localhost_and_admin_for_folder_operations(self):
        client = self.app.test_client()
        self.assertEqual(client.get("/api/agent/local-folders").status_code, 401)
        headers = {"Authorization": "Bearer fixture"}
        with patch("agent.routes.get_user_id", return_value=1):
            self.assertEqual(client.get("/api/agent/local-folders", headers=headers).status_code, 200)
            result = client.post("/api/agent/projects", headers=headers, json={"name": "HTTP", "root_directory": str(self.folder)})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json["data"]["root_directory"], str(self.folder))
            self.assertEqual(client.get("/api/agent/local-folders", headers=headers,
                environ_overrides={"REMOTE_ADDR": "192.168.0.10"}).status_code, 403)
            self.assertEqual(client.post("/api/agent/projects", headers=headers, json={"name": "远程", "root_directory": str(self.folder)},
                environ_overrides={"REMOTE_ADDR": "192.168.0.10"}).status_code, 403)
        with patch("agent.routes.get_user_id", return_value=2):
            self.assertEqual(client.get("/api/agent/local-folders", headers=headers).status_code, 403)


if __name__ == "__main__":
    unittest.main()

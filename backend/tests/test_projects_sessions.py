"""项目与会话：隔离、软删除/恢复、消息历史。"""

from __future__ import annotations

import unittest

from backend.tests.support import Harness, api, body, login


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.alice = self.harness.create_user("alice")
        self.bob = self.harness.create_user("bob")
        self.client = self.harness.client()
        login(self.client, "alice")

    def tearDown(self):
        self.harness.close()

    def _create(self, name: str = "光伏巡检") -> dict:
        response = api(self.client, "POST", "/api/projects", {"name": name})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return body(response)["data"]["project"]

    def test_create_and_list(self):
        project = self._create()
        self.assertEqual(project["name"], "光伏巡检")
        self.assertIsNone(project["root_path"])
        listed = body(api(self.client, "GET", "/api/projects"))["data"]["projects"]
        self.assertEqual([item["id"] for item in listed], [project["id"]])

    def test_empty_name_rejected(self):
        response = api(self.client, "POST", "/api/projects", {"name": "   "})
        self.assertEqual(response.status_code, 400)

    def test_rename(self):
        project = self._create()
        response = api(self.client, "PUT", f"/api/projects/{project['id']}", {"name": "二期"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body(response)["data"]["project"]["name"], "二期")

    def test_soft_delete_and_restore(self):
        project = self._create()
        self.assertEqual(api(self.client, "DELETE", f"/api/projects/{project['id']}").status_code, 200)
        self.assertEqual(body(api(self.client, "GET", "/api/projects"))["data"]["projects"], [])
        trash = body(api(self.client, "GET", "/api/projects?trash=1"))["data"]["projects"]
        self.assertEqual([item["id"] for item in trash], [project["id"]])
        # 回收站内的项目不能再按普通路径读取。
        self.assertEqual(api(self.client, "GET", f"/api/projects/{project['id']}").status_code, 200)
        restore = api(self.client, "POST", f"/api/projects/{project['id']}/restore", {})
        self.assertEqual(restore.status_code, 200)
        self.assertEqual(body(api(self.client, "GET", "/api/projects"))["data"]["projects"][0]["id"], project["id"])

    def test_delete_twice_is_rejected(self):
        project = self._create()
        api(self.client, "DELETE", f"/api/projects/{project['id']}")
        second = api(self.client, "DELETE", f"/api/projects/{project['id']}")
        self.assertIn(second.status_code, (404, 409))

    def test_other_user_cannot_read_or_write(self):
        project = self._create()
        bob = self.harness.client()
        login(bob, "bob")
        self.assertEqual(api(bob, "GET", f"/api/projects/{project['id']}").status_code, 404)
        self.assertEqual(api(bob, "PUT", f"/api/projects/{project['id']}", {"name": "劫持"}).status_code, 404)
        self.assertEqual(api(bob, "DELETE", f"/api/projects/{project['id']}").status_code, 404)
        # 越权尝试后数据未被改动。
        conn = self.harness.conn()
        try:
            row = conn.execute("SELECT name, status FROM projects WHERE id = ?", (project["id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["name"], "光伏巡检")
        self.assertEqual(row["status"], "active")

    def test_bob_only_sees_his_own_projects(self):
        self._create("alice 的项目")
        self.harness.create_project(self.bob["id"], "bob 的项目")
        bob = self.harness.client()
        login(bob, "bob")
        names = [item["name"] for item in body(api(bob, "GET", "/api/projects"))["data"]["projects"]]
        self.assertEqual(names, ["bob 的项目"])


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.alice = self.harness.create_user("alice")
        self.bob = self.harness.create_user("bob")
        self.project = self.harness.create_project(self.alice["id"], "巡检项目")
        self.client = self.harness.client()
        login(self.client, "alice")

    def tearDown(self):
        self.harness.close()

    def _create(self, title: str = "第一次巡检") -> dict:
        response = api(
            self.client, "POST", f"/api/projects/{self.project['id']}/sessions", {"title": title}
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return body(response)["data"]["session"]

    def test_create_list_rename(self):
        session = self._create()
        self.assertEqual(session["project_id"], self.project["id"])
        listed = body(api(self.client, "GET", f"/api/projects/{self.project['id']}/sessions"))["data"]["sessions"]
        self.assertEqual([item["id"] for item in listed], [session["id"]])
        renamed = api(self.client, "PUT", f"/api/sessions/{session['id']}", {"title": "改个名字"})
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(body(renamed)["data"]["session"]["title"], "改个名字")

    def test_soft_delete_and_restore(self):
        session = self._create()
        self.assertEqual(api(self.client, "DELETE", f"/api/sessions/{session['id']}").status_code, 200)
        active = body(api(self.client, "GET", f"/api/projects/{self.project['id']}/sessions"))["data"]["sessions"]
        self.assertEqual(active, [])
        trash = body(
            api(self.client, "GET", f"/api/projects/{self.project['id']}/sessions?trash=1")
        )["data"]["sessions"]
        self.assertEqual(len(trash), 1)
        restored = api(self.client, "POST", f"/api/sessions/{session['id']}/restore", {})
        self.assertEqual(restored.status_code, 200)

    def test_other_user_cannot_open_session(self):
        session = self._create()
        bob = self.harness.client()
        login(bob, "bob")
        self.assertEqual(api(bob, "GET", f"/api/sessions/{session['id']}/messages").status_code, 404)
        self.assertEqual(api(bob, "PUT", f"/api/sessions/{session['id']}", {"title": "x"}).status_code, 404)

    def test_session_of_deleted_project_is_inaccessible(self):
        session = self._create()
        api(self.client, "DELETE", f"/api/projects/{self.project['id']}")
        self.assertEqual(api(self.client, "GET", f"/api/sessions/{session['id']}/messages").status_code, 404)

    def test_message_is_persisted_without_fake_assistant_reply(self):
        session = self._create()
        response = api(
            self.client,
            "POST",
            f"/api/sessions/{session['id']}/messages",
            {"content": "检查 D:/data/panel-01 下的图片"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = body(response)["data"]
        self.assertEqual(data["message"]["role"], "user")
        self.assertIsNone(data["assistant"])
        self.assertIn("WB17-02", data["notice"])

        messages = body(api(self.client, "GET", f"/api/sessions/{session['id']}/messages"))["data"]["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "检查 D:/data/panel-01 下的图片")

    def test_empty_message_rejected(self):
        session = self._create()
        response = api(self.client, "POST", f"/api/sessions/{session['id']}/messages", {"content": "   "})
        self.assertEqual(response.status_code, 400)

    def test_message_belongs_to_one_session_only(self):
        first = self._create("A")
        second = self._create("B")
        api(self.client, "POST", f"/api/sessions/{first['id']}/messages", {"content": "只属于 A"})
        self.assertEqual(
            body(api(self.client, "GET", f"/api/sessions/{second['id']}/messages"))["data"]["messages"], []
        )


if __name__ == "__main__":
    unittest.main()

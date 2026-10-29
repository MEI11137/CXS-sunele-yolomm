"""项目会话标题持久化及删除演示资源后的重启行为。"""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from flask import Flask
from agent import permissions, sessions, store
from agent.identity import ActorContext
from utils.db import Database


class ProjectConversationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="conversation-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "test.sqlite3"
        self.app = Flask(__name__)
        self.app.config["DATABASE"] = str(self.path)
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        store.reset_schema_cache()
        self.actor = ActorContext(1, ("admin",))

    def test_title_uses_first_question_and_survives_reloading(self):
        project = permissions.create_project(self.actor, "热斑检测")
        chat = sessions.create_session(self.actor, project["id"])
        sessions.append_message(chat["id"], sessions.ROLE_USER, "检查这份数据集\n先找错误标注")
        sessions.append_message(chat["id"], sessions.ROLE_USER, "然后再检查模态")
        reloaded = sessions.list_sessions(self.actor, project["id"])[0]
        self.assertEqual(reloaded["title"], "检查这份数据集")
        self.assertEqual(reloaded["project_id"], project["id"])

    def test_custom_title_is_preserved(self):
        project = permissions.create_project(self.actor, "热斑检测")
        chat = sessions.create_session(self.actor, project["id"], "我的训练记录")
        sessions.append_message(chat["id"], sessions.ROLE_USER, "新问题")
        self.assertEqual(sessions.get_session(self.actor, chat["id"])["title"], "我的训练记录")

    def test_deleted_demo_resources_do_not_reappear_on_restart(self):
        database = Database(self.path, seed_demo=True)
        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM models").fetchone()[0], 1)
            conn.execute("DELETE FROM models")
            conn.execute("DELETE FROM datasets")
            conn.commit()
        finally:
            conn.close()
        database.initialize_db()
        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM datasets").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM models").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM user").fetchone()[0], 2)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

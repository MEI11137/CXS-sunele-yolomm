"""INT-01 共同装配回归检查。

与 B、C 各自的测试不同，这里跑的是**同一装配入口**：真实 SQLite、真实
``BusinessToolAdapter``、真实权限桥，用来防止"两边各自注入替身都能过、
合起来却不成立"的情况。

    python -m unittest tests.test_integration_assembly -v

本轮只验证装配与权限语义；工具契约（B 的 ToolSpec ↔ C 的 dict）尚未桥接，
属 INT-02，因此这里不把"模型能看到 C 的工具"当作通过条件。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from flask import Flask

from agent import bootstrap, permissions
from agent.identity import ActorContext, load_actor
from agent.registry import register_agent, reset_registration_cache
from agent.tools import business as business_tools
from agent.tools import registry as tool_registry
from utils.db import Database


class IntegrationAssemblyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="int-assembly-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self._env_backup = {k: os.environ.get(k) for k in ("AGENT_SECRET_PATH", "AGENT_CANDIDATE_ROOT")}
        os.environ["AGENT_SECRET_PATH"] = str(self.tmp / "secret.key")
        os.environ["AGENT_CANDIDATE_ROOT"] = str(self.tmp / "candidates")
        for key in self._env_backup:
            self.addCleanup(self._restore_env, key)

        self.db_path = str(self.tmp / "app.sqlite3")
        database = Database()
        database.db_path = self.db_path
        database.initialize_db()
        conn = sqlite3.connect(self.db_path)
        try:
            # initialize_db 会预置默认管理员；这里换成两个固定 ID 的账号，
            # 让断言可以用确定的 1/2 身份而不是猜自增序号。
            conn.execute("DELETE FROM user")
            conn.execute(
                "INSERT INTO user (id,username,password,role,status) VALUES (1,'admin','x',1,1)"
            )
            conn.execute(
                "INSERT INTO user (id,username,password,role,status) VALUES (2,'plain','x',0,1)"
            )
            conn.commit()
        finally:
            conn.close()

        self.storage_dir = self.tmp / "store"
        self.work_dir = self.tmp / "temp"
        self.storage_dir.mkdir()
        self.work_dir.mkdir()

        self.app = Flask(__name__)
        self.app.config.update(
            DATABASE=self.db_path,
            TEMP_DIR=str(self.work_dir),
            FILE_STORE_PATH=str(self.storage_dir),
            TESTING=True,
        )
        tool_registry.reset_registry()
        business_tools.set_backend(None)
        reset_registration_cache()

        self.report = register_agent(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)

        self.adapter = bootstrap.get_business_backend(self.app)
        self.policy = bootstrap.get_policy(self.app)

    def _restore_env(self, key: str) -> None:
        original = self._env_backup.get(key)
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original

    # -- 装配 ---------------------------------------------------------------

    def test_business_backend_is_assembled_on_this_app(self) -> None:
        self.assertIsNotNone(self.adapter)
        entry = self.app.extensions[bootstrap.EXTENSION_KEY]
        self.assertEqual(entry["report"]["database"], self.db_path)
        self.assertIs(entry["business_backend"], self.adapter)

    def test_project_file_access_policy_is_installed(self) -> None:
        self.assertTrue(callable(self.app.config.get("PROJECT_FILE_ACCESS")))

    def test_mcp_transport_is_wired_but_no_connection_is_faked(self) -> None:
        deps = self.app.extensions[bootstrap.EXTENSION_KEY]["report"]["dependencies"]
        self.assertIn("mcp_store", deps)
        self.assertEqual("ready", deps["mcp_store"])
        store = self.app.extensions[bootstrap.EXTENSION_KEY]["mcp_store"]
        self.assertTrue(callable(store.transport_factory))
        self.assertEqual([], store.list(1))

    def test_reconfiguration_is_idempotent(self) -> None:
        again = bootstrap.configure_business_backend(self.app)
        self.assertIs(again, self.app.extensions[bootstrap.EXTENSION_KEY]["report"])
        self.assertIs(bootstrap.get_business_backend(self.app), self.adapter)

    def test_registry_reads_current_app_backend(self) -> None:
        self.assertIs(business_tools.get_backend(), self.adapter)
        self.assertIsNotNone(tool_registry.get_registry())

    def test_two_apps_do_not_share_a_backend(self) -> None:
        other = Flask(__name__)
        other.config.update(
            DATABASE=str(self.tmp / "other.sqlite3"),
            TEMP_DIR=str(self.work_dir),
            FILE_STORE_PATH=str(self.storage_dir),
            TESTING=True,
        )
        other_adapter = bootstrap.configure_business_backend(other)["database"]
        self.assertEqual(other_adapter, str(self.tmp / "other.sqlite3"))
        self.assertIsNot(bootstrap.get_business_backend(other), self.adapter)

    # -- 权限桥 -------------------------------------------------------------

    def test_account_level_actions_need_a_real_account(self) -> None:
        self.assertTrue(self.policy.require_access(1, "tool:list", None))
        with self.assertRaises(PermissionError):
            self.policy.require_access(999, "tool:list", None)

    def test_unknown_action_fails_closed(self) -> None:
        with self.assertRaises(PermissionError):
            self.policy.require_access(1, "dataset:superuser", None)

    def test_global_publish_is_admin_only(self) -> None:
        self.assertTrue(self.policy.require_access(1, "model:publish-global", None))
        with self.assertRaises(PermissionError):
            self.policy.require_access(2, "model:publish-global", None)

    def test_resource_action_requires_a_snapshot(self) -> None:
        with self.assertRaises(PermissionError):
            self.policy.require_access(1, "dataset:read", None)

    def test_unregistered_business_resource_is_denied(self) -> None:
        with self.assertRaises(PermissionError):
            self.policy.resolve_resource(1, "dataset", "no-such-dataset")

    def test_project_snapshot_and_cross_user_isolation(self) -> None:
        project = permissions.ensure_default_project(load_actor(1))
        snapshot = self.policy.resolve_resource(1, "project", project["id"])
        self.assertEqual(snapshot.owner_id, 1)
        self.assertEqual(snapshot.project_id, project["id"])
        with self.assertRaises(Exception):
            self.policy.resolve_resource(2, "project", project["id"])

    def test_file_policy_is_owner_only(self) -> None:
        project = permissions.ensure_default_project(load_actor(1))
        self.assertTrue(self.policy.project_file_access(1, project["id"], "write"))
        self.assertFalse(self.policy.project_file_access(2, project["id"], "write"))

    # -- C 适配器 -----------------------------------------------------------

    def test_adapter_exposes_real_tools_for_a_real_principal(self) -> None:
        names = {tool["name"] for tool in self.adapter.list_tools(1)}
        self.assertTrue(names, "C 适配器应能返回自己的工具描述符")
        self.assertIn('mcp.execute', names)  # internal C contract
        from agent.business_bridge import BusinessBackendBridge
        self.assertFalse(any(s.name.startswith('business_mcp') for s in BusinessBackendBridge(self.adapter).list_tools(load_actor(1))))

    def test_plain_user_sees_publish_tools_for_own_project(self) -> None:
        """INT-05：发布不再依赖全局发布权，普通用户能发布自己项目里的模型。"""
        names = {tool["name"] for tool in self.adapter.list_tools(2)}
        self.assertIn("model.publish", names)
        self.assertIn("model.unpublish", names)

    def test_human_only_tools_are_not_offered_to_the_agent(self) -> None:
        """人工复核、样本划分、连接配置不进 LLM 工具清单。"""
        names = {tool["name"] for tool in self.adapter.list_tools(1)}
        for blocked in ("review.submit_human", "draft.assign_splits",
                        "mcp.save_connection"):
            self.assertNotIn(blocked, names)

    def test_publish_scope_is_owner_and_project(self) -> None:
        """归档范围解析走真实归属，而不是整表同模态。"""
        from agent import resources

        project = permissions.ensure_default_project(load_actor(1))
        actor = ActorContext(user_id=1, roles=("user", "admin"), source="system")
        for model_id in ("model-a", "model-b"):
            permissions.register_resource(
                actor, project_id=project["id"], kind="model",
                display_name=model_id, source_table="models", source_id=model_id,
            )
        self.assertEqual(sorted(resources.model_scope_ids("model-a")),
                         ["model-a", "model-b"])


if __name__ == "__main__":
    unittest.main()

"""INT-03 权限与真实归属回归检查。

覆盖四类容易"各自单测都过、合起来越权"的场景：

1. ``RegistrationTest``：登记幂等、归属只从业务行/文件链路推导、版本递增、
   登记失败留痕并可在条件具备后重试成功；
2. ``VisibilityTest``：已存在来源的可见性（R1）与 HTTP 不得自报来源（R2）；
3. ``BridgePolicyTest``：权限桥的动作映射 fail-closed、项目文件策略
   （本人可写 / 系统默认只读）、未登记业务行按真实归属懒登记；
4. ``PageGuardTest``：原页面路由的"本人资源"授权，别人的资源一律 403。

    python -m unittest tests.test_integration_policy -v
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

from agent import bootstrap, legacy, permissions, resources
from agent.identity import ActorContext, load_actor
from agent.policy_bridge import AgentPolicyBridge
from agent.registry import register_agent, reset_registration_cache
from agent.store import connect, query_one, reset_schema_cache
from utils.db import Database


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="int-policy-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._env_backup = {
            k: os.environ.get(k)
            for k in ("AGENT_SECRET_PATH", "AGENT_CANDIDATE_ROOT", "AGENT_ALLOW_STUB")
        }
        os.environ["AGENT_SECRET_PATH"] = str(self.tmp / "secret.key")
        os.environ["AGENT_CANDIDATE_ROOT"] = str(self.tmp / "candidates")
        for key in self._env_backup:
            self.addCleanup(self._restore_env, key)

        self.db_path = str(self.tmp / "app.sqlite3")
        database = Database()
        database.db_path = self.db_path
        database.initialize_db()
        self.database = database
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM user")
            conn.execute(
                "INSERT INTO user (id,username,password,role,status) VALUES (1,'admin','x',1,1)"
            )
            conn.execute(
                "INSERT INTO user (id,username,password,role,status) VALUES (2,'owner','x',0,1)"
            )
            conn.execute(
                "INSERT INTO user (id,username,password,role,status) VALUES (3,'other','x',0,1)"
            )
            conn.commit()
        finally:
            conn.close()

        self.storage_dir = self.tmp / "store"
        self.work_dir = self.tmp / "temp"
        self.storage_dir.mkdir()
        self.work_dir.mkdir()

        # file_assets 由文件网关建表，这里显式建好，便于构造上传链路
        from services.file_access import FileAccess

        FileAccess(self.db_path)

        self.app = Flask(__name__)
        self.app.config.update(
            DATABASE=self.db_path,
            TEMP_DIR=str(self.work_dir),
            FILE_STORE_PATH=str(self.storage_dir),
            TESTING=True,
        )
        reset_schema_cache()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)

        self.owner = load_actor(2)
        self.other = load_actor(3)
        self.admin = load_actor(1)

    def _restore_env(self, key: str) -> None:
        original = self._env_backup.get(key)
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original

    # -- 业务数据辅助 ------------------------------------------------------

    def _insert_dataset(self, dataset_id: str, name: str = "数据集") -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO datasets "
                "(id, name, description, zip_bucket, zip_object_key, structure_info, "
                " upload_time, file_size, status) VALUES (?, ?, '', 'b', 'k', '{}', "
                " '2026-01-01T00:00:00', 1, 1)",
                (dataset_id, name),
            )
            conn.commit()
        finally:
            conn.close()

    def _insert_record(self, record_id: str, user_id: int) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO detection_records (id, user_id, model_id, model_name, "
                "original_image_bucket, original_image_object_key, result_image_bucket, "
                "result_image_object_key, detection_result, confidence_threshold, create_time) "
                "VALUES (?, ?, 'm1', 'model', 'b', 'o', 'b', 'r', '{}', 0.25, "
                "'2026-01-01T00:00:00')",
                (record_id, user_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _insert_upload(self, owner_id: int, project_id: str, business_id: str,
                       business_type: str = "datasets") -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO file_assets "
                "(id, bucket, object_key, owner_id, project_id, kind, mime_type, size, "
                " status, version, created_at, business_type, business_id) "
                "VALUES (?, 'b', ?, ?, ?, 'dataset', 'application/zip', 1, 'registered', "
                " 1, '2026-01-01T00:00:00', ?, ?)",
                (
                    f"fa-{business_id}",
                    f"key-{business_id}",
                    owner_id,
                    project_id,
                    business_type,
                    business_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _project(self, actor: ActorContext, name: str = "工作区") -> dict:
        return permissions.create_project(actor, name)


# --------------------------------------------------------------------------- #
# 1) 登记
# --------------------------------------------------------------------------- #
class RegistrationTest(_Base):
    def test_owner_is_derived_from_business_row_not_from_caller(self) -> None:
        self._insert_record("rec-1", 2)
        # 调用方声称自己是 3，但业务行属于 2：登记必须按 2 落库
        view = resources.ensure_kind("record", "rec-1", owner_id=3)
        self.assertIsNotNone(view)
        self.assertEqual(view["owner_id"], 2)
        self.assertTrue(view["created"])

    def test_registration_is_idempotent(self) -> None:
        self._insert_dataset("ds-1")
        first = resources.ensure("datasets", "ds-1", owner_id=2)
        second = resources.ensure("datasets", "ds-1", owner_id=3)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["owner_id"], 2)

    def test_unknown_ownership_is_not_registered(self) -> None:
        # datasets 表没有操作者列、也没有上传文件链路 → 归属无法确认
        self._insert_dataset("ds-unknown")
        self.assertIsNone(resources.ensure("datasets", "ds-unknown"))
        self.assertIsNone(resources.ensure_kind("dataset", "ds-unknown"))

    def test_project_comes_from_upload_link(self) -> None:
        project = self._project(self.owner)
        self._insert_dataset("ds-linked")
        self._insert_upload(2, project["id"], "ds-linked")
        view = resources.ensure("datasets", "ds-linked")
        self.assertEqual(view["project_id"], project["id"])
        self.assertEqual(view["owner_id"], 2)

    def test_revision_bump_on_state_change(self) -> None:
        self._insert_dataset("ds-2")
        view = resources.ensure("datasets", "ds-2", owner_id=2)
        self.assertEqual(view["revision"], 1)
        self.assertEqual(resources.bump("datasets", "ds-2"), 2)
        self.assertEqual(resources.bump("datasets", "ds-2"), 3)

    def test_state_change_ref_bumps_only_existing(self) -> None:
        self._insert_dataset("ds-3")
        resources.ensure("datasets", "ds-3", owner_id=2)
        refs = [{"kind": "dataset", "id": "ds-3"}]
        stats = resources.register_refs(2, refs, state_changed=True)
        self.assertEqual(stats["existing"], 1)
        self.assertEqual(stats["bumped"], 1)
        self.assertEqual(refs[0]["version"], "2")

        new_refs = [{"kind": "dataset", "id": "ds-new"}]
        self._insert_dataset("ds-new")
        stats = resources.register_refs(2, new_refs, state_changed=True)
        self.assertEqual(stats["registered"], 1)
        self.assertEqual(stats["bumped"], 0)

    def test_registration_failure_is_verifiable_and_retryable(self) -> None:
        # 业务还没真正落库时登记失败 → 留痕（不能凭空登记一条资源）
        stats = resources.register_refs(2, [{"kind": "dataset", "id": "ds-late"}])
        self.assertEqual(stats["failed"], 1)
        pending = resources.list_pending_failures()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["source_id"], "ds-late")

        # 业务行出现后重试：补上真实归属来源，登记成功并销账
        self._insert_dataset("ds-late")
        project = self._project(self.owner)
        self._insert_upload(2, project["id"], "ds-late")
        retried = resources.retry_pending()
        self.assertEqual(retried["registered"], 1)
        self.assertEqual(resources.list_pending_failures(), [])


# --------------------------------------------------------------------------- #
# 2) 可见性与来源认领
# --------------------------------------------------------------------------- #
class VisibilityTest(_Base):
    def test_existing_source_is_not_returned_to_another_user(self) -> None:
        project = self._project(self.owner)
        other_project = self._project(self.other, "别人的项目")
        self._insert_dataset("ds-own")
        first = permissions.register_resource(
            ActorContext(user_id=2, roles=("user",), source="system"),
            project_id=project["id"],
            kind="dataset",
            display_name="我的数据集",
            source_table="datasets",
            source_id="ds-own",
        )
        self.assertEqual(first["owner_id"], 2)

        with self.assertRaises(Exception) as caught:
            permissions.register_resource(
                ActorContext(user_id=3, roles=("user",), source="system"),
                project_id=other_project["id"],
                kind="dataset",
                display_name="蹭一下",
                source_table="datasets",
                source_id="ds-own",
            )
        self.assertEqual(getattr(caught.exception, "code", ""), "AGENT_RESOURCE_NOT_FOUND")

    def test_http_cannot_claim_business_source(self) -> None:
        """HTTP 边界拦截来源声明：客户端不能把业务行认领成自己的资源。"""
        from agent.registry import register_agent, reset_registration_cache
        from utils.jwt_util import generate_token

        reset_registration_cache()
        register_agent(self.app)
        project = self._project(self.owner)
        client = self.app.test_client()
        response = client.post(
            f"/api/agent/projects/{project['id']}/resources",
            json={
                "kind": "dataset",
                "display_name": "认领别人的数据集",
                "source_table": "datasets",
                "source_id": "ds-other-user",
            },
            headers={"Authorization": f"Bearer {generate_token(2)}"},
        )
        self.assertEqual(response.status_code, 403, response.get_data(as_text=True))

    def test_access_check_matrix(self) -> None:
        self._insert_dataset("ds-acl")
        resources.ensure("datasets", "ds-acl", owner_id=2)

        self.assertEqual(resources.access_check("dataset", "ds-acl", 2, "read")["owner_id"], 2)
        with self.assertRaises(PermissionError):
            resources.access_check("dataset", "ds-acl", 3, "read")
        self.assertIsNotNone(resources.access_check("dataset", "ds-acl", 1, "write"))
        with self.assertRaises(LookupError):
            resources.access_check("dataset", "ds-missing", 2, "read")

    def test_legacy_manifest_lists_shared_and_unregistered(self) -> None:
        self._insert_dataset("default-yolo")
        self._insert_dataset("ds-unregistered")
        system_actor = ActorContext(user_id=1, roles=("admin", "user"), source="system")
        project = permissions.create_project(system_actor, "系统默认资源")
        resources.ensure(
            "datasets",
            "default-yolo",
            owner_id=1,
            project_id=project["id"],
            access_policy=permissions.POLICY_SHARED_READONLY,
        )
        manifest = legacy.system_default_manifest()
        shared_ids = {entry["source_id"] for entry in manifest["shared"]}
        self.assertIn("default-yolo", shared_ids)
        self.assertIn("dataset", manifest["unregistered"])
        self.assertGreaterEqual(manifest["unregistered"]["dataset"], 1)

    def test_shared_readonly_allows_read_not_write(self) -> None:
        system_actor = ActorContext(user_id=1, roles=("admin", "user"), source="system")
        project = permissions.create_project(system_actor, "系统默认资源")
        self._insert_dataset("default-shared")
        resources.ensure(
            "datasets",
            "default-shared",
            owner_id=1,
            project_id=project["id"],
            access_policy=permissions.POLICY_SHARED_READONLY,
        )
        self.assertIsNotNone(
            resources.access_check("dataset", "default-shared", 3, "read")
        )
        with self.assertRaises(PermissionError):
            resources.access_check("dataset", "default-shared", 3, "write")


# --------------------------------------------------------------------------- #
# 3) 权限桥
# --------------------------------------------------------------------------- #
class BridgePolicyTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.policy = AgentPolicyBridge()

    def test_unknown_action_fails_closed(self) -> None:
        with self.assertRaises(PermissionError):
            self.policy.require_access(2, "dataset:teleport", None)

    def test_account_level_action_only_checks_account(self) -> None:
        self.assertTrue(self.policy.require_access(2, "dataset:create", None))
        with self.assertRaises(PermissionError):
            self.policy.require_access(999, "dataset:create", None)

    def test_resolve_registers_unregistered_row_with_real_owner(self) -> None:
        self._insert_record("rec-bridge", 2)
        snapshot = self.policy.resolve_resource(2, "record", "rec-bridge")
        self.assertEqual(snapshot.id, "rec-bridge")
        self.assertEqual(snapshot.owner_id, 2)
        # 别人的记录仍按"不存在"处理
        with self.assertRaises(PermissionError):
            self.policy.resolve_resource(3, "record", "rec-bridge")

    def test_resolve_rejects_undeclared_kind(self) -> None:
        with self.assertRaises(PermissionError):
            self.policy.resolve_resource(2, "galaxy", "x")

    def test_project_file_access_follows_project_policy(self) -> None:
        project = self._project(self.owner)
        self.assertTrue(
            self.policy.project_file_access(2, project["id"], "file:create")
        )
        self.assertFalse(
            self.policy.project_file_access(3, project["id"], "file:create")
        )
        self.assertFalse(
            self.policy.project_file_access(3, project["id"], "file:read")
        )

        system_actor = ActorContext(user_id=1, roles=("admin", "user"), source="system")
        shared = permissions.create_project(
            system_actor, "系统默认资源", origin=permissions.ORIGIN_SYSTEM
        )
        self.assertTrue(self.policy.project_file_access(3, shared["id"], "file:read"))
        self.assertFalse(self.policy.project_file_access(3, shared["id"], "file:create"))

    def test_project_file_access_denies_unknown_project(self) -> None:
        self.assertFalse(self.policy.project_file_access(2, "prj_missing", "file:read"))
        self.assertFalse(self.policy.project_file_access(2, "", "file:read"))


# --------------------------------------------------------------------------- #
# 4) 原页面
# --------------------------------------------------------------------------- #
class PageGuardTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        import routes.dataset as dataset_routes
        from utils.jwt_util import generate_token

        self.dataset_routes = dataset_routes
        self._db_backup = dataset_routes.db
        self.token = generate_token
        dataset_routes.db = self.database
        self.addCleanup(self._restore_db)
        self.app.register_blueprint(dataset_routes.dataset_bp)
        self.client = self.app.test_client()

    def _restore_db(self) -> None:
        self.dataset_routes.db = self._db_backup

    def _headers(self, user_id: int) -> dict:
        return {"Authorization": f"Bearer {self.token(user_id)}"}

    @staticmethod
    def _code(response) -> int:
        """C 的响应体固定 HTTP 200，业务码在 ``body['code']`` 里。"""
        payload = response.get_json() or {}
        return int(payload.get("code") or 0)

    def test_owner_can_read_own_dataset_and_others_cannot(self) -> None:
        self._insert_dataset("ds-page", "页面上传的数据集")
        resources.ensure("datasets", "ds-page", owner_id=2)

        ok = self.client.get("/api/dataset/ds-page", headers=self._headers(2))
        self.assertEqual(self._code(ok), 200, ok.get_data(as_text=True))

        denied = self.client.get("/api/dataset/ds-page", headers=self._headers(3))
        self.assertEqual(self._code(denied), 403, denied.get_data(as_text=True))

    def test_admin_still_manages_legacy_rows(self) -> None:
        self._insert_dataset("ds-legacy")
        response = self.client.get("/api/dataset/ds-legacy", headers=self._headers(1))
        self.assertEqual(self._code(response), 200, response.get_data(as_text=True))

    def test_missing_dataset_is_404(self) -> None:
        response = self.client.get("/api/dataset/ds-none", headers=self._headers(2))
        self.assertEqual(self._code(response), 404)

    def test_unregistered_dataset_is_not_public(self) -> None:
        self._insert_dataset("ds-unowned")
        response = self.client.get("/api/dataset/ds-unowned", headers=self._headers(3))
        self.assertEqual(self._code(response), 403)

    def test_dataset_list_is_filtered_per_user(self) -> None:
        self._insert_dataset("ds-mine", "我的")
        self._insert_dataset("ds-other", "别人的")
        resources.ensure("datasets", "ds-mine", owner_id=2)
        resources.ensure("datasets", "ds-other", owner_id=3)

        response = self.client.get("/api/dataset/list", headers=self._headers(2))
        self.assertEqual(self._code(response), 200, response.get_data(as_text=True))
        payload = response.get_json() or {}
        ids = {item["id"] for item in (payload.get("data") or {}).get("datasets", [])}
        self.assertIn("ds-mine", ids)
        self.assertNotIn("ds-other", ids)

    def test_successful_write_bumps_revision(self) -> None:
        self._insert_dataset("ds-write", "旧名字")
        resources.ensure("datasets", "ds-write", owner_id=2)
        before = resources.ensure("datasets", "ds-write")["revision"]

        response = self.client.put(
            "/api/dataset/ds-write",
            json={"name": "新名字", "description": "改一下"},
            headers=self._headers(2),
        )
        self.assertEqual(self._code(response), 200, response.get_data(as_text=True))
        after = resources.ensure("datasets", "ds-write")["revision"]
        self.assertEqual(after, before + 1)

    def test_new_draft_is_visible_to_creator_only(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO dataset_drafts (id, name, creator_id, source_type, "
                "dataset_mode, primary_modality, workflow_status, metadata_json, "
                "create_time, update_time) VALUES ('draft-new', '未登记草稿', 2, 'video', "
                "'single', 'rgb', 'ready', '{}', '2026-01-01T00:00:00', "
                "'2026-01-01T00:00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        mine = self.client.get(
            "/api/dataset/list?record_kind=draft", headers=self._headers(2)
        )
        ids = {
            item["id"]
            for item in ((mine.get_json() or {}).get("data") or {}).get("datasets", [])
        }
        self.assertIn("draft-new", ids)

        others = self.client.get(
            "/api/dataset/list?record_kind=draft", headers=self._headers(3)
        )
        other_ids = {
            item["id"]
            for item in ((others.get_json() or {}).get("data") or {}).get("datasets", [])
        }
        self.assertNotIn("draft-new", other_ids)

        # 首次访问即按真实归属补登记
        self.assertIsNotNone(resources.ensure_kind("draft", "draft-new"))


if __name__ == "__main__":
    unittest.main()

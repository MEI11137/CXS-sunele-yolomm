"""WB17 收尾批次3：Agent 逐动作覆盖（D）与单 IR 语义（E）。

每条用例都对应一个真实用户动作，并且用**真实入口**验证：

* 工具网关按 ``full`` 直接执行（不出确认卡），页面 HTTP 读同一个库看到同一结果；
* 第二个普通账号即使拿到自己的令牌，也动不了别人的数据集；
* 删除当前项目/会话后，后续工具调用立刻被拒；
* 回收站往返（delete → 列表不见 → 回收站可见 → restore）对数据集、资料、手册、
  会话、回流草稿都成立，且**原文件不删**；
* 记录查询的人工状态/结论版本/反馈状态/文件名/主模态，页面与工具完全一致；
* 单 IR 数据集：导入 → data.yaml / 导出 ZIP → 训练分派 → 记录字段全程是 ir，
  不把 IR 当 RGB，也不给 IR 造一张 RGB 替身；
* 真实 YOLOMM 权重（有路由/metadata）不会被误拒，纯单模态权重也不能冒充双模态。

重量级真实权重用例在文件不存在时显式跳过，绝不伪造结论。
"""

from __future__ import annotations

import io
import json
import secrets
import unittest
import zipfile
from pathlib import Path

import requests
import yaml

from backend.agent import identity as agent_identity
from backend.agent import store
from backend.agent import tools as agent_tools
from backend.agent.gateway import TOKEN_HEADER
from backend.errors import ApiError
from backend.services import datasets as dataset_service
from backend.services import documents as document_service
from backend.services import reflow as reflow_service
from backend.services import sessions as session_service
from backend.services import storage
from backend.services import training as training_service
from backend.vision import runtime as vision_runtime

from backend.tests.support import body, csrf_headers
from backend.tests.test_business_wb17_03 import png_bytes
from backend.tests.test_business_wb17_05_matrix import MatrixBase
from backend.tests.weights import write_mm_weight, write_real_weight

REAL_YOLOMM = Path("var/acceptance-20261104/workspace/weights/best.pt")


class AgentCallBase(MatrixBase):
    """真实工具网关：按生产顺序先开一轮，再签发会话级令牌。"""

    def gateway(self):
        return self.harness.app.config["HX17_AGENT"].gateway

    def other_account(self, name: str = "bob") -> tuple[dict, dict, dict]:
        """第二个**普通**账号（非管理员），有自己独立的目录、项目与会话。"""
        user = self.harness.create_user(name)
        root = self.harness.make_dir(f"{name}-root")
        self.harness.add_root(root, grant_to=[name])
        project = self.harness.create_project(user["id"], f"{name} 的项目")
        self.harness.bind_project(project["id"], root, name)
        session = self.harness.create_session(project["id"], user["id"])
        self.harness.save_connection(user["id"])
        return user, project, session

    def open_turn(self, *, mode: str = "full", user=None, project=None, session=None) -> str:
        user = user or self.alice
        project = project or self.project
        session = session or self.session
        conn = self.conn()
        try:
            connection = agent_identity.connection_for(conn, int(user["id"]))
            fingerprint = agent_identity.connection_fingerprint(connection)
            turn, _ = store.create(
                conn,
                session_id=str(session["id"]),
                project_id=str(project["id"]),
                owner_id=int(user["id"]),
                request_id=f"req-{secrets.token_hex(8)}",
                mode=mode,
                provider=str(connection["provider"]),
                model=str(connection["model"]),
                fingerprint=fingerprint,
            )
            store.mark_running(conn, turn["id"])
        finally:
            conn.close()
        return self.gateway().issue_session(
            user_id=int(user["id"]),
            session_id=str(session["id"]),
            project_id=str(project["id"]),
            fingerprint=fingerprint,
        )

    def call(self, token: str, tool: str, args: dict | None = None, *, call_id: str | None = None):
        return requests.post(
            f"{self.gateway().base_url}/v1/tools/{tool}",
            headers={TOKEN_HEADER: token},
            json={"args": args or {}, "call_id": call_id or f"call_{secrets.token_hex(8)}"},
            timeout=60,
        )

    def ok_call(self, token: str, tool: str, args: dict | None = None) -> dict:
        response = self.call(token, tool, args)
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["data"]["status"], "ok", payload)
        return payload["data"]["result"]

    # ---- 造真实单 IR 数据集目录

    def ir_source_dir(self, name: str = "ir-ds") -> Path:
        base = self.root / name
        for split, stem in (("train", "a"), ("val", "b")):
            (base / "images" / split).mkdir(parents=True, exist_ok=True)
            (base / "labels" / split).mkdir(parents=True, exist_ok=True)
            (base / "images" / split / f"{stem}.png").write_bytes(
                png_bytes(color=(40, 40 + len(split), 200))
            )
            (base / "labels" / split / f"{stem}.txt").write_text(
                "0 0.5 0.5 0.2 0.2\n", encoding="utf-8"
            )
        (base / "data.yaml").write_text(
            yaml.safe_dump(
                {
                    "path": ".",
                    "nc": 2,
                    "names": ["单一热斑", "大面积热斑"],
                    "train": "images/train",
                    "val": "images/val",
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        return base


# --------------------------------------------------------------------- full 语义


class FullModeTest(AgentCallBase):
    def test_full_mode_runs_directly_and_page_reads_the_same_row(self):
        dataset = self.dataset()
        token = self.open_turn(mode="full")

        result = self.ok_call(token, "hx_dataset_admin", {"action": "delete", "dataset_id": dataset["id"]})
        self.assertEqual(result["status"], "deleted")
        self.assertNotIn("action_id", result, "full 模式直接执行，不该出现确认卡")

        page = self.client.get(f"/api/projects/{self.project['id']}/datasets")
        self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
        self.assertNotIn(dataset["id"], [item["id"] for item in body(page)["data"]["datasets"]])

        trash = self.client.get(f"/api/projects/{self.project['id']}/datasets?deleted=only")
        self.assertIn(dataset["id"], [item["id"] for item in body(trash)["data"]["datasets"]])
        # 软删只移入回收站：目录与样本文件都还在
        self.assertTrue((dataset["root"] / "images" / "train" / "s0.png").is_file())

        again = self.ok_call(token, "hx_dataset_admin", {"action": "delete", "dataset_id": dataset["id"]})
        self.assertEqual(again["status"], "unchanged", "重复删除不能造第二份状态")

        restored = self.ok_call(token, "hx_dataset_admin", {"action": "restore", "dataset_id": dataset["id"]})
        self.assertEqual(restored.get("status", "restored"), "restored")
        page = self.client.get(f"/api/projects/{self.project['id']}/datasets")
        self.assertIn(dataset["id"], [item["id"] for item in body(page)["data"]["datasets"]])

    def test_second_account_cannot_delete_someone_elses_dataset(self):
        dataset = self.dataset()
        user, project, session = self.other_account()
        bob = self.open_turn(user=user, project=project, session=session)

        response = self.call(bob, "hx_dataset_admin", {"action": "delete", "dataset_id": dataset["id"]})
        payload = response.json()
        self.assertEqual(payload["data"]["status"], "error", payload)
        self.assertEqual(payload["data"]["error"]["code"], "DATASET_NOT_FOUND")

        bob_page = self.harness.client()
        from backend.tests.support import login

        login(bob_page, str(user["username"]))
        # 别人的项目也读不到：页面侧同样是 403/404，而不是空列表冒充“没有数据”
        page = bob_page.get(f"/api/projects/{self.project['id']}/datasets")
        self.assertIn(page.status_code, (403, 404), page.get_data(as_text=True))

        conn = self.conn()
        try:
            row = conn.execute("SELECT deleted_at FROM datasets WHERE id = ?", (dataset["id"],)).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row["deleted_at"], "越权调用不能改到数据")

    def test_deleting_current_project_blocks_following_calls(self):
        token = self.open_turn(mode="full")
        result = self.ok_call(token, "hx_project_admin", {"action": "delete"})
        self.assertTrue(result["current_project"])

        after = self.call(token, "hx_list_sessions", {})
        self.assertEqual(after.status_code, 409, after.text)
        self.assertEqual(after.json()["error"]["code"], "PROJECT_CLOSED")

        page = self.client.get("/api/projects")
        self.assertNotIn(self.project["id"], [item["id"] for item in body(page)["data"]["projects"]])
        trash = self.client.get("/api/projects?trash=1")
        self.assertIn(self.project["id"], [item["id"] for item in body(trash)["data"]["projects"]])

    def test_session_delete_ends_the_turn_and_history_stays_readable(self):
        conn = self.conn()
        try:
            session_service.append_message(
                conn, session_id=self.session["id"], role="user", content="大面积热斑的复核口径"
            )
        finally:
            conn.close()

        token = self.open_turn(mode="full")
        conn = self.conn()
        try:
            history = agent_tools.TOOLS["hx_session_history"].handler(self._ctx(conn, {}))
            found = agent_tools.TOOLS["hx_search_sessions"].handler(
                self._ctx(conn, {"query": "大面积热斑"})
            )
        finally:
            conn.close()
        self.assertTrue(any("大面积热斑" in item["content"] for item in history["messages"]))
        self.assertTrue(found["matches"])
        self.assertEqual(found["matches"][0]["session_id"], self.session["id"])

        result = self.ok_call(token, "hx_session_admin", {"action": "delete"})
        self.assertTrue(result["current_session"])
        after = self.call(token, "hx_list_sessions", {})
        self.assertEqual(after.status_code, 409, after.text)
        self.assertEqual(after.json()["error"]["code"], "SESSION_CLOSED")

        page = self.client.get(f"/api/projects/{self.project['id']}/sessions")
        self.assertNotIn(self.session["id"], [item["id"] for item in body(page)["data"]["sessions"]])
        trash = self.client.get(f"/api/projects/{self.project['id']}/sessions?trash=1")
        self.assertIn(self.session["id"], [item["id"] for item in body(trash)["data"]["sessions"]])

        conn = self.conn()
        try:
            # 软删的会话不再出现在搜索里，历史也没被删
            self.assertEqual(
                session_service.search_messages(conn, self.alice["id"], self.project["id"], "大面积")["matches"],
                [],
            )
            self.assertTrue(
                conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?", (self.session["id"],)
                ).fetchone()[0]
            )
        finally:
            conn.close()

        restored = self.client.post(
            f"/api/sessions/{self.session['id']}/restore", headers=csrf_headers(self.client)
        )
        self.assertEqual(restored.status_code, 200, restored.get_data(as_text=True))


# ------------------------------------------------------------------ 回收站往返


class TrashRoundTripTest(AgentCallBase):
    def test_manual_and_document_round_trip_through_tool_and_page(self):
        token = self.open_turn(mode="full")

        (self.root / "巡检说明.md").write_text("# 巡检说明\n热斑判定口径。\n", encoding="utf-8")
        document = self.ok_call(token, "hx_document_admin", {"action": "import", "rel_path": "巡检说明.md"})[
            "document"
        ]
        located = self.ok_call(token, "hx_document_admin", {"action": "locate", "document_id": document["id"]})
        self.assertEqual(located["document"]["id"], document["id"])
        self.assertGreater(located["size"], 0)
        self.assertTrue(located["sha256"])
        self.assertTrue((self.root / located["project_relative_path"]).is_file())

        deleted_doc = self.ok_call(
            token, "hx_document_admin", {"action": "delete", "document_id": document["id"]}
        )
        self.assertTrue(deleted_doc["deleted_at"])
        self.assertTrue(
            (self.root / located["project_relative_path"]).is_file(), "删资料不能删原文件"
        )
        page = self.client.get(f"/api/projects/{self.project['id']}/documents")
        self.assertNotIn(document["id"], [item["id"] for item in body(page)["data"]["documents"]])
        restored_doc = self.ok_call(
            token, "hx_document_admin", {"action": "restore", "document_id": document["id"]}
        )
        self.assertIsNone(restored_doc["deleted_at"])

        manual = self.ok_call(
            token,
            "hx_manual_admin",
            {"action": "create", "name": "我的口径", "content": "热斑按面积分两级。"},
        )["manual"]
        updated = self.ok_call(
            token, "hx_manual_admin", {"action": "update", "manual_id": manual["id"], "content": "改了第二版。"}
        )
        self.assertGreaterEqual(int(updated["active_version"]), 2)
        self.ok_call(token, "hx_manual_admin", {"action": "delete", "manual_id": manual["id"]})

        page = self.client.get(f"/api/projects/{self.project['id']}/manuals")
        self.assertNotIn(manual["id"], [item["id"] for item in body(page)["data"]["manuals"]])
        trash = self.client.get(f"/api/projects/{self.project['id']}/manuals?trash=1")
        self.assertIn(manual["id"], [item["id"] for item in body(trash)["data"]["manuals"]])

        restored = self.client.post(
            f"/api/projects/{self.project['id']}/manuals/{manual['id']}/restore",
            headers=csrf_headers(self.client),
        )
        self.assertEqual(restored.status_code, 200, restored.get_data(as_text=True))
        page = self.client.get(f"/api/projects/{self.project['id']}/manuals")
        self.assertIn(manual["id"], [item["id"] for item in body(page)["data"]["manuals"]])

    def test_builtin_manual_stays_read_only(self):
        token = self.open_turn(mode="full")
        conn = self.conn()
        try:
            builtin = conn.execute("SELECT id FROM manuals WHERE builtin = 1 LIMIT 1").fetchone()
        finally:
            conn.close()
        response = self.call(
            token, "hx_manual_admin", {"action": "delete", "manual_id": str(builtin["id"])}
        )
        self.assertEqual(response.json()["data"]["error"]["code"], "MANUAL_READONLY")


# --------------------------------------------------------- 记录与反馈字段一致性


class RecordFieldParityTest(AgentCallBase):
    def _record_with_review(self) -> dict:
        record = self.record()
        self.confirm(record["id"])
        return record

    def test_page_and_tools_report_the_same_fields(self):
        record = self._record_with_review()

        page = self.client.get(f"/api/projects/{self.project['id']}/detections")
        self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
        page_item = next(
            item for item in body(page)["data"]["records"] if item["id"] == record["id"]
        )
        conn = self.conn()
        try:
            tool = agent_tools.TOOLS["hx_list_records"].handler(self._ctx(conn, {"limit": 20}))
            state = agent_tools.TOOLS["hx_review_state"].handler(
                self._ctx(conn, {"record_id": record["id"]})
            )
        finally:
            conn.close()
        tool_item = next(item for item in tool["records"] if item["id"] == record["id"])

        for key in ("human_status", "review_version", "feedback_status", "primary_modality", "rgb_filename"):
            self.assertEqual(tool_item.get(key), page_item.get(key), key)
        self.assertEqual(tool_item["human_status"], "confirmed")
        self.assertEqual(tool_item["review_version"], 1)
        self.assertEqual(state["human_status"], page_item["human_status"])
        self.assertEqual(state["review_version"], page_item["review_version"])
        self.assertEqual(state["filename"], page_item["rgb_filename"])
        self.assertEqual(state["primary_modality"], page_item["primary_modality"])

    def test_feedback_pool_ids_come_from_the_real_list(self):
        record = self._record_with_review()
        token = self.open_turn(mode="full")
        created = self.ok_call(
            token,
            "hx_add_feedback",
            {"record_id": record["id"], "note": "命中偏大", "request_key": "fb-0001"},
        )
        self.assertTrue(created["created"])

        conn = self.conn()
        try:
            listed = agent_tools.TOOLS["hx_list_feedback"].handler(self._ctx(conn, {"status": "pending"}))
        finally:
            conn.close()
        self.assertEqual([item["id"] for item in listed["feedback"]], [created["feedback"]["id"]])

        page = self.client.get(f"/api/projects/{self.project['id']}/feedback?status=pending")
        self.assertEqual(
            [item["id"] for item in body(page)["data"]["feedback"]],
            [created["feedback"]["id"]],
        )


# --------------------------------------------------------------------- 回流草稿


class ReflowDraftToolTest(AgentCallBase):
    def _distinct_records(self) -> list[dict]:
        """两张**内容不同**的已确认记录：同字节的图会被身份检查当成重复。"""
        model = self.make_model()
        records = []
        for index, color in enumerate(((10, 20, 30), (200, 120, 60))):
            asset = self.upload_asset(png_bytes(color=color), f"draft-{index}.png", role="rgb")
            records.append(self.detect(model, asset["id"], key=f"draft-det-{index}"))
        for index, record in enumerate(records):
            self.confirm(record["id"], key=f"s-draft-{index}")
        return records

    def test_draft_list_read_delete_and_restore(self):
        first, second = self._distinct_records()
        selections = [
            {"record_id": first["id"], "action": "append", "split": "train"},
            {"record_id": second["id"], "action": "append", "split": "val"},
        ]
        token = self.open_turn(mode="full")
        draft = self.ok_call(
            token,
            "hx_reflow_draft",
            {"action": "save", "name": "批次草稿", "mode": "new", "selections": selections},
        )
        self.assertTrue(draft["id"])

        listed = self.ok_call(token, "hx_reflow_draft", {"action": "list"})
        self.assertEqual([item["id"] for item in listed["drafts"]], [draft["id"]])

        read = self.ok_call(token, "hx_reflow_draft", {"action": "read", "draft_id": draft["id"]})
        self.assertEqual(read["id"], draft["id"])
        self.assertEqual(read["plan"]["counts"]["append"], 2)

        page = self.client.get(f"/api/projects/{self.project['id']}/reflow/drafts")
        self.assertEqual([item["id"] for item in body(page)["data"]["drafts"]], [draft["id"]])

        deleted = self.ok_call(
            token, "hx_reflow_draft", {"action": "delete", "draft_id": draft["id"]}
        )
        self.assertEqual(deleted["status"], "deleted")
        listed = self.ok_call(token, "hx_reflow_draft", {"action": "list"})
        self.assertEqual(listed["drafts"], [])
        with_trash = self.ok_call(
            token, "hx_reflow_draft", {"action": "list", "include_deleted": True}
        )
        self.assertIn(draft["id"], [item["id"] for item in with_trash["drafts"]])

        restored = self.ok_call(
            token, "hx_reflow_draft", {"action": "restore", "draft_id": draft["id"]}
        )
        self.assertIn(restored["status"], {"ready", "editing"})
        listed = self.ok_call(token, "hx_reflow_draft", {"action": "list"})
        self.assertEqual([item["id"] for item in listed["drafts"]], [draft["id"]])


# ------------------------------------------------------------------ 单 IR 语义


class SingleIrTest(AgentCallBase):
    def import_ir(self) -> dict:
        response = self.client.post(
            f"/api/projects/{self.project['id']}/datasets",
            json={"name": "红外集", "rel_path": "ir-ds", "modalities": ["ir"]},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return body(response)["data"]["dataset"]

    def test_single_ir_import_export_and_training_dispatch(self):
        self._ir_dataset_and_record()

    def _ir_dataset_and_record(self) -> tuple[dict, dict]:
        """导入单 IR 数据集、导出、训练分派、记录/复核字段，一条链走完并逐项断言。"""
        self.ir_source_dir()
        dataset = self.import_ir()
        self.assertEqual(dataset["modalities"], ["ir"])
        self.assertEqual(dataset["primary_modality"], "ir")
        self.assertIsNone(dataset["secondary_modality"])
        self.assertEqual(dataset["sample_count"], 2)

        # 导出的 ZIP 里 data.yaml 如实写 ir → images，不出现 rgb
        token = self.open_turn(mode="full")
        exported = self.ok_call(token, "hx_export_dataset", {"dataset_id": dataset["id"]})
        self.assertGreater(exported["bytes"], 0)
        download = self.client.get(
            f"/api/projects/{self.project['id']}/datasets/{dataset['id']}/export"
        )
        self.assertEqual(download.status_code, 200, "导出包没生成")
        raw = download.get_data()  # 二进制，绝不能按文本解码
        download.close()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            # 导入的数据集把原始目录放在 raw/ 下，配置里 path: raw 指回来；
            # 根目录这份 data.yaml 才是服务重写的那份。
            config = yaml.safe_load(archive.read("data.yaml"))
            self.assertEqual(config["path"], "raw")
            self.assertEqual(config["modalities"], {"ir": "images"})
            self.assertEqual(config["modality_used"], ["ir"])
            self.assertTrue(any(name.endswith("images/train/a.png") for name in names), names)
            self.assertTrue(any(name.endswith("labels/train/a.txt") for name in names), names)

        conn = self.conn()
        try:
            row = dataset_service.get_owned(
                conn, self.alice["id"], self.project["id"], dataset["id"]
            )
            family, secondary = training_service._family_for(
                conn, self.alice["id"], self.project["id"], row, None
            )
            dataset_yaml = Path(storage.resolve_rel(self.root, row["root_rel"])) / "data.yaml"
        finally:
            conn.close()
        self.assertEqual((family, secondary), ("yolo", None), "单 IR 不能分派成双模态训练")

        run_dir = storage.project_dir(self.root, "training", "ir-probe")
        runtime_yaml = training_service.generate_runtime_config(
            run_dir, dataset_yaml, family=family, secondary=secondary
        )
        runtime = yaml.safe_load(runtime_yaml.read_text(encoding="utf-8"))
        self.assertEqual(runtime["modalities"], {"ir": "images"})
        self.assertEqual(runtime["modality_used"], ["ir"])

        # 巡检/记录侧：模型绑定这份单 IR 数据集，记录的主模态必须是 ir
        model = self.make_model()
        conn = self.conn()
        try:
            conn.execute(
                "UPDATE models SET dataset_id = ? WHERE id = ?", (dataset["id"], model["id"])
            )
            conn.commit()
        finally:
            conn.close()
        record = self.detect(
            model, self.upload_asset(png_bytes(), "ir-record.png", role="rgb")["id"], key="ir-det"
        )
        page = self.client.get(f"/api/projects/{self.project['id']}/detections")
        page_item = next(item for item in body(page)["data"]["records"] if item["id"] == record["id"])
        self.assertEqual(page_item["primary_modality"], "ir")

        # 复核页/复核工具读到的主图模态也必须是 ir（不能让复核以为这是 RGB 原图）
        conn = self.conn()
        try:
            state = agent_tools.TOOLS["hx_review_state"].handler(
                self._ctx(conn, {"record_id": record["id"]})
            )
        finally:
            conn.close()
        self.assertEqual(state["primary_modality"], "ir")

        return dataset, record

    def test_reflow_from_ir_record_keeps_ir(self):
        dataset, record = self._ir_dataset_and_record()
        self.confirm(record["id"], key="s-ir")

        conn = self.conn()
        try:
            plan = reflow_service.preview(
                conn,
                self.alice["id"],
                self.project["id"],
                mode="new",
                dataset_id=None,
                selections=[{"record_id": record["id"], "action": "append", "split": "train"}],
            )
            self.assertEqual(plan["counts"]["append"], 1, plan)
            created = reflow_service.apply(
                conn,
                self.alice["id"],
                self.project["id"],
                mode="new",
                dataset_id=None,
                selections=[{"record_id": record["id"], "action": "append", "split": "train"}],
                name="红外回流",
                request_key="ir-reflow-1",
            )
        finally:
            conn.close()
        self.assertEqual(created["modalities"], ["ir"])
        self.assertIsNone(created["secondary_modality"])
        root = Path(storage.resolve_rel(self.root, created["root_rel"]))
        config = yaml.safe_load((root / "data.yaml").read_text(encoding="utf-8"))
        self.assertEqual(config["modalities"], {"ir": "images"})
        self.assertEqual(config["modality_used"], ["ir"])
        self.assertTrue((root / "images" / "train").is_dir())

    def test_single_ir_cannot_declare_a_second_modality(self):
        self.ir_source_dir()
        response = self.client.post(
            f"/api/projects/{self.project['id']}/datasets",
            json={
                "name": "假双模态",
                "rel_path": "ir-ds",
                "modalities": ["ir"],
                "secondary_modality": "rgb",
            },
            headers=csrf_headers(self.client),
        )
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        self.assertEqual(body(response)["error"], "VALIDATION")

    def test_pairing_missing_or_broken_ir_is_refused_without_touching_source(self):
        base = self.ir_source_dir()
        (base / "images_ir" / "train").mkdir(parents=True, exist_ok=True)
        response = self.client.post(
            f"/api/projects/{self.project['id']}/datasets",
            json={
                "name": "缺配图",
                "rel_path": "ir-ds",
                "modalities": ["rgb", "ir"],
                "secondary_modality": "ir",
            },
            headers=csrf_headers(self.client),
        )
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(body(response)["error"], "MISSING_MODALITY")

        # 有同名文件但内容坏掉也不算有效配对
        broken = base / "images_ir" / "train" / "a.png"
        broken.write_bytes(b"not-an-image")
        (base / "images_ir" / "val").mkdir(parents=True, exist_ok=True)
        (base / "images_ir" / "val" / "b.png").write_bytes(png_bytes(color=(5, 5, 5)))
        response = self.client.post(
            f"/api/projects/{self.project['id']}/datasets",
            json={
                "name": "坏配图",
                "rel_path": "ir-ds",
                "modalities": ["rgb", "ir"],
                "secondary_modality": "ir",
            },
            headers=csrf_headers(self.client),
        )
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(body(response)["error"], "IMAGE_UNREADABLE")

        # 源目录一个字节都没被改：原始 IR/主图/标注都还在
        self.assertTrue((base / "images" / "train" / "a.png").is_file())
        self.assertTrue((base / "labels" / "train" / "a.txt").is_file())
        self.assertEqual(broken.read_bytes(), b"not-an-image")


# ------------------------------------------------------------- 权重模态识别


class WeightModalityTest(AgentCallBase):
    def test_real_multimodal_weight_is_recognised_not_rejected(self):
        if not REAL_YOLOMM.is_file():
            self.skipTest(f"本机没有真实 YOLOMM 权重：{REAL_YOLOMM}")
        target = self.root / "weights" / "real-mm.pt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(REAL_YOLOMM.read_bytes())

        info = vision_runtime.inspect_weight(target)
        evidence = info["multimodal_evidence"]
        self.assertTrue(info["multimodal"], evidence)
        self.assertEqual(info["secondary_modality"], "ir")
        self.assertEqual(info["primary_modality"], "rgb")
        self.assertTrue(
            evidence["yaml_routing"] or evidence["routing_layers"] or evidence["router"] or evidence["metadata"],
            evidence,
        )

        conn = self.conn()
        try:
            inspected = agent_tools.TOOLS["hx_inspect_weight"].handler(
                self._ctx(conn, {"rel_path": "weights/real-mm.pt"})
            )
        finally:
            conn.close()
        self.assertTrue(inspected["multimodal"])
        self.assertEqual(inspected["suggested_family"], "yolomm")
        self.assertEqual(inspected["primary_modality"], "rgb")
        self.assertEqual(inspected["secondary_modality"], "ir")

        from backend.services import models as model_service

        conn = self.conn()
        try:
            with self.assertRaises(ApiError) as ctx:
                model_service.import_model(
                    conn,
                    self.alice["id"],
                    self.project["id"],
                    name="错标单模态",
                    family="yolo",
                    rel_path="weights/real-mm.pt",
                )
        finally:
            conn.close()
        self.assertEqual(ctx.exception.code, "MODEL_FAMILY_MISMATCH")

        conn = self.conn()
        try:
            model = model_service.import_model(
                conn,
                self.alice["id"],
                self.project["id"],
                name="真实双模态",
                family="yolomm",
                secondary_modality="ir",
                rel_path="weights/real-mm.pt",
            )
        finally:
            conn.close()
        self.assertEqual(model["family"], "yolomm")
        self.assertEqual(model["secondary_modality"], "ir")
        self.assertEqual(model["model_mode"], "multimodal")

    def test_minimal_multimodal_fixture_really_has_routing(self):
        """证据链自检：vendor 默认给每个 DetectionModel 挂 router，
        所以“有 router”不算双模态；真实双模态必须留下 YAML 路由注解与 X 模态 metadata。"""
        write_mm_weight(self.root / "weights" / "tiny-mm.pt")
        info = vision_runtime.inspect_weight(self.root / "weights" / "tiny-mm.pt")
        self.assertTrue(info["multimodal"], info["multimodal_evidence"])
        self.assertEqual(info["primary_modality"], "rgb")
        self.assertEqual(info["secondary_modality"], "ir")
        self.assertGreaterEqual(info["multimodal_evidence"]["routing_annotations"], 2)

    def test_single_modality_weight_cannot_pose_as_multimodal(self):
        write_real_weight(self.root / "weights" / "tiny.pt")
        info = vision_runtime.inspect_weight(self.root / "weights" / "tiny.pt")
        self.assertFalse(info["multimodal"], info["multimodal_evidence"])
        self.assertIsNone(info["secondary_modality"])

        from backend.services import models as model_service

        conn = self.conn()
        try:
            with self.assertRaises(ApiError) as ctx:
                model_service.import_model(
                    conn,
                    self.alice["id"],
                    self.project["id"],
                    name="假双模态",
                    family="yolomm",
                    secondary_modality="ir",
                    rel_path="weights/tiny.pt",
                )
        finally:
            conn.close()
        self.assertEqual(ctx.exception.code, "MODEL_FAMILY_MISMATCH")

        conn = self.conn()
        try:
            model = model_service.import_model(
                conn,
                self.alice["id"],
                self.project["id"],
                name="单模态",
                family="yolo",
                rel_path="weights/tiny.pt",
            )
        finally:
            conn.close()
        self.assertEqual(model["family"], "yolo")
        self.assertIsNone(model["secondary_modality"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

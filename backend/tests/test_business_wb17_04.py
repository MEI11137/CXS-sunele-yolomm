"""WB17-04：人工复核版本、记录操作、数据回流与身份检查。"""

from __future__ import annotations

import io
import json
import secrets
import unittest
import zipfile
from pathlib import Path

import numpy as np
import yaml
from PIL import Image

from backend.agent import identity as agent_identity
from backend.agent import tools as agent_tools
from backend.agent.context import Actor
from backend.agent.tools import ToolContext
from backend.errors import ApiError
from backend.services import assets as asset_service
from backend.services import datasets as dataset_service
from backend.services import detection as detection_service
from backend.services import identity as identity_service
from backend.services import records as record_service
from backend.services import reflow as reflow_service
from backend.services import review as review_service
from backend.services import storage
from backend.vision import runtime as vision_runtime

from backend.tests.support import body, csrf_headers, wait_turn
from backend.tests.test_business_wb17_03 import BusinessBase, StubProvider, png_bytes


class _Detector:
    def __init__(self, boxes):
        self._boxes = boxes

    def predict(self, **kwargs):
        return [type("R", (), {"boxes": self._boxes})()]


class ReviewBase(BusinessBase):
    def setUp(self):
        super().setUp()
        self._original_factory = vision_runtime._DETECTOR_FACTORY
        vision_runtime._DETECTOR_FACTORY = lambda path, mode: _Detector(
            np.array([[8.0, 8.0, 40.0, 30.0, 0.9, 0]])
        )

    def tearDown(self):
        vision_runtime._DETECTOR_FACTORY = self._original_factory
        super().tearDown()

    def record(self, *, key: str = "rec", x: bool = False) -> dict:
        model = self.make_model(mode="multimodal" if x else "single", secondary="ir" if x else None)
        rgb = self.upload_asset(png_bytes(), f"{key}-rgb.png", role="rgb")
        x_id = None
        if x:
            x_asset = self.upload_asset(png_bytes(color=(9, 9, 9)), f"{key}-ir.png", role="ir")
            x_id = x_asset["id"]
        return self.detect(model, rgb["id"], x_id=x_id, key=f"{key}-det")

    def confirm(self, record_id: str, *, boxes=None, outcome="defect", key: str = "s1") -> dict:
        conn = self.conn()
        try:
            version, _ = review_service.submit(
                conn,
                self.alice["id"],
                self.project["id"],
                record_id,
                boxes=boxes
                if boxes is not None
                else [{"class_id": 1, "bbox": [10, 10, 44, 36], "severity": "high"}],
                outcome=outcome,
                base_version=0,
                request_key=key,
            )
        finally:
            conn.close()
        return version


class ReviewFlowTest(ReviewBase):
    def test_http_review_endpoints_share_the_state_envelope(self):
        """页面读 `data.state.*`；GET 不能返回裸 state，否则真实浏览器直接 TypeError。

        这是 WB17-04 联调里复现的故障：GET 返回裸 state，`data.state.draft` 就是
        undefined，图片不加载、保存/提交全禁用。三条返回状态的接口必须同形。
        """
        record = self.record()
        loaded = self.client.get(
            f"/api/projects/{self.project['id']}/detections/{record['id']}/review"
        )
        self.assertEqual(loaded.status_code, 200, loaded.get_data(as_text=True))
        payload = body(loaded)["data"]
        self.assertIn("state", payload)
        self.assertEqual(payload["state"]["record"]["id"], record["id"])
        self.assertIn("rgb_url", payload["state"]["image"])
        self.assertIsNone(payload["state"]["draft"])

        saved = self.client.post(
            f"/api/projects/{self.project['id']}/detections/{record['id']}/review/draft",
            json={"boxes": [], "note": "空标注", "base_version": 0},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        saved_state = body(saved)["data"]
        self.assertIn("state", saved_state)
        self.assertEqual(saved_state["state"]["draft"]["boxes"], [])

        rejected = self.client.post(
            f"/api/projects/{self.project['id']}/detections/{record['id']}/review/reject",
            json={"reason": "误检", "base_version": 0, "request_key": "http-reject"},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(rejected.status_code, 200, rejected.get_data(as_text=True))
        reject_payload = body(rejected)["data"]
        self.assertIn("version", reject_payload)
        self.assertIn("created", reject_payload)
        self.assertEqual(reject_payload["version"]["status"], "rejected")

        reopened = self.client.post(
            f"/api/projects/{self.project['id']}/detections/{record['id']}/review/reopen",
            json={},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        reopen_payload = body(reopened)["data"]
        self.assertIn("state", reopen_payload)
        self.assertIsNotNone(reopen_payload["state"]["draft"])

    def test_submit_creates_immutable_version_and_clears_draft(self):
        record = self.record()
        conn = self.conn()
        try:
            review_service.save_draft(
                conn, self.alice["id"], self.project["id"], record["id"],
                boxes=[{"class_id": 0, "bbox": [5, 6, 20, 25]}], note="草稿", base_version=0,
            )
            version, created = review_service.submit(
                conn, self.alice["id"], self.project["id"], record["id"],
                boxes=[{"class_id": 1, "bbox": [10, 10, 44, 36], "severity": "high"}],
                outcome="defect", note="确认", base_version=0, request_key="submit-1",
            )
            state = review_service.state(conn, self.alice["id"], self.project["id"], record["id"])
        finally:
            conn.close()
        self.assertTrue(created)
        self.assertEqual(version["status"], "confirmed")
        self.assertEqual(version["version"], 1)
        self.assertEqual(version["outcome"], "defect")
        self.assertEqual(version["boxes"][0]["bbox"], [10.0, 10.0, 44.0, 36.0])
        self.assertIsNone(state["draft"])
        self.assertEqual(state["queue"], "confirmed")
        self.assertFalse(state["pending"])

    def test_same_request_key_is_idempotent(self):
        record = self.record()
        first = self.confirm(record["id"], key="same")
        second = self.confirm(record["id"], key="same")
        conn = self.conn()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM review_versions WHERE detection_id = ?", (record["id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(count, 1, "同一请求重试不能反复涨版本")

    def test_version_conflict_keeps_draft_and_returns_409(self):
        record = self.record()
        conn = self.conn()
        try:
            review_service.save_draft(
                conn, self.alice["id"], self.project["id"], record["id"],
                boxes=[{"class_id": 0, "bbox": [4, 4, 30, 30]}], base_version=0,
            )
            # 模拟“另一个页面已经提交”：直接落一条 v1（不经过 submit，避免消费草稿）
            conn.execute(
                "INSERT INTO review_versions (id, project_id, owner_id, detection_id, version,"
                " status, outcome, boxes, class_table, image_width, image_height, created_at,"
                " updated_at, submitted_at) VALUES ('rvw_other', ?, ?, ?, 2, 'confirmed', 'defect',"
                " '[]', '[]', 64, 48, '2026-11-05T00:00:00Z', '2026-11-05T00:00:00Z',"
                " '2026-11-05T00:00:00Z')",
                (self.project["id"], int(self.alice["id"]), record["id"]),
            )
            conn.commit()
            with self.assertRaises(ApiError) as ctx:
                review_service.submit(
                    conn, self.alice["id"], self.project["id"], record["id"],
                    boxes=[{"class_id": 0, "bbox": [4, 4, 30, 30]}], outcome="defect",
                    base_version=0, request_key="stale",
                )
            state = review_service.state(conn, self.alice["id"], self.project["id"], record["id"])
        finally:
            conn.close()
        self.assertEqual(ctx.exception.code, "REVIEW_CONFLICT")
        self.assertEqual(ctx.exception.details["current_version"], 2)
        # 提交失败不丢已有编辑：草稿仍在
        self.assertIsNotNone(state["draft"])
        self.assertEqual(state["draft"]["boxes"][0]["bbox"], [4.0, 4.0, 30.0, 30.0])

    def test_stale_draft_edit_is_rejected_and_latest_edit_survives(self):
        """同一旧基线的第二次保存必须 409：草稿也要有真正的版本前进。"""
        record = self.record()
        conn = self.conn()
        try:
            first = review_service.save_draft(
                conn, self.alice["id"], self.project["id"], record["id"],
                boxes=[{"class_id": 0, "bbox": [4, 4, 30, 30]}], note="第一版", base_version=0,
            )
            stale_base = first["latest"]["version"]
            review_service.save_draft(
                conn, self.alice["id"], self.project["id"], record["id"],
                boxes=[{"class_id": 1, "bbox": [5, 5, 33, 33]}], note="较新的编辑",
                base_version=stale_base,
            )
            with self.assertRaises(ApiError) as ctx:
                review_service.save_draft(
                    conn, self.alice["id"], self.project["id"], record["id"],
                    boxes=[{"class_id": 0, "bbox": [1, 1, 9, 9]}], note="过期编辑",
                    base_version=stale_base,
                )
            state = review_service.state(conn, self.alice["id"], self.project["id"], record["id"])
        finally:
            conn.close()
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "REVIEW_CONFLICT")
        self.assertEqual(state["draft"]["note"], "较新的编辑", "过期保存不能覆盖较新的草稿")
        self.assertEqual(state["draft"]["boxes"][0]["bbox"], [5.0, 5.0, 33.0, 33.0])

    def test_rejected_record_cannot_reuse_older_confirmed_version(self):
        """驳回之后，历史里那条 confirmed 已经失效，不能再拿它加入反馈。"""
        record = self.record(key="reject-fb")
        confirmed = self.confirm(record["id"], key="rf-1")
        conn = self.conn()
        try:
            review_service.reject(
                conn, self.alice["id"], self.project["id"], record["id"],
                reason="框错了", base_version=confirmed["version"], request_key="rf-reject",
            )
            with self.assertRaises(ApiError) as ctx:
                record_service.add_feedback(
                    conn, self.alice["id"], self.project["id"], record["id"], request_key="rf-fb"
                )
        finally:
            conn.close()
        self.assertEqual(ctx.exception.code, "REVIEW_NOT_CONFIRMED")

    def test_reject_leaves_pending_and_reopen_revives(self):
        record = self.record()
        conn = self.conn()
        try:
            review_service.reject(
                conn, self.alice["id"], self.project["id"], record["id"],
                reason="图片不清", base_version=0, request_key="rej-1",
            )
            pending = review_service.queue(conn, self.alice["id"], self.project["id"], view="pending")
            rejected = review_service.queue(conn, self.alice["id"], self.project["id"], view="rejected")
            again, created = review_service.reject(
                conn, self.alice["id"], self.project["id"], record["id"],
                base_version=1, request_key="rej-2",
            )
            reopened = review_service.reopen(
                conn, self.alice["id"], self.project["id"], record["id"], request_key="reopen-1"
            )
        finally:
            conn.close()
        self.assertEqual(pending["counts"]["pending"], 0, "驳回后不能继续留在待复核")
        self.assertEqual(rejected["counts"]["rejected"], 1)
        self.assertFalse(created, "重复驳回不再产生新版本")
        self.assertEqual(again["status"], "rejected")
        self.assertIsNotNone(reopened["draft"])
        self.assertEqual(reopened["draft"]["version"], 2)

    def test_no_defect_needs_no_boxes_and_box_out_of_range_is_rejected(self):
        record = self.record()
        conn = self.conn()
        try:
            version = review_service.submit(
                conn, self.alice["id"], self.project["id"], record["id"],
                boxes=[], outcome="no_defect", base_version=0, request_key="clean",
            )[0]
            with self.assertRaises(ApiError) as ctx:
                review_service.save_draft(
                    conn, self.alice["id"], self.project["id"], record["id"],
                    boxes=[{"class_id": 0, "bbox": [1, 1, 999, 999]}], base_version=1,
                )
        finally:
            conn.close()
        self.assertEqual(version["outcome"], "no_defect")
        self.assertEqual(version["boxes"], [])
        self.assertEqual(ctx.exception.code, "BAD_BOX")

    def test_other_account_cannot_read_or_submit(self):
        record = self.record()
        bob = self.harness.create_user("bob", role="member")
        self.harness.grant_root(int(self.root_row["id"]), "bob")
        project = self.harness.create_project(bob["id"], "bob 项目")
        self.harness.bind_project(project["id"], self.root, "bob")
        conn = self.conn()
        try:
            with self.assertRaises(ApiError) as ctx:
                review_service.state(conn, bob["id"], self.project["id"], record["id"])
        finally:
            conn.close()
        self.assertEqual(ctx.exception.status, 404)


class RecordOpsTest(ReviewBase):
    def test_soft_delete_restore_and_feedback_flow(self):
        record = self.record()
        conn = self.conn()
        try:
            result = record_service.soft_delete(
                conn, self.alice["id"], self.project["id"], [record["id"], "det_missing"]
            )
            listed = detection_service.list_for_project(conn, self.alice["id"], self.project["id"])
            only = detection_service.list_for_project(
                conn, self.alice["id"], self.project["id"], deleted="only"
            )
            with self.assertRaises(ApiError) as ctx:
                record_service.add_feedback(conn, self.alice["id"], self.project["id"], record["id"])
            restored = record_service.restore(
                conn, self.alice["id"], self.project["id"], [record["id"]]
            )
        finally:
            conn.close()
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["items"][1]["status"], "missing", "批量里单项缺失要单独可见")
        self.assertEqual(listed["records"], [])
        self.assertEqual(len(only["records"]), 1)
        self.assertEqual(ctx.exception.code, "RECORD_DELETED")
        self.assertEqual(restored["restored"], 1)

    def test_feedback_requires_confirmed_review_and_is_idempotent(self):
        record = self.record()
        self.confirm(record["id"])
        conn = self.conn()
        try:
            feedback, created = record_service.add_feedback(
                conn, self.alice["id"], self.project["id"], record["id"], request_key="fb-1"
            )
            again, created_again = record_service.add_feedback(
                conn, self.alice["id"], self.project["id"], record["id"]
            )
            pending = record_service.list_feedback(conn, self.alice["id"], self.project["id"])
        finally:
            conn.close()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(feedback["id"], again["id"])
        self.assertEqual(feedback["status"], "pending")
        self.assertEqual(len(pending["feedback"]), 1)


class ReflowTest(ReviewBase):
    def _base_dataset(self, names=("单一热斑", "大面积热斑")):
        """造一个真实文件齐全的最小数据集：1 张 RGB+IR 样本。"""
        conn = self.conn()
        try:
            dataset_id = f"dst_{secrets.token_hex(8)}"
            base = storage.project_dir(self.root, storage.DATASETS, dataset_id)
            for folder in ("images/train", "images_ir/train", "labels/train"):
                storage.project_dir(self.root, storage.DATASETS, dataset_id, *folder.split("/"))
            rgb_bytes = png_bytes()
            storage.write_bytes(
                self.root, base / "images" / "train" / "a.png", rgb_bytes
            )
            storage.write_bytes(
                self.root, base / "images_ir" / "train" / "a.png", png_bytes(color=(7, 7, 7))
            )
            storage.write_bytes(
                self.root, base / "labels" / "train" / "a.txt", b"0 0.5 0.5 0.2 0.2\n"
            )
            storage.write_bytes(
                self.root,
                base / "data.yaml",
                yaml.safe_dump({"names": list(names), "nc": len(names)}).encode("utf-8"),
            )
            identity = identity_service.identity_of_bytes(rgb_bytes)
            conn.execute(
                "INSERT INTO datasets (id, project_id, owner_id, name, modalities, secondary_modality,"
                " classes, root_rel, source, sample_count, split_counts, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'ir', ?, ?, 'local', 1, '{\"train\": 1}', 'ready', ?)",
                (
                    dataset_id,
                    self.project["id"],
                    int(self.alice["id"]),
                    f"底稿-{dataset_id[-4:]}",
                    json.dumps(["rgb", "ir"]),
                    json.dumps(list(names), ensure_ascii=False),
                    storage.relative(self.root, base),
                    "2026-11-05T00:00:00Z",
                ),
            )
            conn.execute(
                "INSERT INTO dataset_samples (id, dataset_id, split, stem, rgb_rel, x_rel, label_rel,"
                " width, height, boxes, class_ids, created_at, sha256, pixel_sha256)"
                " VALUES (?, ?, 'train', 'a', ?, ?, ?, 64, 48, '[{\"class_id\": 0}]', '[0]', ?, ?, ?)",
                (
                    f"smp_{secrets.token_hex(8)}",
                    dataset_id,
                    storage.relative(self.root, base / "images" / "train" / "a.png"),
                    storage.relative(self.root, base / "images_ir" / "train" / "a.png"),
                    storage.relative(self.root, base / "labels" / "train" / "a.txt"),
                    "2026-11-05T00:00:00Z",
                    identity["sha256"],
                    identity["pixel_sha256"],
                ),
            )
            conn.commit()
            return dataset_id, base
        finally:
            conn.close()

    def _record_with_bytes(self, color, key):
        """用与底稿同字节的图建记录（这样身份能对上）。"""
        model = self.make_model()
        asset = self.upload_asset(color, f"{key}.png", role="rgb")
        return self.detect(model, asset["id"], key=f"{key}-det"), asset

    def test_revision_replaces_label_without_adding_samples(self):
        dataset_id, base = self._base_dataset()
        record, _ = self._record_with_bytes(png_bytes(), "same")
        self.confirm(
            record["id"],
            boxes=[{"class_id": 0, "bbox": [4, 4, 30, 30], "severity": "high"}],
        )
        conn = self.conn()
        try:
            plan = reflow_service.preview(
                conn, self.alice["id"], self.project["id"], mode="revision", dataset_id=dataset_id,
                selections=[{"record_id": record["id"], "action": "replace"}],
            )
            self.assertEqual(plan["counts"]["replace"], 1, plan)
            new = reflow_service.apply(
                conn, self.alice["id"], self.project["id"], mode="revision", dataset_id=dataset_id,
                selections=[{"record_id": record["id"], "action": "replace"}],
                name="修订版", request_key="rev-1",
            )
            samples = conn.execute(
                "SELECT * FROM dataset_samples WHERE dataset_id = ?", (new["id"],)
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(new["parent_dataset_id"], dataset_id)
        self.assertEqual(new["kind"], "revision")
        self.assertEqual(new["sample_count"], 1, "回原修订不能增加样本数")
        self.assertEqual(len(samples), 1)
        root = self.root
        label_path = storage.resolve_rel(root, samples[0]["label_rel"], must_exist=True)
        line = label_path.read_text(encoding="utf-8").strip()
        self.assertTrue(line.startswith("0 "), line)
        cx, cy, w, h = (float(value) for value in line.split()[1:])
        self.assertAlmostEqual(cx, 17 / 64, places=5)
        self.assertAlmostEqual(w, 26 / 64, places=5)
        # 真实文件：图片、配图、data.yaml、manifest、ZIP 都在
        for rel in (samples[0]["rgb_rel"], samples[0]["x_rel"], new["manifest_rel"], new["export_rel"]):
            self.assertTrue(storage.resolve_rel(root, rel, must_exist=True).is_file(), rel)
        self.assertTrue((root / new["root_rel"] / "data.yaml").is_file())
        with zipfile.ZipFile(storage.resolve_rel(root, new["export_rel"], must_exist=True)) as archive:
            names = archive.namelist()
        self.assertTrue(any(name.startswith("images/train/") for name in names), names)
        self.assertTrue(any(name.startswith("labels/train/") for name in names), names)

    def test_new_image_in_revision_mode_is_a_conflict(self):
        dataset_id, _ = self._base_dataset()
        model = self.make_model()
        asset = self.upload_asset(png_bytes(color=(200, 10, 10)), "other.png", role="rgb")
        record = self.detect(model, asset["id"], key="other-det")
        self.confirm(record["id"])
        conn = self.conn()
        try:
            plan = reflow_service.preview(
                conn, self.alice["id"], self.project["id"], mode="revision", dataset_id=dataset_id,
                selections=[{"record_id": record["id"], "action": "replace"}],
            )
            with self.assertRaises(ApiError) as ctx:
                reflow_service.apply(
                    conn, self.alice["id"], self.project["id"], mode="revision", dataset_id=dataset_id,
                    selections=[{"record_id": record["id"], "action": "replace"}],
                    name="不该生成", request_key="rev-conflict",
                )
        finally:
            conn.close()
        self.assertEqual(plan["counts"]["conflicts"], 1)
        self.assertEqual(plan["conflicts"][0]["code"], "NEW_IMAGE")
        self.assertEqual(ctx.exception.code, "REFLOW_CONFLICTS")

    def test_new_dataset_even_split_and_idempotent(self):
        records = []
        for index, color in enumerate([(10, 200, 10), (20, 20, 200), (200, 200, 20)]):
            model = self.make_model()
            asset = self.upload_asset(png_bytes(color=color), f"n{index}.png", role="rgb")
            record = self.detect(model, asset["id"], key=f"n{index}-det")
            self.confirm(record["id"], key=f"n{index}-review")
            records.append(record["id"])
        selections = [{"record_id": rid, "action": "append"} for rid in records]
        conn = self.conn()
        try:
            first = reflow_service.apply(
                conn, self.alice["id"], self.project["id"], mode="new", dataset_id=None,
                selections=selections, name="新集", strategy="even", seed=7, request_key="new-1",
            )
            second = reflow_service.apply(
                conn, self.alice["id"], self.project["id"], mode="new", dataset_id=None,
                selections=selections, name="新集", strategy="even", seed=7, request_key="new-1",
            )
            counts = first["split_counts"]
            samples = conn.execute(
                "SELECT split, origin_record_id FROM dataset_samples WHERE dataset_id = ?",
                (first["id"],),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(first["id"], second["id"], "同一 request_key 不能生成两份")
        self.assertEqual(sum(counts.values()), 3)
        self.assertEqual(sorted(counts.values()), [1, 1, 1])
        self.assertEqual(len({row["origin_record_id"] for row in samples}), 3)

    def test_feedback_is_marked_reflowed_and_cannot_be_reused(self):
        dataset_id, _ = self._base_dataset()
        record, _ = self._record_with_bytes(png_bytes(), "fb-same")
        self.confirm(record["id"])
        conn = self.conn()
        try:
            feedback, _ = record_service.add_feedback(
                conn, self.alice["id"], self.project["id"], record["id"], request_key="fb-reflow"
            )
            reflow_service.apply(
                conn, self.alice["id"], self.project["id"], mode="revision", dataset_id=dataset_id,
                selections=[{"record_id": record["id"], "action": "replace"}],
                name="反馈回流", request_key="rev-fb",
            )
            listed = record_service.list_feedback(
                conn, self.alice["id"], self.project["id"], status="reflowed"
            )
            with self.assertRaises(ApiError) as ctx:
                record_service.add_feedback(
                    conn, self.alice["id"], self.project["id"], record["id"]
                )
        finally:
            conn.close()
        self.assertEqual(len(listed["feedback"]), 1)
        self.assertEqual(listed["feedback"][0]["id"], feedback["id"])
        self.assertEqual(ctx.exception.code, "ALREADY_REFLOVED")

    # ---------------------------------------------------------------- 产物真实性

    def test_revision_yaml_keeps_real_ir_directory(self):
        """data.yaml 的 modalities 必须指向真实配图目录，不能把划分名当模态。"""
        dataset_id, _base = self._base_dataset()
        record, _ = self._record_with_bytes(png_bytes(), "yaml-ir")
        self.confirm(record["id"], key="yaml-ir")
        conn = self.conn()
        try:
            new = reflow_service.apply(
                conn, self.alice["id"], self.project["id"], mode="revision",
                dataset_id=dataset_id,
                selections=[{"record_id": record["id"], "action": "replace"}],
                name="模态目录检查", request_key="yaml-ir-1",
            )
        finally:
            conn.close()
        config = yaml.safe_load(
            (self.root / new["root_rel"] / "data.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(config["modalities"], {"rgb": "images", "ir": "images_ir"}, config)
        self.assertNotIn("train", config["modalities"], "划分名不能出现在模态表里")
        self.assertEqual(config["train"], "images/train")
        self.assertTrue((self.root / new["root_rel"] / "images_ir" / "train").is_dir())
        self.assertEqual(new["secondary_modality"], "ir")
        self.assertEqual(new["modalities"], ["rgb", "ir"])

    def test_zero_weight_split_is_not_silently_used(self):
        selections = []
        for index, color in enumerate([(10, 200, 10), (20, 20, 200), (200, 200, 20)]):
            record, _ = self._record_with_bytes(png_bytes(color=color), f"ratio-{index}")
            self.confirm(record["id"], key=f"ratio-{index}")
            selections.append({"record_id": record["id"], "action": "append"})
        conn = self.conn()
        try:
            plan = reflow_service.preview(
                conn, self.alice["id"], self.project["id"], mode="new", dataset_id=None,
                selections=selections, strategy="ratio",
                ratios={"train": 0, "val": 1, "test": 0},
            )
            with self.assertRaises(ApiError) as ctx:
                reflow_service.preview(
                    conn, self.alice["id"], self.project["id"], mode="new", dataset_id=None,
                    selections=selections, strategy="ratio",
                    ratios={"train": -1, "val": 1, "test": 1},
                )
        finally:
            conn.close()
        self.assertFalse(plan["conflicts"], plan)
        self.assertEqual({item["split"] for item in plan["items"]}, {"val"})
        self.assertEqual(plan["splits"]["train"], 0)
        self.assertEqual(ctx.exception.code, "BAD_RATIOS")

    def test_explicit_stem_cannot_escape_generated_dataset(self):
        record, _ = self._record_with_bytes(png_bytes(color=(90, 10, 10)), "escape")
        self.confirm(record["id"], key="escape")
        conn = self.conn()
        try:
            with self.assertRaises(ApiError) as ctx:
                reflow_service.apply(
                    conn, self.alice["id"], self.project["id"], mode="new", dataset_id=None,
                    selections=[{
                        "record_id": record["id"], "action": "append", "split": "train",
                        "stem": "../../../../should-stay-in-dataset",
                    }],
                    name="文件名边界", request_key="escape-1",
                )
            leaked = list(self.root.glob("should-stay-in-dataset*"))
        finally:
            conn.close()
        self.assertEqual(ctx.exception.code, "REFLOW_CONFLICTS")
        self.assertEqual(ctx.exception.details["conflicts"][0]["code"], "BAD_STEM")
        self.assertFalse(leaked, f"不能在数据集目录外落文件：{leaked}")

    def test_export_zip_reimports_with_real_ir(self):
        """导出的 ZIP 再导入：RGB 与真实 IR 都要找得到，模态不能丢。"""
        dataset_id, _base = self._base_dataset()
        record, _ = self._record_with_bytes(png_bytes(), "roundtrip")
        self.confirm(record["id"], key="roundtrip")
        conn = self.conn()
        try:
            new = reflow_service.apply(
                conn, self.alice["id"], self.project["id"], mode="revision",
                dataset_id=dataset_id,
                selections=[{"record_id": record["id"], "action": "replace"}],
                name="回导检查", request_key="roundtrip-1",
            )
            archive = storage.resolve_rel(self.root, new["export_rel"], must_exist=True).read_bytes()
        finally:
            conn.close()
        asset = self.upload_asset(archive, "revision.zip")
        conn = self.conn()
        try:
            imported = dataset_service.import_dataset(
                conn, self.alice["id"], self.project["id"], name="回导数据集",
                modalities=["rgb", "ir"], secondary_modality="ir", asset_id=asset["id"],
            )
            samples = conn.execute(
                "SELECT * FROM dataset_samples WHERE dataset_id = ?", (imported["id"],)
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(imported["sample_count"], 1)
        self.assertEqual(imported["secondary_modality"], "ir")
        self.assertEqual(len(samples), 1)
        self.assertTrue(samples[0]["x_rel"].endswith(".png"), samples[0]["x_rel"])
        self.assertIn("images_ir", samples[0]["x_rel"])

    def test_same_target_sample_cannot_be_replaced_twice(self):
        dataset_id, _base = self._base_dataset()
        conn = self.conn()
        try:
            sample = conn.execute(
                "SELECT * FROM dataset_samples WHERE dataset_id = ?", (dataset_id,)
            ).fetchone()
        finally:
            conn.close()
        first, _ = self._record_with_bytes(png_bytes(color=(3, 3, 3)), "target-a")
        second, _ = self._record_with_bytes(png_bytes(color=(6, 6, 6)), "target-b")
        self.confirm(first["id"], key="target-a")
        self.confirm(second["id"], key="target-b")
        conn = self.conn()
        try:
            plan = reflow_service.preview(
                conn, self.alice["id"], self.project["id"], mode="revision",
                dataset_id=dataset_id,
                selections=[
                    {"record_id": first["id"], "action": "replace",
                     "target_sample_id": sample["id"]},
                    {"record_id": second["id"], "action": "replace",
                     "target_sample_id": sample["id"]},
                ],
            )
        finally:
            conn.close()
        self.assertIn("DUPLICATE_TARGET", [item["code"] for item in plan["conflicts"]])

    def test_stale_feedback_blocks_reflow_after_new_version(self):
        record = self.record(key="stale-fb")
        confirmed = self.confirm(record["id"], key="stale-fb-1")
        conn = self.conn()
        try:
            record_service.add_feedback(
                conn, self.alice["id"], self.project["id"], record["id"], request_key="stale-fb"
            )
            # 用户又确认了一次：反馈池里那条指向 v1，已经过期
            review_service.submit(
                conn, self.alice["id"], self.project["id"], record["id"],
                boxes=[{"class_id": 0, "bbox": [8, 8, 24, 24]}], outcome="defect",
                base_version=confirmed["version"], request_key="stale-fb-2",
            )
            plan = reflow_service.preview(
                conn, self.alice["id"], self.project["id"], mode="new", dataset_id=None,
                selections=[{"record_id": record["id"], "action": "append"}],
            )
        finally:
            conn.close()
        self.assertIn("STALE_FEEDBACK", [item["code"] for item in plan["conflicts"]])


class FingerprintTest(ReviewBase):
    def test_replaced_image_bytes_are_rejected(self):
        model = self.make_model()
        asset = self.upload_asset(png_bytes(), "fp.png", role="rgb")
        conn = self.conn()
        try:
            target = asset_service.path_of(conn, self.alice["id"], self.project["id"], asset["id"])
        finally:
            conn.close()
        target.write_bytes(png_bytes(color=(1, 2, 3)))
        conn = self.conn()
        try:
            with self.assertRaises(ApiError) as ctx:
                detection_service.run_detection(
                    conn, self.alice["id"], self.project["id"], model_id=model["id"],
                    rgb_asset_id=asset["id"], request_key="fp-img",
                )
            count = conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(ctx.exception.code, "ASSET_CORRUPTED")
        self.assertEqual(count, 0, "指纹不符时不能落一条“成功”记录")

    def test_replaced_weight_is_rejected(self):
        model = self.make_model()
        asset = self.upload_asset(png_bytes(), "fp2.png", role="rgb")
        weight = storage.resolve_rel(self.root, model["weight_rel"], must_exist=True)
        weight.write_bytes(b"replaced-by-someone")
        conn = self.conn()
        try:
            with self.assertRaises(ApiError) as ctx:
                detection_service.run_detection(
                    conn, self.alice["id"], self.project["id"], model_id=model["id"],
                    rgb_asset_id=asset["id"], request_key="fp-weight",
                )
        finally:
            conn.close()
        self.assertEqual(ctx.exception.code, "MODEL_WEIGHT_CHANGED")


class IdentityTest(ReviewBase):
    def test_report_finds_duplicates_leakage_and_conflicts(self):
        conn = self.conn()
        try:
            dataset_id, base = ReflowTest._base_dataset(self)
            # 人为再插一条同身份、不同划分、不同标注
            identity = identity_service.identity_of_bytes(png_bytes())
            conn.execute(
                "INSERT INTO dataset_samples (id, dataset_id, split, stem, rgb_rel, label_rel,"
                " width, height, boxes, class_ids, created_at, sha256, pixel_sha256)"
                " VALUES (?, ?, 'val', 'a-copy', ?, NULL, 64, 48, '[{\"class_id\": 1}]', '[1]', ?, ?, ?)",
                (
                    f"smp_{secrets.token_hex(8)}",
                    dataset_id,
                    storage.relative(self.root, base / "images" / "train" / "a.png"),
                    "2026-11-05T00:00:00Z",
                    identity["sha256"],
                    identity["pixel_sha256"],
                ),
            )
            conn.commit()
            report = identity_service.dataset_report(
                conn, self.alice["id"], self.project["id"], dataset_id
            )
        finally:
            conn.close()
        self.assertEqual(len(report["duplicate_groups"]), 1)
        self.assertEqual(len(report["cross_split_leakage"]), 1)
        self.assertEqual(len(report["annotation_conflicts"]), 1)
        self.assertEqual(report["note"], "只读检查：不会自动删除或合并任何样本。")


class FollowupTest(ReviewBase):
    """人工提交后的会话接力：提交完不需要再发一句“你好”。

    这里走的是真实链路：真实工具 ``hx_open_page`` 登记等待 → 真实 HTTP 提交 →
    真实运行器用假引擎起一轮续跑。断言看落库的轮次、提示词与消息，不看内部变量。
    """

    fake_engine = True

    @property
    def fake(self):
        return self.harness.fake

    def _actor(self, conn):
        project = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (self.project["id"],)
        ).fetchone()
        connection = agent_identity.connection_for(conn, self.alice["id"])
        return Actor(
            user_id=int(self.alice["id"]),
            username="alice",
            project_id=str(self.project["id"]),
            project_name=str(project["name"]),
            session_id=str(self.session["id"]),
            turn_id=f"trn_{secrets.token_hex(8)}",
            mode=agent_identity.effective_mode(conn, self.alice["id"]),
            root_path=self.root,
            connection_fingerprint=agent_identity.connection_fingerprint(connection),
            provider=str(connection["provider"]),
            model=agent_identity.effective_model(conn, self.alice["id"], connection),
        )

    def _open_review(self, record_id: str, instruction: str = "") -> dict:
        conn = self.conn()
        try:
            ctx = ToolContext(
                conn=conn,
                actor=self._actor(conn),
                args={"page": "review", "record_id": record_id, "instruction": instruction},
                emit=lambda *_args: None,
                jobs={},
            )
            return agent_tools.TOOLS["hx_open_page"].handler(ctx)
        finally:
            conn.close()

    def _submit(self, record_id: str, *, key: str = "f1", boxes=None, base_version: int = 0):
        return self.client.post(
            f"/api/projects/{self.project['id']}/detections/{record_id}/review/submit",
            json={
                "boxes": boxes
                if boxes is not None
                else [{"class_id": 1, "bbox": [10, 10, 44, 36], "severity": "high"}],
                "outcome": "defect",
                "base_version": base_version,
                "request_key": key,
            },
            headers=csrf_headers(self.client),
        )

    def _followup_turns(self, conn) -> list:
        return conn.execute(
            "SELECT * FROM turns WHERE session_id = ? AND request_id LIKE 'review-followup:%'"
            " ORDER BY created_at ASC",
            (str(self.session["id"]),),
        ).fetchall()

    def messages(self) -> list[dict]:
        response = self.client.get(f"/api/sessions/{self.session['id']}/messages")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return body(response)["data"]["messages"]

    def test_dataset_audit_tool_lists_samples_and_report(self):
        dataset_id, _base = ReflowTest._base_dataset(self)
        record, _ = ReflowTest._record_with_bytes(self, png_bytes(), "audit")
        self.confirm(record["id"], key="audit")
        conn = self.conn()
        try:
            ctx = ToolContext(
                conn=conn,
                actor=self._actor(conn),
                args={"dataset_id": dataset_id, "record_ids": [record["id"]]},
                emit=lambda *_args: None,
                jobs={},
            )
            out = agent_tools.TOOLS["hx_dataset_audit"].handler(ctx)
        finally:
            conn.close()
        self.assertEqual(out["dataset"]["id"], dataset_id)
        self.assertEqual(len(out["samples"]), 1)
        self.assertEqual(out["samples"][0]["modality"], "rgb+ir")
        self.assertEqual(out["samples"][0]["split"], "train")
        self.assertEqual(out["report"]["sample_count"], 1)
        self.assertEqual(out["matches"][0]["status"], "unique")
        self.assertIn("只读", out["note"])

    def test_submit_continues_same_session_without_extra_user_message(self):
        record = self.record(key="follow")
        opened = self._open_review(record["id"], instruction="确认是热斑后，回原修订到数据集 dst_demo")
        self.assertTrue(opened["waiting"], opened)

        self.fake.queue({"text": "已读到第 1 版确认结论；按你的要求准备回原修订。"})
        response = self._submit(record["id"], key="follow-submit")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

        conn = self.conn()
        try:
            turns = self._followup_turns(conn)
        finally:
            conn.close()
        self.assertEqual(len(turns), 1, "提交后应该在本会话自动接一轮")
        final = wait_turn(self.client, str(turns[0]["id"]))
        self.assertEqual(final["turn"]["status"], "completed")

        prompt = self.harness.fake.prompts[-1]["text"]
        self.assertIn("系统事件", prompt)
        self.assertIn(record["id"], prompt)
        self.assertIn("dst_demo", prompt, "续跑提示词要带上用户原来的去向要求")

        messages = self.messages()
        self.assertFalse(
            [item for item in messages if item["role"] == "user"],
            "用户没有再发消息，会话里不应凭空多出一条用户消息",
        )
        self.assertEqual(
            [item["content"] for item in messages if item["role"] == "assistant"],
            ["已读到第 1 版确认结论；按你的要求准备回原修订。"],
        )

    def test_repeat_submit_does_not_wake_the_session_twice(self):
        record = self.record(key="twice")
        self._open_review(record["id"])
        self.fake.queue({"text": "第一轮续跑完成。"})
        self._submit(record["id"], key="twice-1")
        conn = self.conn()
        try:
            first = self._followup_turns(conn)
        finally:
            conn.close()
        wait_turn(self.client, str(first[0]["id"]))

        # 再来一版提交：没有新的等待项，不能凭空再开一轮
        response = self._submit(
            record["id"], key="twice-2", base_version=1,
            boxes=[{"class_id": 0, "bbox": [12, 12, 30, 30]}],
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = self.conn()
        try:
            again = self._followup_turns(conn)
        finally:
            conn.close()
        self.assertEqual(len(again), 1)

    def test_new_user_message_cancels_waiting_followup(self):
        record = self.record(key="switch")
        self._open_review(record["id"])
        # 用户改主意，直接发新消息：旧等待必须作废
        self.fake.queue({"text": "好的，先看别的。"})
        self.client.post(
            f"/api/sessions/{self.session['id']}/turns",
            json={"content": "先别等复核了，看看记录列表", "request_id": secrets.token_hex(8)},
            headers=csrf_headers(self.client),
        )
        conn = self.conn()
        try:
            row = conn.execute(
                "SELECT * FROM review_followups WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
                (str(self.session["id"]),),
            ).fetchone()
            turns_before = len(self._followup_turns(conn))
        finally:
            conn.close()
        self.assertEqual(row["status"], "cancelled")
        self._submit(record["id"], key="switch-submit")
        conn = self.conn()
        try:
            self.assertEqual(len(self._followup_turns(conn)), turns_before)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
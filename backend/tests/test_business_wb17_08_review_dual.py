"""双模态标注沿用/清空语义（回归 root 独立核验发现的两个失败项）。

root 用真实普通账号 + 共享服务实测发现：显式把草稿 x_boxes 清空为 [] 之后，
旧客户端下一次**省略** x_boxes 保存/提交，会把已清掉的旧确认框“复活”。
根因在 _resolve_x_boxes 用 ``carried = _stored_x_boxes(draft) or _stored_x_boxes(submitted)``
判断：空数组是假值，于是回退到已提交版本。

这里直接测共享函数本身（不走 UI），确保：
* 草稿 x_boxes 列存在且为 [] → 沿用结果就是 []，不能被 submitted 复活；
* 草稿列是 NULL（旧数据/未迁移）→ 才回退到最近提交；
* 显式 [] / 数组 / None 三种入参语义正确，且按辅模态自己的宽高校验。
"""

from __future__ import annotations

import json
import sqlite3
import unittest
import zipfile

import yaml
from PIL import Image

import secrets

from backend.errors import ApiError
from backend.services import identity as identity_service
from backend.services import reflow as reflow_service
from backend.services import review as review_service
from backend.services import storage
from backend.services.review import _effective_x_boxes, _resolve_x_boxes
from backend.tests import test_business_wb17_04 as base04
from backend.tests.support import body, csrf_headers
from backend.tests.test_business_wb17_03 import png_bytes

BOX_A = {
    "class_id": 0,
    "confidence": 0.9,
    "bbox": [10.0, 20.0, 40.0, 60.0],
    "severity": "medium",
    "source": "manual",
}


def _row(x_boxes: str | None) -> sqlite3.Row:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE review_versions (id TEXT PRIMARY KEY, x_boxes TEXT)")
    connection.execute("INSERT INTO review_versions (id, x_boxes) VALUES (?, ?)", ("ver", x_boxes))
    return connection.execute("SELECT * FROM review_versions").fetchone()


def _data(**overrides) -> dict:
    image = {
        "x_modality": "ir",
        "x_width": 90,
        "x_height": 120,
        "primary_modality": "rgb",
        "width": 160,
        "height": 90,
    }
    image.update(overrides.pop("image", {}))
    data = {
        "image": image,
        "class_options": [{"index": 0, "name": "单一热斑"}, {"index": 1, "name": "大面积热斑"}],
    }
    data.update(overrides)
    return data


class EffectiveXBoxesTest(unittest.TestCase):
    def test_explicit_empty_draft_is_not_resurrected_by_submitted(self):
        draft = _row("[]")
        submitted = _row('[{"class_id": 0, "bbox": [1, 2, 3, 4]}]')
        self.assertEqual(_effective_x_boxes(draft, submitted), [])

    def test_legacy_null_draft_falls_back_to_submitted(self):
        draft = _row(None)
        submitted = _row('[{"class_id": 0, "bbox": [1, 2, 3, 4]}]')
        self.assertEqual(len(_effective_x_boxes(draft, submitted)), 1)

    def test_draft_value_wins(self):
        draft = _row('[{"class_id": 1, "bbox": [5, 6, 7, 8]}]')
        submitted = _row("[]")
        self.assertEqual(len(_effective_x_boxes(draft, submitted)), 1)

    def test_no_rows_returns_empty(self):
        self.assertEqual(_effective_x_boxes(None, None), [])


class ResolveXBoxesTest(unittest.TestCase):
    def test_omitted_after_cleared_draft_stays_cleared(self):
        """root 失败项 1：清空草稿后旧客户端省略 x_boxes 保存。"""
        clean, has_secondary = _resolve_x_boxes(
            _data(), _row("[]"), _row('[{"class_id": 0, "bbox": [1, 2, 3, 4]}]'), None
        )
        self.assertEqual(clean, [])
        self.assertTrue(has_secondary)

    def test_omitted_after_cleared_draft_stays_cleared_on_submit(self):
        """root 失败项 2：清空草稿后旧客户端省略 x_boxes 提交。"""
        clean, has_secondary = _resolve_x_boxes(
            _data(), None, _row("[]"), None
        )
        self.assertEqual(clean, [])
        self.assertTrue(has_secondary)

    def test_omitted_carries_legacy_submitted(self):
        clean, _ = _resolve_x_boxes(
            _data(), None, _row('[{"class_id": 0, "confidence": 0.8, "bbox": [1, 2, 3, 4]}]'), None
        )
        self.assertEqual(len(clean), 1)
        self.assertEqual(clean[0]["bbox"], [1, 2, 3, 4])

    def test_explicit_empty_clears(self):
        clean, _ = _resolve_x_boxes(
            _data(), _row('[{"class_id": 0, "bbox": [1, 2, 3, 4]}]'), None, []
        )
        self.assertEqual(clean, [])

    def test_explicit_boxes_use_secondary_size(self):
        clean, _ = _resolve_x_boxes(_data(), None, None, [BOX_A])
        self.assertEqual(clean[0]["bbox"], [10.0, 20.0, 40.0, 60.0])

    def test_box_beyond_secondary_size_is_rejected(self):
        bad = dict(BOX_A, bbox=[10.0, 20.0, 200.0, 60.0])
        with self.assertRaises(ApiError) as caught:
            _resolve_x_boxes(_data(), None, None, [bad])
        self.assertEqual(caught.exception.code, "BAD_BOX")

    def test_boxes_without_secondary_modality_are_rejected(self):
        with self.assertRaises(ApiError) as caught:
            _resolve_x_boxes(_data(image={"x_modality": None}), None, None, [BOX_A])
        self.assertEqual(caught.exception.code, "NO_SECONDARY_MODALITY")

    def test_omitted_without_secondary_is_harmless(self):
        clean, has_secondary = _resolve_x_boxes(_data(image={"x_modality": None}), None, None, None)
        self.assertEqual(clean, [])
        self.assertFalse(has_secondary)


def _label_lines(path) -> list[list[float]]:
    return [[float(part) for part in line.split()] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class DualModalityReflowTest(base04.ReviewBase):
    """两画布产生的两套框必须各自落到真实文件，且不伪造跨模态配准。"""


    def _base_dataset(self, names=("单一热斑", "大面积热斑")):
        """造一个真实文件齐全的最小 RGB+IR 数据集（1 个样本）。"""
        conn = self.conn()
        try:
            dataset_id = f"dst_{secrets.token_hex(8)}"
            base = storage.project_dir(self.root, storage.DATASETS, dataset_id)
            for folder in ("images/train", "images_ir/train", "labels/train"):
                storage.project_dir(self.root, storage.DATASETS, dataset_id, *folder.split("/"))
            rgb_bytes = png_bytes()
            storage.write_bytes(self.root, base / "images" / "train" / "a.png", rgb_bytes)
            storage.write_bytes(self.root, base / "images_ir" / "train" / "a.png", png_bytes(color=(7, 7, 7)))
            storage.write_bytes(self.root, base / "labels" / "train" / "a.txt", b"0 0.5 0.5 0.2 0.2\n")
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


    def _dual_record(self, key: str = "dual"):
        model = self.make_model(mode="multimodal", secondary="ir")
        rgb = self.upload_asset(png_bytes(160, 90), f"{key}-rgb.png", role="rgb")
        ir = self.upload_asset(png_bytes(90, 120, color=(9, 9, 9)), f"{key}-ir.png", role="ir")
        return self.detect(model, rgb["id"], x_id=ir["id"], key=f"{key}-det")

    def _confirm_dual(self, record_id: str, *, boxes, x_boxes, key: str) -> dict:
        conn = self.conn()
        try:
            version, _ = review_service.submit(
                conn,
                self.alice["id"],
                self.project["id"],
                record_id,
                boxes=boxes,
                x_boxes=x_boxes,
                outcome="defect",
                base_version=0,
                request_key=key,
            )
        finally:
            conn.close()
        return version

    def _apply_new(self, record_id: str, *, reference: str, key: str) -> dict:
        conn = self.conn()
        try:
            plan = reflow_service.preview(
                conn,
                self.alice["id"],
                self.project["id"],
                mode="new",
                dataset_id=None,
                selections=[{"record_id": record_id, "action": "append"}],
                reference_modality=reference,
            )
            self.assertEqual(plan["conflicts"], [], plan["conflicts"])
            self.assertEqual(plan["reference_modality"], reference, plan)
            dataset = reflow_service.apply(
                conn,
                self.alice["id"],
                self.project["id"],
                mode="new",
                dataset_id=None,
                selections=[{"record_id": record_id, "action": "append"}],
                name=f"双模态-{reference}-{key}",
                request_key=key,
                reference_modality=reference,
            )
            samples = [dict(row) for row in conn.execute("SELECT * FROM dataset_samples WHERE dataset_id = ?", (dataset["id"],)).fetchall()]
        finally:
            conn.close()
        return {"plan": plan, "dataset": dataset, "samples": samples}

    def _dataset_root(self, dataset: dict):
        # resolve_rel(must_exist=True) 只接受文件；数据集根是目录，这里自己确认存在。
        root = storage.resolve_rel(self.root, dataset["root_rel"])
        self.assertTrue(root.is_dir(), f"数据集目录不存在: {root}")
        return root

    def test_both_modalities_are_written_as_two_real_label_sets(self):
        record = self._dual_record("both")
        self._confirm_dual(
            record["id"],
            boxes=[{"class_id": 0, "bbox": [100, 10, 140, 60]}],
            x_boxes=[{"class_id": 1, "bbox": [70, 80, 85, 110]}],
            key="both-confirm",
        )
        result = self._apply_new(record["id"], reference="rgb", key="both-apply")
        self.assertEqual(len(result["samples"]), 1)
        sample = result["samples"][0]
        self.assertIsNotNone(sample["labels_ir_rel"], "辅模态标注必须落真实文件")
        label = storage.resolve_rel(self.root, sample["label_rel"], must_exist=True)
        label_ir = storage.resolve_rel(self.root, sample["labels_ir_rel"], must_exist=True)
        rgb_line = _label_lines(label)[0]
        ir_line = _label_lines(label_ir)[0]
        self.assertAlmostEqual(rgb_line[0], 0.0)
        self.assertAlmostEqual(rgb_line[1], 0.75, places=5)
        self.assertAlmostEqual(rgb_line[2], 35 / 90, places=5)
        self.assertAlmostEqual(ir_line[0], 1.0)
        self.assertAlmostEqual(ir_line[1], 155 / 180, places=5)
        self.assertAlmostEqual(ir_line[2], 95 / 120, places=5)
        dataset_root = self._dataset_root(result["dataset"])
        yaml_data = yaml.safe_load((dataset_root / "data.yaml").read_text(encoding="utf-8"))
        self.assertEqual(yaml_data["modalities"], {"rgb": "images", "ir": "images_ir"})
        self.assertEqual(yaml_data["modality_used"], ["rgb", "ir"])
        with zipfile.ZipFile(storage.resolve_rel(self.root, result["dataset"]["export_rel"], must_exist=True)) as archive:
            names = archive.namelist()
        self.assertTrue(any(name.startswith("labels/train/") for name in names), names)
        self.assertTrue(any(name.startswith("labels_ir/train/") for name in names), names)
        manifest = json.loads(storage.resolve_rel(self.root, result["dataset"]["manifest_rel"], must_exist=True).read_text(encoding="utf-8"))
        self.assertEqual(manifest["reference_modality"], "rgb")
        self.assertEqual(manifest["secondary_modality"], "ir")
        self.assertIsNotNone(manifest["samples"][0]["labels_ir_rel"])

    def test_ir_only_defect_can_use_ir_as_reference_without_reprojection(self):
        record = self._dual_record("ir-only")
        self._confirm_dual(
            record["id"], boxes=[], x_boxes=[{"class_id": 1, "bbox": [70, 80, 85, 110]}], key="ir-only-confirm"
        )
        result = self._apply_new(record["id"], reference="ir", key="ir-only-apply")
        sample = result["samples"][0]
        label = storage.resolve_rel(self.root, sample["label_rel"], must_exist=True)
        line = _label_lines(label)[0]
        self.assertAlmostEqual(line[0], 1.0)
        self.assertAlmostEqual(line[1], 155 / 180, places=5)
        self.assertAlmostEqual(line[2], 95 / 120, places=5)
        self.assertIsNone(sample["labels_ir_rel"], "没有 RGB 框时不能写空文件冒充负样本")
        dataset_root = self._dataset_root(result["dataset"])
        primary_images = list((dataset_root / "images" / "train").glob("*.png"))
        self.assertEqual(len(primary_images), 1)
        with Image.open(primary_images[0]) as image:
            self.assertEqual(image.size, (90, 120), "参考模态是 IR 时 images/ 必须是 IR 原图")
        rgb_images = list((dataset_root / "images_rgb" / "train").glob("*.png"))
        self.assertEqual(len(rgb_images), 1)
        with Image.open(rgb_images[0]) as image:
            self.assertEqual(image.size, (160, 90))
        yaml_data = yaml.safe_load((dataset_root / "data.yaml").read_text(encoding="utf-8"))
        self.assertEqual(yaml_data["modalities"], {"ir": "images", "rgb": "images_rgb"})
        self.assertEqual(yaml_data["modality_used"], ["ir", "rgb"])

    def test_ir_only_defect_with_rgb_reference_is_blocked_not_silently_dropped(self):
        record = self._dual_record("ir-ref-rgb")
        self._confirm_dual(
            record["id"], boxes=[], x_boxes=[{"class_id": 1, "bbox": [70, 80, 85, 110]}], key="ir-ref-rgb-confirm"
        )
        conn = self.conn()
        try:
            plan = reflow_service.preview(
                conn,
                self.alice["id"],
                self.project["id"],
                mode="new",
                dataset_id=None,
                selections=[{"record_id": record["id"], "action": "append"}],
                reference_modality="rgb",
            )
            with self.assertRaises(ApiError) as caught:
                reflow_service.apply(
                    conn,
                    self.alice["id"],
                    self.project["id"],
                    mode="new",
                    dataset_id=None,
                    selections=[{"record_id": record["id"], "action": "append"}],
                    name="不该生成",
                    request_key="ir-ref-rgb-apply",
                    reference_modality="rgb",
                )
        finally:
            conn.close()
        self.assertEqual(plan["conflicts"][0]["code"], "REFERENCE_HAS_NO_BOXES", plan["conflicts"])
        self.assertEqual(caught.exception.code, "REFLOW_CONFLICTS")

    def test_revision_cannot_change_parent_reference_modality(self):
        dataset_id, _ = self._base_dataset()
        record = self._dual_record("rev-mismatch")
        self._confirm_dual(
            record["id"],
            boxes=[{"class_id": 0, "bbox": [10, 10, 40, 30]}],
            x_boxes=[{"class_id": 1, "bbox": [20, 20, 40, 40]}],
            key="rev-mismatch-confirm",
        )
        conn = self.conn()
        try:
            plan = reflow_service.preview(
                conn,
                self.alice["id"],
                self.project["id"],
                mode="revision",
                dataset_id=dataset_id,
                selections=[{"record_id": record["id"], "action": "replace"}],
                reference_modality="ir",
            )
        finally:
            conn.close()
        codes = [conflict["code"] for conflict in plan["conflicts"]]
        self.assertIn("REFERENCE_MODALITY_MISMATCH", codes, codes)


class ReviewEventsCursorTest(base04.ReviewBase):
    """root m04798：轮询游标必须用 record 内单调的 version，不能用秒级 updated_at。

    同秒内 Agent/手动两次保存时 updated_at 相同，``updated_at > after`` 会漏掉后一次；
    version 单调递增，用 ``after_version`` 一定能取到新版本。旧 ``after`` 参数继续兼容。
    """

    def _events(self, record_id: str, **params) -> dict:
        query = "&".join(f"{key}={value}" for key, value in params.items())
        response = self.client.get(
            f"/api/projects/{self.project['id']}/review-events?record_id={record_id}&{query}"
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return body(response)["data"]

    def _latest(self, record_id: str) -> dict | None:
        data = self._events(record_id, latest=1)
        return data["projection"]

    def test_version_cursor_is_monotonic_even_within_same_second(self):
        record = self.record()
        self.confirm(record["id"], key="cursor-v1")
        first = self._events(record["id"], after_version=0, limit=50)
        self.assertIn(1, [event["version"] for event in first["events"]])
        self.assertEqual(first["last_version"], 1)

        saved = self.client.post(
            f"/api/projects/{self.project['id']}/detections/{record['id']}/review/draft",
            json={
                "boxes": [{"class_id": 1, "bbox": [10, 10, 40, 30]}],
                "note": "same-second",
                "base_version": 1,
            },
            headers=csrf_headers(self.client),
        )
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        latest_version = int(body(saved)["data"]["state"]["draft"]["version"])

        second = self._events(record["id"], after_version=1, limit=50)
        versions = [event["version"] for event in second["events"]]
        self.assertIn(latest_version, versions, versions)
        self.assertEqual(second["last_version"], latest_version)

        empty = self._events(record["id"], after_version=latest_version, limit=50)
        self.assertEqual(empty["events"], [])

        legacy = self._events(record["id"], after="1970-01-01T00:00:00+00:00", limit=10)
        self.assertTrue(legacy["events"])



    def test_latest_projection_detects_submit_after_draft_version_regression(self):
        """submit 从“最近已提交版本”+1，草稿版本号更大；版本号回退仍必须被判为变化。"""
        record = self.record()
        self.confirm(record["id"], key="regress-v1")
        base = 1
        for note in ("draft-2", "draft-3"):
            response = self.client.post(
                f"/api/projects/{self.project['id']}/detections/{record['id']}/review/draft",
                json={
                    "boxes": [{"class_id": 1, "bbox": [10, 10, 40, 30]}],
                    "note": note,
                    "base_version": base,
                },
                headers=csrf_headers(self.client),
            )
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            base = int(body(response)["data"]["state"]["draft"]["version"])

        draft_projection = self._latest(record["id"])
        self.assertEqual(draft_projection["status"], "draft")
        self.assertEqual(draft_projection["version"], 3)

        submitted = self.client.post(
            f"/api/projects/{self.project['id']}/detections/{record['id']}/review/submit",
            json={
                "boxes": [{"class_id": 0, "bbox": [10, 10, 44, 36]}],
                "outcome": "defect",
                "base_version": 1,
                "request_key": "regress-submit",
            },
            headers=csrf_headers(self.client),
        )
        self.assertEqual(submitted.status_code, 200, submitted.get_data(as_text=True))

        projection = self._latest(record["id"])
        self.assertEqual(projection["status"], "confirmed")
        self.assertEqual(projection["version"], 2)
        # 关键：version 从草稿 3 回退到已确认 2——严格 after_version 游标会漏这次提交。
        self.assertLess(projection["version"], draft_projection["version"])
        self.assertNotEqual(
            (projection["id"], projection["status"], projection["version"]),
            (draft_projection["id"], draft_projection["status"], draft_projection["version"]),
        )
        missed = self._events(record["id"], after_version=draft_projection["version"], limit=50)
        self.assertEqual(missed["events"], [])


if __name__ == "__main__":
    unittest.main()
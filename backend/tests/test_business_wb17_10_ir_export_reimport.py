"""WB17-10：平台自己导出的“IR 参考 + RGB 辅助”数据集必须能再导入。

独立反例（root）：reflow.apply 生成的 IR 参考数据集导出后，在另一项目用
``modalities=["ir","rgb"]`` + ``secondary_modality="rgb"`` 导入会被
``第二模态只支持 depth/edge/ir/x`` 拒绝——平台不能重新导入自己的合法导出。

这里钉住的行为：

* 目录与 ZIP 都能导入该布局：IR 主图在 ``images/``、RGB 辅助图在
  ``images_ir/``，``labels/`` 与 ``labels_ir/`` 两套标注都校验并登记；
* 导入后落库的 ``reference_modality`` 与 ``modalities`` 顺序解耦，legacy 行
  （reference=ir 但 modalities=[rgb,ir]）在 datasets/models/detection 都按 ir；
* 辅助标注参与内容指纹：只改 labels_ir 不能被当成重复数据复用；
* 非法组合（IR+edge、主辅相同、参考不在 modalities）仍然明确报错；
* 外来 manifest 的 owner/record ID 不被信任，只按真实文件扫描。
"""

from __future__ import annotations

import io
import json
import secrets
import zipfile

import yaml

from backend.errors import ApiError
from backend.services import datasets as dataset_service
from backend.services import detection as detection_service
from backend.services import models as model_service
from backend.services import storage
from backend.tests.test_business_wb17_03 import BusinessBase, png_bytes

NAMES = ["热斑"]


def _label(box: str = "0 0.5 0.5 0.2 0.2") -> str:
    return box + "\n"


class IRReferenceReimportTest(BusinessBase):
    def _source(self, name: str, *, aux_label: str = "0 0.25 0.25 0.2 0.2") -> str:
        """造一份 reflow 导出布局：IR 主图 120x90，RGB 辅助图 160x120（尺寸不同）。"""
        rel = f"uploads/irref-{name}-{secrets.token_hex(4)}"
        base = storage.project_dir(self.root, *rel.split("/"))
        for split in ("train", "val"):
            storage.write_bytes(self.root, base / "images" / split / "a.png", png_bytes(120, 90, (20, 20, 20)))
            storage.write_bytes(self.root, base / "labels" / split / "a.txt", _label().encode())
            storage.write_bytes(
                self.root, base / "images_ir" / split / "a.png", png_bytes(160, 120, (90, 60, 30))
            )
            storage.write_bytes(self.root, base / "labels_ir" / split / "a.txt", _label(aux_label).encode())
        config = {
            "path": ".",
            "nc": 1,
            "names": NAMES,
            "train": "images/train",
            "val": "images/val",
            "modalities": {"ir": "images", "rgb": "images_ir"},
            "modality_used": ["ir", "rgb"],
        }
        storage.write_bytes(
            self.root, base / "data.yaml", yaml.safe_dump(config, allow_unicode=True).encode("utf-8")
        )
        return rel

    def _import(
        self,
        rel: str | None = None,
        name: str = "IR 参考集",
        *,
        asset_id: str | None = None,
        modalities: list[str] | None = None,
        secondary: str | None = "rgb",
        reference: str | None = "ir",
    ):
        conn = self.conn()
        try:
            return dataset_service.import_dataset(
                conn,
                self.alice["id"],
                self.project["id"],
                name=name,
                modalities=modalities if modalities is not None else ["ir", "rgb"],
                secondary_modality=secondary,
                reference_modality=reference,
                classes=NAMES,
                rel_path=rel,
                asset_id=asset_id,
            )
        finally:
            conn.close()

    def _raw_yaml(self, dataset: dict) -> dict:
        # 导入后的规范 data.yaml 写在数据集目录根部（path=raw 指向复制出来的树）。
        path = storage.resolve_rel(self.root, f"{dataset['root_rel']}/data.yaml")
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ 目录

    def test_directory_layout_keeps_ir_reference_rgb_auxiliary_and_both_labels(self):
        dataset = self._import(self._source("dir"))
        self.assertEqual(dataset["reference_modality"], "ir")
        self.assertEqual(dataset["primary_modality"], "ir")
        self.assertEqual(dataset["secondary_modality"], "rgb")
        self.assertEqual(dataset["modalities"], ["ir", "rgb"])
        self.assertEqual(dataset["sample_count"], 2)

        conn = self.conn()
        try:
            page = dataset_service.samples(
                conn, self.alice["id"], self.project["id"], dataset["id"]
            )
        finally:
            conn.close()
        self.assertTrue(all(item["x_rel"] for item in page["samples"]))
        self.assertTrue(all(item.get("labels_ir_rel") for item in page["samples"]))

        # 重新写出的 data.yaml 必须还原真实目录，而不是猜 images_rgb。
        config = self._raw_yaml(dataset)
        self.assertEqual(config["modalities"], {"ir": "images", "rgb": "images_ir"})
        self.assertEqual(config["train"], "images/train")
        self.assertEqual(config["val"], "images/val")
        self.assertEqual(config["path"], "raw")

    def test_auto_infers_modalities_from_data_yaml(self):
        # 省略 modalities 时，导入端必须按 data.yaml 的 modalities 自己识别，
        # 不能把真实 RGB+IR 数据集默默当单 RGB 导入。
        rel = self._source("auto")
        conn = self.conn()
        try:
            dataset = dataset_service.import_dataset(
                conn,
                self.alice["id"],
                self.project["id"],
                name="IR 自动识别",
                modalities=None,
                secondary_modality=None,
                reference_modality=None,
                classes=NAMES,
                rel_path=rel,
            )
        finally:
            conn.close()
        self.assertEqual(dataset["reference_modality"], "ir")
        self.assertEqual(dataset["primary_modality"], "ir")
        self.assertEqual(dataset["secondary_modality"], "rgb")
        self.assertEqual(dataset["modalities"], ["ir", "rgb"])

    def test_auto_inference_falls_back_to_rgb_without_modalities(self):
        rel = f"uploads/rgb-auto-{secrets.token_hex(4)}"
        base = storage.project_dir(self.root, *rel.split("/"))
        for split in ("train", "val"):
            storage.write_bytes(self.root, base / "images" / split / "a.png", png_bytes(120, 90, (30, 60, 90)))
            storage.write_bytes(self.root, base / "labels" / split / "a.txt", _label().encode())
        config = {"path": ".", "nc": 1, "names": NAMES, "train": "images/train", "val": "images/val"}
        storage.write_bytes(
            self.root, base / "data.yaml", yaml.safe_dump(config, allow_unicode=True).encode("utf-8")
        )
        conn = self.conn()
        try:
            dataset = dataset_service.import_dataset(
                conn,
                self.alice["id"],
                self.project["id"],
                name="RGB 自动回退",
                modalities=None,
                classes=NAMES,
                rel_path=rel,
            )
        finally:
            conn.close()
        self.assertEqual(dataset["reference_modality"], "rgb")
        self.assertEqual(dataset["modalities"], ["rgb"])

    def test_changed_auxiliary_label_is_not_deduplicated(self):
        rel = self._source("aux-label")
        first = self._import(rel, "IR 集")
        base = storage.project_dir(self.root, *rel.split("/"))
        storage.write_bytes(
            self.root,
            base / "labels_ir" / "train" / "a.txt",
            _label("0 0.7 0.7 0.1 0.1").encode(),
        )
        second = self._import(rel, "IR 集-改辅助标签")
        self.assertNotEqual(second["id"], first["id"], "辅助标注变了就是另一份内容")
        self.assertFalse(second.get("reused"))

    def test_same_content_reuses_the_first_dataset(self):
        rel = self._source("reuse")
        first = self._import(rel, "IR 集")
        second = self._import(rel, "IR 集-同内容")
        self.assertTrue(second.get("reused"))
        self.assertEqual(second["id"], first["id"])

    def test_legacy_modalities_order_with_explicit_reference_still_imports_as_ir(self):
        # legacy 调用方可能把 modalities 写成 [rgb, ir]；显式 reference=ir 必须胜出。
        dataset = self._import(
            self._source("legacy-order"),
            "旧顺序",
            modalities=["rgb", "ir"],
        )
        self.assertEqual(dataset["reference_modality"], "ir")
        self.assertEqual(dataset["primary_modality"], "ir")
        self.assertEqual(dataset["modalities"], ["ir", "rgb"])

    # ------------------------------------------------------------------ ZIP

    def test_zip_layout_round_trips(self):
        rel = self._source("zip")
        base = storage.project_dir(self.root, *rel.split("/"))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(base.rglob("*")):
                if path.is_file():
                    archive.writestr(path.relative_to(base).as_posix(), path.read_bytes())
        asset = self.upload_asset(buffer.getvalue(), "ir-ref.zip")
        dataset = self._import(asset_id=asset["id"], name="ZIP 版")
        self.assertEqual(dataset["reference_modality"], "ir")
        conn = self.conn()
        try:
            page = dataset_service.samples(
                conn, self.alice["id"], self.project["id"], dataset["id"]
            )
        finally:
            conn.close()
        self.assertEqual(len(page["samples"]), 2)
        self.assertTrue(all(item.get("labels_ir_rel") for item in page["samples"]))

    # ------------------------------------------------------------------ 校验

    def test_invalid_combinations_are_rejected(self):
        rel = self._source("invalid")
        cases = [
            (["ir", "edge"], "edge", "ir"),
            (["rgb"], "rgb", "rgb"),
            (["rgb"], "ir", "ir"),
            (["ir", "rgb"], "rgb", "edge"),
        ]
        for modalities, secondary, reference in cases:
            with self.assertRaises(ApiError) as caught:
                self._import(rel, f"坏组合-{reference}-{secondary}", modalities=modalities,
                             secondary=secondary, reference=reference)
            self.assertEqual(caught.exception.status, 400, (modalities, secondary, reference))

    def test_missing_auxiliary_image_is_rejected(self):
        rel = self._source("missing-aux")
        base = storage.project_dir(self.root, *rel.split("/"))
        (base / "images_ir" / "train" / "a.png").unlink()
        with self.assertRaises(ApiError) as caught:
            self._import(rel, "缺辅助图")
        self.assertIn(caught.exception.code, {"MISSING_MODALITY", "VALIDATION"})

    def test_broken_auxiliary_label_is_rejected(self):
        rel = self._source("bad-aux")
        base = storage.project_dir(self.root, *rel.split("/"))
        storage.write_bytes(self.root, base / "labels_ir" / "train" / "a.txt", b"0 0.5 0.5\n")
        with self.assertRaises(ApiError) as caught:
            self._import(rel, "坏辅助标注")
        self.assertEqual(caught.exception.code, "BAD_LABEL")

    # ------------------------------------------------------------------ 信任边界

    def test_foreign_manifest_ids_are_ignored(self):
        rel = self._source("manifest")
        base = storage.project_dir(self.root, *rel.split("/"))
        foreign = {
            "dataset_id": "dst_forged",
            "owner_id": 999999,
            "samples": [{"record_id": "det_forged", "cross_project_path": "../../etc/passwd"}],
        }
        storage.write_bytes(
            self.root, base / "manifest.json", json.dumps(foreign).encode("utf-8")
        )
        dataset = self._import(rel, "带伪造 manifest")
        conn = self.conn()
        try:
            row = conn.execute(
                "SELECT * FROM datasets WHERE id = ?", (dataset["id"],)
            ).fetchone()
            page = dataset_service.samples(
                conn, self.alice["id"], self.project["id"], dataset["id"]
            )
        finally:
            conn.close()
        self.assertEqual(row["owner_id"], int(self.alice["id"]))
        self.assertEqual(dataset["reference_modality"], "ir")
        # manifest 里的 record/样本来源不被采信；只有真实文件扫描结果入库。
        self.assertTrue(all(item["origin_record_id"] is None for item in page["samples"]))

    # ------------------------------------------------------------------ legacy 行

    def test_legacy_reference_column_wins_across_dataset_model_and_detection(self):
        legacy_id = f"dst_{secrets.token_hex(8)}"
        conn = self.conn()
        try:
            conn.execute(
                """
                INSERT INTO datasets
                    (id, project_id, owner_id, name, modalities, secondary_modality,
                     reference_modality, classes, root_rel, source, sample_count,
                     split_counts, status, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'ir', ?, ?, 'local', 0, '{}', 'ready', '', ?)
                """,
                (
                    legacy_id,
                    self.project["id"],
                    int(self.alice["id"]),
                    "legacy-IR",
                    json.dumps(["rgb", "ir"]),
                    "ir",
                    json.dumps(NAMES),
                    f"datasets/{legacy_id}",
                    "2026-11-05T00:00:00Z",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        conn = self.conn()
        try:
            row = conn.execute("SELECT * FROM datasets WHERE id = ?", (legacy_id,)).fetchone()
            exposed = dataset_service.public(row)
            model = self.make_model()
            conn.execute(
                "UPDATE models SET dataset_id = ? WHERE id = ?", (legacy_id, model["id"])
            )
            conn.commit()
            listed = model_service.list_for_project(
                conn, self.alice["id"], self.project["id"]
            )["models"]
            records = [{"model_id": model["id"]}]
            detection_service.attach_primary_modality(
                conn, self.alice["id"], self.project["id"], records
            )
        finally:
            conn.close()

        self.assertEqual(exposed["reference_modality"], "ir")
        self.assertEqual(exposed["primary_modality"], "ir")
        self.assertEqual(
            next(item for item in listed if item["id"] == model["id"])[
                "dataset_primary_modality"
            ],
            "ir",
        )
        self.assertEqual(records[0]["primary_modality"], "ir")
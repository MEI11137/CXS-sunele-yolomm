"""WB17-09：参考模态训练预检 + 单 IR 派生 + 反馈池 X 摘要（最小闭合回归）。

覆盖：
* IR 参考 + RGB 辅助的数据集默认拒绝训练，显式 single_ir 才派生单 IR；
* 非 IR 数据集不能开 single_ir；
* 生成运行时配置时 single_ir 只声明 IR，不把 RGB 辅助图塞进训练输入；
* 反馈池公开结构给出 X 模态/框数（x_box_count），box_count 旧语义不变。
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

import yaml
from PIL import Image

from backend.errors import ApiError
from backend.services import records as record_service
from backend.services import review as review_service
from backend.services import training as training_service
from backend.tests import test_business_wb17_04 as base04
from backend.tests.test_business_wb17_03 import png_bytes


def _png_bytes(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 80, 40)).save(buffer, "PNG")
    return buffer.getvalue()


class ReferenceFamilyTest(unittest.TestCase):
    def test_ir_reference_with_rgb_auxiliary_is_rejected_by_default(self):
        dataset = {"reference_modality": "ir", "secondary_modality": "rgb", "modalities": '["ir", "rgb"]'}
        with self.assertRaises(ApiError) as caught:
            training_service._family_for(None, 1, "p", dataset, None)
        self.assertEqual(caught.exception.code, "DATASET_REFERENCE_IR_UNSUPPORTED")
        self.assertIn("仅 IR 单模态", caught.exception.msg)

    def test_single_ir_explicitly_derives_single_modality_training(self):
        dataset = {"reference_modality": "ir", "secondary_modality": "rgb", "modalities": '["ir", "rgb"]'}
        family, secondary = training_service._family_for(None, 1, "p", dataset, None, single_ir=True)
        self.assertEqual((family, secondary), ("yolo", None))

    def test_single_ir_requires_ir_reference(self):
        dataset = {"reference_modality": "rgb", "secondary_modality": "ir", "modalities": '["rgb", "ir"]'}
        with self.assertRaises(ApiError) as caught:
            training_service._family_for(None, 1, "p", dataset, None, single_ir=True)
        self.assertEqual(caught.exception.code, "SINGLE_IR_REQUIRES_IR_DATASET")

    def test_reference_column_wins_over_modalities_order(self):
        dataset = {"reference_modality": "ir", "secondary_modality": None, "modalities": '["rgb", "ir"]'}
        self.assertEqual(training_service._dataset_reference(dataset), "ir")

    def test_validate_params_accepts_single_ir_boolean(self):
        clean = training_service.validate_params({"single_ir": True, "device": "cpu"})
        self.assertTrue(clean["single_ir"])


class RuntimeConfigSingleIrTest(unittest.TestCase):
    def test_single_ir_yaml_only_declares_ir(self):
        with tempfile.TemporaryDirectory(prefix="hx17-runtime-") as tmp:
            base = Path(tmp) / "data"
            for split in ("train", "val"):
                for folder in ("images", "images_rgb"):
                    directory = base / folder / split
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / f"a-{split}.png").write_bytes(_png_bytes(90, 120))
                labels = base / "labels" / split
                labels.mkdir(parents=True, exist_ok=True)
                (labels / f"a-{split}.txt").write_text("0 0.5 0.5 0.2 0.2\n", encoding="utf-8")
            data_yaml = base / "data.yaml"
            data_yaml.write_text(
                yaml.safe_dump(
                    {
                        "path": ".",
                        "train": "images/train",
                        "val": "images/val",
                        "nc": 1,
                        "names": ["单一热斑"],
                        "modalities": {"ir": "images", "rgb": "images_rgb"},
                        "modality_used": ["ir", "rgb"],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            runtime = training_service.generate_runtime_config(
                Path(tmp) / "run",
                data_yaml,
                family="yolomm",
                secondary="rgb",
                primary="ir",
                single_ir=True,
            )
            payload = yaml.safe_load(runtime.read_text(encoding="utf-8"))
        self.assertEqual(payload["modalities"], {"ir": "images"})
        self.assertEqual(payload["modality_used"], ["ir"])


class FeedbackXSummaryTest(base04.ReviewBase):
    def test_feedback_reports_x_modality_and_count(self):
        record = self.record(key="fb-x", x=True)
        conn = self.conn()
        try:
            review_service.submit(
                conn,
                self.alice["id"],
                self.project["id"],
                record["id"],
                boxes=[],
                x_boxes=[{"class_id": 1, "bbox": [10, 10, 40, 30]}],
                outcome="defect",
                base_version=0,
                request_key="fb-x-confirm",
            )
            feedback, created = record_service.add_feedback(
                conn, self.alice["id"], self.project["id"], record["id"], note="IR 可见缺陷", request_key="fb-x"
            )
            listed = record_service.list_feedback(
                conn, self.alice["id"], self.project["id"], status="all", limit=50
            )
        finally:
            conn.close()
        self.assertTrue(created)
        self.assertEqual(feedback["review"]["box_count"], 0)
        self.assertEqual(feedback["review"]["x_box_count"], 1)
        self.assertEqual(feedback["review"]["x_modality"], "ir")
        self.assertEqual(feedback["review"]["x_image"], {"width": 64, "height": 48})
        self.assertEqual(listed["feedback"][0]["review"]["x_box_count"], 1)


if __name__ == "__main__":
    unittest.main()
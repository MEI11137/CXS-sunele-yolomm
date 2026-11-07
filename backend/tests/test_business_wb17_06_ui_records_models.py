"""WB17-06 UI-03B：识别记录与模型管理页精简背后的共享服务行为。

钉住的是页面将要依赖的真实接口，不是页面本身（页面用真实浏览器探针另测）：

* 记录列表返回**真实文件名**（``rgb_filename``）与**人工状态**（``human_status``），
  AI 核查与人工状态是两个独立字段；
* ``q`` 按 RGB 原图文件名模糊搜索，``%``/``_`` 是普通字符不是通配；
* ``GET /training/device`` 报告训练设备的**真实能力**（探针可注入），
  plan/start 对不可用设备明确报 ``TRAINING_DEVICE_UNAVAILABLE``；
* 训练参数里的 ``device`` 规范化后真实落进计划条件（cpu / cuda / cuda:N）。
"""

from __future__ import annotations

import json
import secrets

import numpy as np

from backend.services import training as training_service
from backend.tests.support import body, csrf_headers
from backend.tests.test_business_wb17_03 import BusinessBase, png_bytes


class RecordsListTest(BusinessBase):
    def setUp(self):
        super().setUp()
        self._original_factory = None

        boxes = np.array([[3.0, 4.0, 20.0, 24.0, 0.9, 0]], dtype=float)

        class FakeDetector:
            def predict(self, **kwargs):
                return [type("R", (), {"boxes": boxes, "orig_shape": (48, 64)})()]

        from backend.vision import runtime as vision_runtime

        self._original_factory = vision_runtime._DETECTOR_FACTORY
        vision_runtime._DETECTOR_FACTORY = lambda path, mode: FakeDetector()
        self.model = self.make_model()

    def tearDown(self):
        from backend.vision import runtime as vision_runtime

        vision_runtime._DETECTOR_FACTORY = self._original_factory
        super().tearDown()

    def _detect_named(self, filename: str, *, key: str) -> dict:
        asset = self.upload_asset(png_bytes(), filename, role="rgb")
        return self.detect(self.model, asset["id"], key=key)

    def _list(self, **params):
        query = "&".join(f"{key}={value}" for key, value in params.items() if value != "")
        suffix = f"?{query}" if query else ""
        response = self.client.get(
            f"/api/projects/{self.project['id']}/detections{suffix}",
            headers=csrf_headers(self.client),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return body(response)["data"]

    def test_list_reports_real_filename_and_separate_human_status(self):
        record = self._detect_named("东部风机塔筒-001.png", key="fn-1")
        page = self._list()
        row = next(item for item in page["records"] if item["id"] == record["id"])
        self.assertEqual(row["rgb_filename"], "东部风机塔筒-001.png")
        self.assertEqual(row["human_status"], "pending")
        self.assertIn("latest_review", row, "AI 核查字段独立存在")

    def test_q_filters_by_filename_and_escapes_wildcards(self):
        first = self._detect_named("东部风机塔筒.png", key="q-1")
        self._detect_named("码头灯光.jpg", key="q-2")
        self._detect_named("100%负荷图.png", key="q-3")

        hit = self._list(q="风机")
        self.assertEqual([item["id"] for item in hit["records"]], [first["id"]])

        # ``%`` 是文件名里的普通字符：搜“%”只命中真带了 % 的那张，不变成全匹配
        percent = self._list(q="%")
        self.assertEqual(
            sorted(item["rgb_filename"] for item in percent["records"]),
            ["100%负荷图.png"],
        )
        # ``_`` 同理：三个下划线不是“任意三字符”
        underscore = self._list(q="__")
        self.assertEqual(underscore["records"], [])

    def test_human_status_follows_review_versions(self):
        record = self._detect_named("状态记录.png", key="hs-1")
        record_id = record["id"]
        self.assertEqual(self._list()["records"][0]["human_status"], "pending")
        # 草稿只算 draft，不算已确认
        self.client.post(
            f"/api/projects/{self.project['id']}/detections/{record_id}/review/draft",
            json={"boxes": record["boxes"], "note": "", "base_version": 0},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(self._list()["records"][0]["human_status"], "draft")
        # 提交确认后才算 confirmed（反馈入口依据的就是这个状态）
        response = self.client.post(
            f"/api/projects/{self.project['id']}/detections/{record_id}/review/submit",
            json={"boxes": record["boxes"], "outcome": "defect", "note": "", "base_version": 0},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        row = self._list()["records"][0]
        self.assertEqual(row["human_status"], "confirmed")
        self.assertEqual(row["review_version"], 1)


class TrainingDeviceTest(BusinessBase):
    def setUp(self):
        super().setUp()
        self._original_probe = training_service._device_probe

    def tearDown(self):
        training_service._device_probe = self._original_probe
        super().tearDown()

    def _trainable_dataset(self) -> str:
        """造一份带 data.yaml 的真实数据集目录（直接登记，不走导入全流程）。"""
        rel = f"datasets/train-{secrets.token_hex(4)}"
        from backend.services import storage

        base = storage.project_dir(self.root, *rel.split("/"))
        for split in ("train", "val"):
            storage.write_bytes(self.root, base / "images" / split / "a.png", png_bytes())
            storage.write_bytes(self.root, base / "labels" / split / "a.txt", b"0 0.5 0.5 0.2 0.2\n")
        storage.write_bytes(
            self.root,
            base / "data.yaml",
            b"path: .\ntrain: images/train\nval: images/val\nnames: [hotspot]\n",
        )
        conn = self.conn()
        try:
            dataset_id = f"dst_{secrets.token_hex(8)}"
            conn.execute(
                "INSERT INTO datasets (id, project_id, owner_id, name, modalities, classes,"
                " root_rel, source, status, sample_count, created_at)"
                " VALUES (?, ?, ?, '训练集', ?, ?, ?, 'local', 'ready', 2, '2026-11-05T00:00:00Z')",
                (
                    dataset_id,
                    self.project["id"],
                    int(self.alice["id"]),
                    json.dumps(["rgb"]),
                    json.dumps(["hotspot"]),
                    rel,
                ),
            )
            conn.commit()
            return dataset_id
        finally:
            conn.close()

    def _device(self) -> dict:
        response = self.client.get(
            f"/api/projects/{self.project['id']}/training/device",
            headers=csrf_headers(self.client),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return body(response)["data"]

    def test_device_endpoint_reports_cpu_always_and_cuda_per_probe(self):
        capabilities = self._device()
        self.assertTrue(capabilities["cpu"])
        self.assertIsInstance(capabilities["cuda_available"], bool)
        self.assertIsInstance(capabilities["detail"], str)

        training_service.set_device_probe(lambda device: {"ok": True, "detail": "injected-1-gpu"})
        capabilities = self._device()
        self.assertTrue(capabilities["cuda_available"])
        self.assertEqual(capabilities["detail"], "injected-1-gpu")

        training_service.set_device_probe(lambda device: {"ok": False, "detail": "injected-no-cuda"})
        capabilities = self._device()
        self.assertFalse(capabilities["cuda_available"])
        self.assertEqual(capabilities["detail"], "injected-no-cuda")

    def _plan(self, dataset_id: str, device: str):
        return self.client.post(
            f"/api/projects/{self.project['id']}/training/plan",
            json={"dataset_id": dataset_id, "params": {"device": device}},
            headers=csrf_headers(self.client),
        )

    def test_plan_passes_normalized_device_to_conditions(self):
        dataset_id = self._trainable_dataset()
        training_service.set_device_probe(lambda device: {"ok": True, "detail": "injected"})
        for raw, expect in (("gpu", "cuda"), ("cuda:1", "cuda:1"), ("gpu:0", "cuda:0")):
            response = self._plan(dataset_id, raw)
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            plan = body(response)["data"]
            self.assertEqual(plan["conditions"]["device"], expect)

    def test_plan_rejects_unavailable_device_with_real_reason(self):
        dataset_id = self._trainable_dataset()
        training_service.set_device_probe(
            lambda device: {"ok": False, "detail": "cuda_available=False, device_count=0"}
        )
        response = self._plan(dataset_id, "cuda")
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        payload = body(response)
        self.assertEqual(payload["error"], "TRAINING_DEVICE_UNAVAILABLE")
        self.assertIn("cuda_available=False", payload["msg"])

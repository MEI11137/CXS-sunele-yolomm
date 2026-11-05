"""WB17-03 业务层：资产、数据集、模型、检测、AI 核查。

这一批用例的共同底线：**不假装成功**。

* 资产：文件类型按真实内容判定（图片不是 ``project_file``），原始字节不被改写，
  跨账号拿不到别人的资产，内容流没有免鉴权入口；
* 数据集：缺类别、标签越界、ZIP 越界解压都必须明确报错，不用 RGB 顶替 IR；
* 检测：结果归一化对两种真实返回形态（torch ``Boxes`` 与 Nx6 ndarray）都成立，
  缺权重/缺模态/模型未发布都是显式失败，同一 request_key 不产生第二条记录；
* 核查：只用**本轮**模型（不是连接里可能过期的值）且有正向视觉证据；
  真图真发（RGB + 配套图），失败就是失败，绝不落一条 ``consistent``。

检测器一律通过 ``vision_runtime._DETECTOR_FACTORY`` 注入假实现——这只在测试里可用，
生产路径（``_detector``）没有这个开关，缺依赖/缺权重会直接报错。
"""

from __future__ import annotations

import hashlib
import io
import json
import secrets
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from backend.agent import identity, store
from backend.agent.gateway import TOKEN_HEADER
from backend.errors import ApiError
from backend.services import aireview, detection as detection_service, storage
from backend.services import assets as assets_service
from backend.services import datasets as dataset_service
from backend.services import jobs as job_service
from backend.vision import runtime as vision_runtime
from backend.tests.support import Harness, body, csrf_headers, login

import requests


def png_bytes(width: int = 64, height: int = 48, color=(120, 80, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


class StubProvider:
    """回环桩：记录真实请求体并返回指定 JSON，不连任何付费模型。"""

    def __init__(self, *, content: str | None = None, status: int = 200) -> None:
        self.requests: list[dict] = []
        self.status = status
        self.content = content or json.dumps(
            {
                "verdict": "possible_false_positive",
                "confidence": 0.72,
                "summary": "热斑位于边框附近，可能是反光造成的误检。",
                "findings": [{"kind": "reflection", "note": "高亮区域呈条状"}],
            },
            ensure_ascii=False,
        )
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: A003
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    outer.requests.append(json.loads(raw.decode("utf-8")))
                except ValueError:
                    outer.requests.append({"raw": raw.decode("utf-8", "replace")})
                if outer.status >= 400:
                    self.send_response(outer.status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":"boom"}')
                    return
                payload = json.dumps(
                    {"choices": [{"message": {"content": outer.content}}]}, ensure_ascii=False
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._server.server_address[1])
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class BusinessBase(unittest.TestCase):
    # 需要真实轮次的应用级集成（人工提交后的会话接力）把它打开，用假引擎跑；
    # 其余业务用例保持默认，不启动引擎进程。
    fake_engine = False

    def setUp(self):
        self.harness = Harness(fake_engine=self.fake_engine)
        self.alice = self.harness.create_user("alice", role="admin")
        self.root = self.harness.make_dir("alice-root")
        self.root_row = self.harness.add_root(self.root, grant_to=["alice"])
        self.project = self.harness.create_project(self.alice["id"], "光伏项目")
        self.harness.bind_project(self.project["id"], self.root, "alice")
        self.session = self.harness.create_session(self.project["id"], self.alice["id"])
        self.harness.save_connection(self.alice["id"])
        self.client = self.harness.client()
        login(self.client, "alice")
        self.stubs: list[StubProvider] = []

    def tearDown(self):
        for stub in self.stubs:
            stub.close()
        self.harness.close()

    # ------------------------------------------------------------------ 帮助

    def conn(self):
        return self.harness.conn()

    def upload(self, data: bytes, filename: str, *, role: str | None = None, client=None):
        client = client or self.client
        form = {"file": (io.BytesIO(data), filename)}
        if role:
            form["role"] = role
        return client.post(
            f"/api/projects/{self.project['id']}/assets/upload",
            data=form,
            headers=csrf_headers(client),
            content_type="multipart/form-data",
        )

    def upload_asset(self, data: bytes, filename: str, *, role: str | None = None) -> dict:
        response = self.upload(data, filename, role=role)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return body(response)["data"]["asset"]

    def stub(self, **kwargs) -> StubProvider:
        stub = StubProvider(**kwargs)
        self.stubs.append(stub)
        return stub

    def mark_vision(self, model: str, *, base_url: str, vision: bool = True) -> None:
        """模拟“检查连接”落下的能力证据（含它覆盖的模型）。"""
        conn = self.conn()
        try:
            row = identity.connection_for(conn, self.alice["id"])
            conn.execute(
                """
                UPDATE connections SET capabilities = ?, checked_at = ?, checked_base_url = ?,
                       checked_model = ?, checked_provider = ?, checked_fingerprint = ?
                 WHERE user_id = ?
                """,
                (
                    json.dumps(
                        {"auth": True, "chat": True, "vision": bool(vision), "tools": False}
                    ),
                    "2026-11-05T00:00:00Z",
                    base_url,
                    model,
                    str(row["provider"]),
                    str(row["key_fingerprint"]),
                    int(self.alice["id"]),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    # ---- 检测夹具：直接落库模型行 + 注入假检测器（生产路径无此开关）

    def make_model(self, *, mode: str = "single", secondary: str | None = None) -> dict:
        weight = self.root / "weights" / "best.pt"
        weight.parent.mkdir(parents=True, exist_ok=True)
        payload = b"not-a-real-weight"  # 只在被“加载”时才需要是真权重
        weight.write_bytes(payload)
        weight_sha = hashlib.sha256(payload).hexdigest()
        conn = self.conn()
        try:
            model_id = f"mdl_{secrets.token_hex(8)}"
            classes = [{"index": 0, "name": "单一热斑"}, {"index": 1, "name": "大面积热斑"}]
            conn.execute(
                """
                INSERT INTO models
                    (id, project_id, owner_id, name, family, model_mode, secondary_modality,
                     dataset_id, weight_rel, weight_sha256, classes, class_map, status, notes,
                     created_at, published_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, 'published', '', ?, ?)
                """,
                (
                    model_id,
                    self.project["id"],
                    int(self.alice["id"]),
                    f"{mode}-model-{model_id[-4:]}",
                    "yolo" if mode == "single" else "yolomm",
                    "single" if mode == "single" else "multimodal",
                    secondary,
                    "weights/best.pt",
                    weight_sha,
                    json.dumps(classes, ensure_ascii=False),
                    json.dumps({"0": 0, "1": 1}),
                    "2026-11-05T00:00:00Z",
                    "2026-11-05T00:00:00Z",
                ),
            )
            conn.commit()
            return dict(
                conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
            )
        finally:
            conn.close()

    def detect(self, model: dict, rgb_id: str, *, x_id: str | None = None, key: str = "k-1") -> dict:
        conn = self.conn()
        try:
            return detection_service.run_detection(
                conn,
                int(self.alice["id"]),
                self.project["id"],
                model_id=model["id"],
                rgb_asset_id=rgb_id,
                x_asset_id=x_id,
                request_key=key,
            )
        finally:
            conn.close()


# --------------------------------------------------------------------------- 资产


class AssetServiceTest(BusinessBase):
    def test_uploaded_image_keeps_bytes_and_is_not_a_project_file(self):
        data = png_bytes(80, 60)
        asset = self.upload_asset(data, "巡检图 01.png", role="rgb")
        self.assertEqual(asset["kind"], "image")
        self.assertNotEqual(asset["kind"], "project_file")
        self.assertEqual(asset["filename"], "巡检图 01.png")
        self.assertEqual(asset["size"], len(data))
        self.assertEqual(asset["mime"], "image/png")
        self.assertEqual((asset["width"], asset["height"]), (80, 60))
        self.assertTrue(asset["sha256"])

        response = self.client.get(
            f"/api/projects/{self.project['id']}/assets/{asset['id']}/content"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, data, "内容流必须返回原始字节")
        self.assertIn("inline", response.headers.get("Content-Disposition", "inline"))

    def test_weight_zip_is_detected_by_real_content(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("data.pkl", b"\x80\x02}q\x00.")
            archive.writestr("version", b"3\n")
        weight = self.upload_asset(buffer.getvalue(), "best.zip")
        self.assertEqual(weight["kind"], "weight")

        plain = io.BytesIO()
        with zipfile.ZipFile(plain, "w") as archive:
            archive.writestr("readme.txt", "hello")
        archive_asset = self.upload_asset(plain.getvalue(), "pack.zip")
        self.assertEqual(archive_asset["kind"], "archive")

    def test_other_account_cannot_touch_asset(self):
        asset = self.upload_asset(png_bytes(), "a.png")
        self.harness.create_user("bob")
        bob_client = self.harness.client()
        login(bob_client, "bob")
        for path in (
            f"/api/projects/{self.project['id']}/assets/{asset['id']}",
            f"/api/projects/{self.project['id']}/assets/{asset['id']}/content",
        ):
            response = bob_client.get(path)
            self.assertIn(response.status_code, (403, 404), path)

    def test_upload_route_requires_csrf_and_json_routes_stay_json_only(self):
        no_csrf = self.client.post(
            f"/api/projects/{self.project['id']}/assets/upload",
            data={"file": (io.BytesIO(png_bytes()), "a.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(no_csrf.status_code, 403, no_csrf.get_data(as_text=True))

        # 其它写接口不接受 multipart —— 只放开上传这一个端点。
        guarded = self.client.post(
            f"/api/projects/{self.project['id']}/assets/register",
            data={"rel_path": "x.png"},
            headers=csrf_headers(self.client),
            content_type="multipart/form-data",
        )
        self.assertEqual(guarded.status_code, 415, guarded.get_data(as_text=True))
        self.assertEqual(body(guarded)["error"], "UNSUPPORTED_MEDIA_TYPE")


# --------------------------------------------------------------------------- 数据集


class DatasetImportTest(BusinessBase):
    def _make_dataset_dir(self, *, with_names: bool = True, label: str = "0 0.5 0.5 0.2 0.2\n") -> Path:
        base = self.root / "ds"
        for split in ("train", "val"):
            (base / "images" / split).mkdir(parents=True, exist_ok=True)
            (base / "images_ir" / split).mkdir(parents=True, exist_ok=True)
            (base / "labels" / split).mkdir(parents=True, exist_ok=True)
            (base / "images" / split / "a.png").write_bytes(png_bytes())
            (base / "images_ir" / split / "a.png").write_bytes(png_bytes(color=(10, 10, 10)))
            (base / "labels" / split / "a.txt").write_text(label, encoding="utf-8")
        names = "names: [单一热斑, 大面积热斑]\n" if with_names else "nc: 2\n"
        (base / "data.yaml").write_text(
            "path: .\ntrain: images/train\nval: images/val\n" + names + "modalities: {rgb: images, ir: images_ir}\n",
            encoding="utf-8",
        )
        return base

    def import_dir(self, **kwargs):
        conn = self.conn()
        try:
            return dataset_service.import_dataset(
                conn,
                int(self.alice["id"]),
                self.project["id"],
                name=kwargs.pop("name", "巡检数据集"),
                modalities=kwargs.pop("modalities", ["rgb", "ir"]),
                secondary_modality=kwargs.pop("secondary_modality", "ir"),
                rel_path=kwargs.pop("rel_path", "ds"),
                **kwargs,
            )
        finally:
            conn.close()

    def test_directory_import_records_splits_and_pairing(self):
        self._make_dataset_dir()
        dataset = self.import_dir()
        self.assertEqual(dataset["sample_count"], 2)
        self.assertEqual(dataset["split_counts"]["train"], 1)
        self.assertEqual(dataset["split_counts"]["val"], 1)
        self.assertEqual(dataset["classes"], ["单一热斑", "大面积热斑"])
        self.assertEqual(dataset["secondary_modality"], "ir")
        self.assertEqual(dataset["status"], "ready")

        conn = self.conn()
        try:
            samples = dataset_service.samples(
                conn, int(self.alice["id"]), self.project["id"], dataset["id"]
            )
        finally:
            conn.close()
        self.assertEqual(len(samples["samples"]), 2)
        for sample in samples["samples"]:
            self.assertTrue(sample["x_rel"], "IR 配图必须登记，不能被丢掉")
            self.assertIn("images_ir", sample["x_rel"])

    def test_missing_class_table_asks_instead_of_guessing(self):
        self._make_dataset_dir(with_names=False)
        with self.assertRaises(ApiError) as caught:
            self.import_dir()
        self.assertEqual(caught.exception.code, "NEED_CLASSES")

    def test_label_class_index_out_of_range_is_rejected(self):
        self._make_dataset_dir(label="5 0.5 0.5 0.2 0.2\n")
        with self.assertRaises(ApiError) as caught:
            self.import_dir()
        self.assertEqual(caught.exception.code, "CLASS_OUT_OF_RANGE")

    def test_bbox_outside_image_is_rejected(self):
        self._make_dataset_dir(label="0 0.5 0.5 1.4 0.2\n")
        with self.assertRaises(ApiError) as caught:
            self.import_dir()
        self.assertEqual(caught.exception.code, "BAD_BBOX")

    def test_zip_with_path_traversal_is_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("../evil/a.png", png_bytes())
        asset = self.upload_asset(buffer.getvalue(), "ds.zip")
        conn = self.conn()
        try:
            with self.assertRaises(ApiError) as caught:
                dataset_service.import_dataset(
                    conn,
                    int(self.alice["id"]),
                    self.project["id"],
                    name="坏包",
                    modalities=["rgb"],
                    asset_id=asset["id"],
                )
        finally:
            conn.close()
        self.assertEqual(caught.exception.code, "ZIP_PATH_TRAVERSAL")
        self.assertFalse((self.root.parent / "evil").exists())


# --------------------------------------------------------------------------- 检测


class DetectionTest(BusinessBase):
    def setUp(self):
        super().setUp()
        self._original_factory = vision_runtime._DETECTOR_FACTORY

    def tearDown(self):
        vision_runtime._DETECTOR_FACTORY = self._original_factory
        super().tearDown()

    def _install(self, boxes) -> list[str]:
        calls: list[tuple] = []

        class FakeDetector:
            def predict(self, **kwargs):
                calls.append(kwargs)
                return [type("R", (), {"boxes": boxes, "orig_shape": (48, 64)})()]

        vision_runtime._DETECTOR_FACTORY = lambda path, mode: FakeDetector()
        self.detector_calls = calls
        return calls

    def test_target_dataset_class_names_follow_mapping(self):
        """非恒等映射：展示名必须来自目标数据集，并保留 native 追溯。"""
        boxes = np.array([[3.0, 4.0, 20.0, 24.0, 0.9, 0], [30.0, 10.0, 50.0, 30.0, 0.5, 1]])
        self._install(boxes)
        model = self.make_model()
        conn = self.conn()
        try:
            dataset_id = f"dst_{secrets.token_hex(8)}"
            conn.execute(
                "INSERT INTO datasets (id, project_id, owner_id, name, modalities, classes, root_rel,"
                " source, status, created_at) VALUES (?, ?, ?, '中文类别集', ?, ?,"
                " 'datasets/x', 'local', 'ready', '2026-11-05T00:00:00Z')",
                (
                    dataset_id,
                    self.project["id"],
                    int(self.alice["id"]),
                    json.dumps(["rgb"]),
                    json.dumps(["鸟粪污渍", "裂纹"], ensure_ascii=False),
                ),
            )
            conn.execute(
                "UPDATE models SET dataset_id = ?, classes = ?, class_map = ? WHERE id = ?",
                (
                    dataset_id,
                    json.dumps(
                        [
                            {"index": 0, "name": "single_hotspot"},
                            {"index": 1, "name": "large_area_hotspot"},
                        ],
                        ensure_ascii=False,
                    ),
                    json.dumps({"0": 1, "1": 0}),
                    model["id"],
                ),
            )
            conn.commit()
            refreshed = dict(
                conn.execute("SELECT * FROM models WHERE id = ?", (model["id"],)).fetchone()
            )
        finally:
            conn.close()

        rgb = self.upload_asset(png_bytes(), "rgb.png", role="rgb")
        record = self.detect(refreshed, rgb["id"], key="mapping-1")
        self.assertEqual(record["status"], "done", record)
        first, second = record["boxes"]
        self.assertEqual((first["native_class_id"], first["native_class_name"]), (0, "single_hotspot"))
        self.assertEqual((first["class_id"], first["class_name"]), (1, "裂纹"))
        self.assertEqual((second["class_id"], second["class_name"]), (0, "鸟粪污渍"))
        self.assertEqual(record["class_summary"], {"裂纹": 1, "鸟粪污渍": 1})

        conn = self.conn()
        try:
            page = detection_service.list_for_project(
                conn, self.alice["id"], self.project["id"], limit=5
            )
        finally:
            conn.close()
        self.assertEqual(page["records"][0]["boxes"][0]["class_name"], "裂纹")
        self.assertEqual(page["records"][0]["class_summary"].get("裂纹"), 1)

    def test_ndarray_and_torch_boxes_normalise_the_same(self):
        try:
            import torch
        except Exception:  # pragma: no cover - 环境没有 torch 时跳过这一分支
            torch = None

        ndarray_boxes = np.array([[3.0, 4.0, 20.0, 24.0, 0.9, 0], [30.0, 10.0, 50.0, 30.0, 0.5, 1]])
        self._install(ndarray_boxes)
        model = self.make_model()
        rgb = self.upload_asset(png_bytes(), "rgb.png", role="rgb")
        first = self.detect(model, rgb["id"], key="nd-1")
        self.assertEqual(first["status"], "done", first)
        self.assertEqual(first["hit_count"], 2)
        self.assertEqual(first["boxes"][0]["class_name"], "单一热斑")
        self.assertEqual(first["boxes"][1]["class_name"], "大面积热斑")

        if torch is None:
            self.skipTest("环境无 torch，仅验证 ndarray 形态")
        torch_boxes = torch.tensor(ndarray_boxes, dtype=torch.float32)
        self._install(torch_boxes)
        second = self.detect(model, rgb["id"], key="nd-2")
        self.assertEqual(second["status"], "done", second)
        for left, right in zip(first["boxes"], second["boxes"]):
            for index, value in enumerate(left["bbox"]):
                self.assertAlmostEqual(value, right["bbox"][index], places=3)
            self.assertAlmostEqual(left["confidence"], right["confidence"], places=3)
            self.assertEqual(left["class_name"], right["class_name"])

    def test_same_request_key_never_creates_a_second_record(self):
        self._install(np.array([[3.0, 4.0, 20.0, 24.0, 0.9, 0]]))
        model = self.make_model()
        rgb = self.upload_asset(png_bytes(), "rgb.png", role="rgb")
        first = self.detect(model, rgb["id"], key="dup-key")
        second = self.detect(model, rgb["id"], key="dup-key")
        self.assertEqual(first["id"], second["id"])
        conn = self.conn()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM detections WHERE request_key = ?", ("dup-key",)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(int(count), 1)

    def test_original_image_bytes_survive_inference(self):
        self._install(np.array([[3.0, 4.0, 20.0, 24.0, 0.9, 0]]))
        model = self.make_model()
        blob = png_bytes()
        rgb = self.upload_asset(blob, "rgb.png", role="rgb")
        record = self.detect(model, rgb["id"], key="bytes-1")
        self.assertEqual(record["status"], "done")
        self.assertNotEqual(record["result_url"], record["image_url"])
        response = self.client.get(
            f"/api/projects/{self.project['id']}/assets/{rgb['id']}/content"
        )
        self.assertEqual(response.data, blob, "推理不允许改写原图字节")

    def test_multimodal_model_refuses_to_fake_the_ir_input(self):
        self._install(np.array([[3.0, 4.0, 20.0, 24.0, 0.9, 0]]))
        model = self.make_model(mode="dual", secondary="ir")
        rgb = self.upload_asset(png_bytes(), "rgb.png", role="rgb")
        with self.assertRaises(ApiError) as caught:
            self.detect(model, rgb["id"], key="no-x")
        self.assertEqual(caught.exception.code, "VISION_X_REQUIRED")
        conn = self.conn()
        try:
            rows = conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(int(rows), 0, "缺模态时不应留下半截记录")

    def test_missing_weight_file_fails_instead_of_returning_empty_boxes(self):
        vision_runtime._DETECTOR_FACTORY = self._original_factory
        model = self.make_model()
        Path(self.root / "weights" / "best.pt").unlink()
        rgb = self.upload_asset(png_bytes(), "rgb.png", role="rgb")
        with self.assertRaises(ApiError) as caught:
            self.detect(model, rgb["id"], key="no-weight")
        self.assertEqual(caught.exception.code, "MODEL_WEIGHT_MISSING")
        conn = self.conn()
        try:
            rows = conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(int(rows), 0, "缺权重不能留下孤儿记录，更不能编造空结果")


# --------------------------------------------------------------------------- AI 核查


class AiReviewTest(BusinessBase):
    def setUp(self):
        super().setUp()
        self._original_factory = vision_runtime._DETECTOR_FACTORY
        vision_runtime._DETECTOR_FACTORY = lambda path, mode: _FixedDetector()

    def tearDown(self):
        vision_runtime._DETECTOR_FACTORY = self._original_factory
        super().tearDown()

    def _record(self, *, mode: str = "dual", key: str = "rec-1") -> dict:
        model = self.make_model(mode=mode, secondary="ir" if mode == "dual" else None)
        rgb = self.upload_asset(png_bytes(), "rgb.png", role="rgb")
        x = self.upload_asset(png_bytes(color=(8, 8, 8)), "ir.png", role="ir") if mode == "dual" else None
        return self.detect(model, rgb["id"], x_id=(x["id"] if x else None), key=key)

    def test_review_sends_both_rgb_and_ir_and_records_real_model(self):
        stub = self.stub()
        self.harness.save_connection(self.alice["id"], base_url=stub.base_url)
        self.mark_vision("probe-model", base_url=stub.base_url)
        record = self._record()

        conn = self.conn()
        try:
            job, _ = aireview.start_batch(
                conn,
                int(self.alice["id"]),
                self.project["id"],
                record_ids=[record["id"]],
                request_key="review-1",
            )
        finally:
            conn.close()
        result = job_service.wait(job["id"], timeout=60.0)
        self.assertEqual(result["status"], "done", result)

        self.assertEqual(len(stub.requests), 1, "一条记录只应发一次请求")
        payload = stub.requests[0]
        self.assertEqual(payload["model"], "probe-model")
        content = payload["messages"][1]["content"]
        images = [item for item in content if item.get("type") == "image_url"]
        self.assertEqual(len(images), 2, "RGB 与红外配套图必须一起发出，不能丢掉红外")
        self.assertEqual(payload["temperature"], 0)
        self.assertFalse(payload["stream"])

        conn = self.conn()
        try:
            row = conn.execute(
                "SELECT * FROM ai_reviews WHERE detection_id = ?", (record["id"],)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["verdict"], "possible_false_positive")
        self.assertEqual(row["model"], "probe-model")

    def test_review_only_touches_the_selected_record(self):
        stub = self.stub()
        self.harness.save_connection(self.alice["id"], base_url=stub.base_url)
        self.mark_vision("probe-model", base_url=stub.base_url)
        first = self._record(key="sel-1")
        second = self._record(key="sel-2")

        conn = self.conn()
        try:
            job, _ = aireview.start_batch(
                conn,
                int(self.alice["id"]),
                self.project["id"],
                record_ids=[second["id"]],
                request_key="review-sel",
            )
        finally:
            conn.close()
        self.assertEqual(job_service.wait(job["id"], timeout=60.0)["status"], "done")
        self.assertEqual(len(stub.requests), 1, "只选一条就不能核查全部")
        conn = self.conn()
        try:
            rows = conn.execute(
                "SELECT detection_id FROM ai_reviews ORDER BY detection_id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([row["detection_id"] for row in rows], [second["id"]])
        self.assertNotIn(first["id"], [row["detection_id"] for row in rows])

    def test_provider_failure_is_a_failure_not_a_consistent_verdict(self):
        stub = self.stub(status=500)
        self.harness.save_connection(self.alice["id"], base_url=stub.base_url)
        self.mark_vision("probe-model", base_url=stub.base_url)
        record = self._record()

        conn = self.conn()
        try:
            job, _ = aireview.start_batch(
                conn,
                int(self.alice["id"]),
                self.project["id"],
                record_ids=[record["id"]],
                request_key="review-fail",
            )
        finally:
            conn.close()
        result = job_service.wait(job["id"], timeout=60.0)
        self.assertEqual(result["status"], "failed", result)

        conn = self.conn()
        try:
            row = conn.execute(
                "SELECT * FROM ai_reviews WHERE detection_id = ?", (record["id"],)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["verdict"], "失败不能落任何结论，尤其不能是 consistent")
        self.assertEqual(row["error_code"], "REVIEW_PROVIDER_ERROR")

    def test_bad_json_from_model_is_rejected(self):
        stub = self.stub(content="看起来没问题，应该是误报。")
        self.harness.save_connection(self.alice["id"], base_url=stub.base_url)
        self.mark_vision("probe-model", base_url=stub.base_url)
        record = self._record()
        conn = self.conn()
        try:
            job, _ = aireview.start_batch(
                conn,
                int(self.alice["id"]),
                self.project["id"],
                record_ids=[record["id"]],
                request_key="review-json",
            )
        finally:
            conn.close()
        self.assertEqual(job_service.wait(job["id"], timeout=60.0)["status"], "failed")
        conn = self.conn()
        try:
            row = conn.execute(
                "SELECT status, verdict, error_code FROM ai_reviews WHERE detection_id = ?",
                (record["id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["verdict"])
        self.assertEqual(row["error_code"], "REVIEW_BAD_JSON")

    def test_review_refuses_when_this_turn_model_has_no_vision_evidence(self):
        """聊天用 A、业务却按连接里的 B 解析——这正是要禁止的分裂。"""
        stub = self.stub()
        self.harness.save_connection(
            self.alice["id"], base_url=stub.base_url, provider="deepseek", model="deepseek-chat"
        )
        # 证据只覆盖 deepseek-chat
        self.mark_vision("deepseek-chat", base_url=stub.base_url)
        conn = self.conn()
        try:
            store.save_prefs(conn, int(self.alice["id"]), mode="full", model="deepseek-reasoner")
            record = self._record()
            default = identity.resolve(conn, int(self.alice["id"]))
            self.assertEqual(default.model, "deepseek-reasoner")
            self.assertFalse(default.vision, "证据属于 chat，不属于 reasoner")
            # 传本轮真实模型 → 没有证据，直接拒绝，绝不退回 A
            with self.assertRaises(ApiError) as caught:
                aireview.start_batch(
                    conn,
                    int(self.alice["id"]),
                    self.project["id"],
                    record_ids=[record["id"]],
                    request_key="review-model-b",
                    model="deepseek-reasoner",
                )
            self.assertEqual(caught.exception.code, "VISION_UNVERIFIED")
        finally:
            conn.close()
        self.assertEqual(stub.requests, [], "没有证据时不能发任何计费请求")

        # 给 B 补证据后，请求里的 model 必须是 B
        self.mark_vision("deepseek-reasoner", base_url=stub.base_url)
        conn = self.conn()
        try:
            job, _ = aireview.start_batch(
                conn,
                int(self.alice["id"]),
                self.project["id"],
                record_ids=[record["id"]],
                request_key="review-model-b2",
                model="deepseek-reasoner",
            )
        finally:
            conn.close()
        self.assertEqual(job_service.wait(job["id"], timeout=60.0)["status"], "done")
        self.assertEqual(stub.requests[-1]["model"], "deepseek-reasoner")

    def test_job_model_snapshot_does_not_drift_after_switching_model(self):
        stub = self.stub()
        self.harness.save_connection(
            self.alice["id"], base_url=stub.base_url, provider="deepseek", model="deepseek-chat"
        )
        self.mark_vision("deepseek-chat", base_url=stub.base_url)
        record = self._record()
        conn = self.conn()
        try:
            job, _ = aireview.start_batch(
                conn,
                int(self.alice["id"]),
                self.project["id"],
                record_ids=[record["id"]],
                request_key="review-snapshot",
                model="deepseek-chat",
            )
            # 作业创建后立刻切到另一个模型（不允许影响已经在跑的作业）
            store.save_prefs(conn, int(self.alice["id"]), mode="full", model="deepseek-reasoner")
        finally:
            conn.close()
        self.assertEqual(job_service.wait(job["id"], timeout=60.0)["status"], "done")
        self.assertEqual(stub.requests[-1]["model"], "deepseek-chat")


class JobIntegrityTest(BusinessBase):
    """作业/核查的重复计费、异常收尾与重启恢复（独立脚本已覆盖，这里留最小回归）。"""

    def setUp(self):
        super().setUp()
        self._original_factory = vision_runtime._DETECTOR_FACTORY
        vision_runtime._DETECTOR_FACTORY = lambda path, mode: _FixedDetector()

    def tearDown(self):
        vision_runtime._DETECTOR_FACTORY = self._original_factory
        super().tearDown()

    def _record(self, *, key: str = "rec-1") -> dict:
        model = self.make_model(mode="dual", secondary="ir")
        rgb = self.upload_asset(png_bytes(), "rgb.png", role="rgb")
        x = self.upload_asset(png_bytes(color=(8, 8, 8)), "ir.png", role="ir")
        return self.detect(model, rgb["id"], x_id=x["id"], key=key)

    def _start(self, record_ids, key):
        conn = self.conn()
        try:
            job, _ = aireview.start_batch(
                conn,
                self.alice["id"],
                self.project["id"],
                record_ids=record_ids,
                request_key=key,
            )
            return job
        finally:
            conn.close()

    def test_duplicate_record_ids_send_only_one_request(self):
        stub = self.stub()
        self.harness.save_connection(self.alice["id"], base_url=stub.base_url)
        self.mark_vision("probe-model", base_url=stub.base_url)
        record = self._record()

        job = self._start([record["id"], record["id"]], "dup-ids")
        self.assertEqual(job_service.wait(job["id"], timeout=60.0)["status"], "done")
        self.assertEqual(len(stub.requests), 1, "同一个 ID 传两次不是两张图，只能发一次请求")

    def test_overlapping_review_is_rejected_before_second_request(self):
        stub = self.stub()
        self.harness.save_connection(self.alice["id"], base_url=stub.base_url)
        self.mark_vision("probe-model", base_url=stub.base_url)
        record = self._record()

        entered = threading.Event()
        release = threading.Event()
        calls: list[int] = []

        def held(*args, **kwargs):
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(15))
            return json.dumps(
                {"verdict": "consistent", "confidence": 0.8, "summary": "test-only", "findings": []}
            )

        with mock.patch.object(aireview, "_call", held):
            first = self._start([record["id"]], "overlap-a")
            self.assertTrue(entered.wait(10))
            with self.assertRaises(ApiError) as ctx:
                self._start([record["id"]], "overlap-b")
            release.set()
            self.assertEqual(job_service.wait(first["id"], timeout=60.0)["status"], "done")

        self.assertEqual(ctx.exception.code, "REVIEW_IN_PROGRESS")
        self.assertEqual(len(calls), 1, "在途核查只能有一条，第二次不能发请求")
        self.assertIn("job_id", ctx.exception.details or {})

    def test_caption_states_real_modality(self):
        stub = self.stub()
        self.harness.save_connection(self.alice["id"], base_url=stub.base_url)
        self.mark_vision("probe-model", base_url=stub.base_url)
        record = self._record()
        job = self._start([record["id"]], "caption")
        self.assertEqual(job_service.wait(job["id"], timeout=60.0)["status"], "done")
        captions = [
            item.get("text", "")
            for item in stub.requests[0]["messages"][1]["content"]
            if item.get("type") == "text"
        ]
        joined = "\n".join(captions)
        self.assertIn("可见光 RGB", joined)
        self.assertIn("红外 IR", joined)

        derived = aireview._modality_label(
            {"role": "edge", "origin": "derived", "meta": json.dumps({"derived_from": "ast_x"})}
        )
        self.assertIn("边缘图", derived)
        self.assertIn("不是真实红外", derived)
        self.assertIn("ast_x", derived)

    def test_malformed_provider_body_marks_review_terminal(self):
        stub = self.stub()
        self.harness.save_connection(self.alice["id"], base_url=stub.base_url)
        self.mark_vision("probe-model", base_url=stub.base_url)
        record = self._record()

        class Malformed:
            status_code = 200

            def json(self):
                return {"choices": ["not-a-message-object"]}

        with mock.patch("backend.services.aireview.requests.post", return_value=Malformed()):
            job = self._start([record["id"]], "malformed")
            result = job_service.wait(job["id"], timeout=60.0)

        self.assertEqual(result["status"], "failed")
        conn = self.conn()
        try:
            rows = conn.execute(
                "SELECT status, verdict FROM ai_reviews WHERE job_id = ?", (job["id"],)
            ).fetchall()
        finally:
            conn.close()
        self.assertTrue(rows)
        self.assertTrue(all(row["status"] != "running" for row in rows), "异常出口必须把核查行收尾")
        self.assertTrue(all(row["verdict"] is None for row in rows), "失败不能编造结论")

    def test_manual_detection_http_entry_returns_real_record(self):
        """人工巡检页走的就是这个入口：上传两张图 → POST /detections → 真记录 + 结果图。"""
        model = self.make_model(mode="dual", secondary="ir")
        rgb = self.upload_asset(png_bytes(), "manual-rgb.png", role="rgb")
        ir = self.upload_asset(png_bytes(color=(6, 6, 6)), "manual-ir.png", role="ir")
        response = self.client.post(
            f"/api/projects/{self.project['id']}/detections",
            json={
                "model_id": model["id"],
                "rgb_asset_id": rgb["id"],
                "x_asset_id": ir["id"],
                "confidence": 0.25,
            },
            headers=csrf_headers(self.client),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        record = body(response)["data"]["record"]
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["confidence"], 0.25)
        self.assertTrue(record["x_asset_id"])
        self.assertTrue(record["result_url"], "人工检测也要产出结果图")
        image = self.client.get(record["result_url"])
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.headers["Content-Type"], "image/jpeg")

    def test_restart_marks_unfinished_work_interrupted(self):
        from backend.app import create_app

        self._record(key="crash-record")
        conn = self.conn()
        try:
            orphan, _ = job_service.create(
                conn,
                project_id=self.project["id"],
                owner_id=self.alice["id"],
                kind="ai_review",
                request_key="simulated-crash",
                payload={"record_ids": []},
            )
            job_service.update(conn, orphan["id"], status="running")
            done_job, _ = job_service.create(
                conn,
                project_id=self.project["id"],
                owner_id=self.alice["id"],
                kind="ai_review",
                request_key="already-done",
                payload={},
            )
            job_service.update(conn, done_job["id"], status="done", result='{"done": 1}')
        finally:
            conn.close()

        self.harness.app.config["HX17_AGENT"].shutdown()
        restarted = create_app(settings=self.harness.settings)
        try:
            conn = self.conn()
            try:
                interrupted = job_service.public(job_service.get(conn, orphan["id"]))
                finished = job_service.public(job_service.get(conn, done_job["id"]))
            finally:
                conn.close()
        finally:
            restarted.config["HX17_AGENT"].shutdown()

        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(interrupted["error"]["code"], "SERVICE_RESTART")
        self.assertEqual(finished["status"], "done", "已完成的结果必须保留")


class MigrationIntegrityTest(unittest.TestCase):
    """v7 → v8 升级：重建 jobs/ai_reviews 时不能丢数据，且新状态真的可用。"""

    def test_v7_database_upgrades_without_losing_rows(self):
        import sqlite3
        import tempfile

        from backend import db

        workdir = Path(tempfile.mkdtemp(prefix="hx17-mig-"))
        path = workdir / "old.db"
        raw = sqlite3.connect(path)
        try:
            for version, script in db.MIGRATIONS:
                if version <= 7:
                    raw.executescript(script)
            raw.execute("PRAGMA user_version = 7")
            now = "2026-11-01T00:00:00Z"
            raw.execute(
                "INSERT INTO users (id, username, display_name, password_hash, role, status,"
                " token_version, created_at, updated_at) VALUES (1, 'u', 'u', 'x', 'member',"
                " 'active', 1, ?, ?)",
                (now, now),
            )
            raw.execute(
                "INSERT INTO projects (id, owner_id, name, description, status, created_at, updated_at)"
                " VALUES ('prj_old', 1, '旧库', '', 'active', ?, ?)",
                (now, now),
            )
            raw.execute(
                "INSERT INTO jobs (id, project_id, owner_id, kind, request_key, status, payload,"
                " created_at) VALUES ('job_old', 'prj_old', 1, 'ai_review', 'k1', 'done', '{}', ?)",
                (now,),
            )
            raw.execute(
                "INSERT INTO ai_reviews (id, project_id, owner_id, detection_id, job_id, status,"
                " verdict, created_at) VALUES ('rev_old', 'prj_old', 1, 'det_old', 'job_old', 'done',"
                " 'consistent', ?)",
                (now,),
            )
            raw.commit()
        finally:
            raw.close()

        conn = db.connect(path)
        try:
            db.ensure_schema(conn)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            job = conn.execute("SELECT * FROM jobs WHERE id = 'job_old'").fetchone()
            review = conn.execute("SELECT * FROM ai_reviews WHERE id = 'rev_old'").fetchone()
            conn.execute(
                "INSERT INTO ai_reviews (id, project_id, owner_id, detection_id, model,"
                " connection_fingerprint, status, created_at)"
                " VALUES ('rev_a', 'prj_old', 1, 'det_new', 'm', 'f', 'running', ?)",
                (now,),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ai_reviews (id, project_id, owner_id, detection_id, model,"
                    " connection_fingerprint, status, created_at)"
                    " VALUES ('rev_b', 'prj_old', 1, 'det_new', 'm', 'f', 'running', ?)",
                    (now,),
                )
        finally:
            conn.close()

        self.assertGreaterEqual(int(version), 8)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["request_key"], "k1")
        self.assertEqual(review["verdict"], "consistent")


class _FixedDetector:
    def predict(self, **kwargs):
        boxes = np.array([[3.0, 4.0, 20.0, 24.0, 0.88, 0]])
        return [type("R", (), {"boxes": boxes, "orig_shape": (48, 64)})()]


# --------------------------------------------------------------------------- 工具层身份


class ToolIdentitySnapshotTest(BusinessBase):
    def test_actor_uses_turn_model_not_the_connection_row(self):
        self.harness.save_connection(
            self.alice["id"], provider="deepseek", model="deepseek-chat"
        )
        runtime = self.harness.app.config["HX17_AGENT"]
        conn = self.conn()
        try:
            connection = identity.connection_for(conn, self.alice["id"])
            turn, _ = store.create(
                conn,
                session_id=self.session["id"],
                project_id=self.project["id"],
                owner_id=self.alice["id"],
                request_id="req-model-snapshot",
                mode="full",
                provider=str(connection["provider"]),
                model="deepseek-reasoner",
                fingerprint=identity.connection_fingerprint(connection),
            )
            store.mark_running(conn, turn["id"])
        finally:
            conn.close()

        token = runtime.gateway.issue_session(
            user_id=self.alice["id"],
            session_id=self.session["id"],
            project_id=self.project["id"],
            fingerprint=turn["connection_fingerprint"],
        )
        response = requests.post(
            f"{runtime.gateway.base_url}/v1/tools/hx_project_overview",
            headers={TOKEN_HEADER: token},
            json={"args": {}, "call_id": "call-model-snapshot"},
            timeout=30,
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["data"]["result"]
        self.assertEqual(result["model"]["model"], "deepseek-reasoner")
        self.assertNotEqual(result["model"]["model"], "deepseek-chat")


class StorageGuardTest(unittest.TestCase):
    """存储反例回归：越界联接、同名并发、同时间分页、权重反序列化。

    这批用例来自独立审查脚本，这里改写成本仓可重复运行的版本；构造数据全部在
    Harness 的临时工作区内，不碰真实项目目录。
    """

    def setUp(self) -> None:
        self.h = Harness()
        self.user = self.h.create_user("storage_guard")
        self.root = self.h.make_dir("guard-root")
        self.outside = self.h.make_dir("guard-outside")
        self.h.add_root(self.root, grant_to=["storage_guard"])
        self.project = self.h.create_project(self.user["id"], "存储守卫")
        self.h.bind_project(self.project["id"], self.root, "storage_guard")

    def tearDown(self) -> None:
        self.h.close()

    def test_junction_escape_is_rejected_before_writing_outside(self) -> None:
        import subprocess

        link = self.root / "uploads"
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(self.outside)],
            capture_output=True,
        )
        if made.returncode != 0:
            self.skipTest("本机无法创建 NTFS junction，跳过越界反例")
        conn = self.h.conn()
        try:
            with self.assertRaises(ApiError) as ctx:
                assets_service.add_bytes(
                    conn,
                    self.user["id"],
                    self.project["id"],
                    data=png_bytes(),
                    filename="outside-write.png",
                )
        finally:
            conn.close()
            # 只解除联接本身，绝不递归删除它指向的目标。
            self.assertEqual(link.resolve(), self.outside.resolve())
            link.rmdir()
        self.assertIn(ctx.exception.code, {"PATH_OUTSIDE_PROJECT", "PATH_PRIVATE"})
        self.assertEqual(list(self.outside.rglob("outside-write.png")), [])

    def test_same_name_concurrent_uploads_keep_both_files(self) -> None:
        import concurrent.futures

        # 把 unique_path 换成“永远返回同一个候选名”，模拟最坏情况：
        # 旧实现靠“查一次不存在”，两个线程会拿到同一路径并互相覆盖。
        # 现在的保证来自“每个资产一个目录”，因此即使 unique_path 完全不抗并发也不会撞车。
        def same_name(directory, filename):
            return directory / filename

        def upload(color):
            conn = self.h.conn()
            try:
                return assets_service.add_bytes(
                    conn,
                    self.user["id"],
                    self.project["id"],
                    data=png_bytes(color=color),
                    filename="pair.png",
                )
            finally:
                conn.close()

        with mock.patch.object(storage, "unique_path", same_name):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                rows = list(pool.map(upload, [(255, 0, 0), (0, 0, 255)]))

        conn = self.h.conn()
        try:
            paths = set()
            for row in rows:
                found = assets_service.get_owned(
                    conn, self.user["id"], self.project["id"], row["id"]
                )
                paths.add(found["rel_path"])
                assets_service.read_bytes(conn, self.user["id"], self.project["id"], row["id"])
        finally:
            conn.close()
        self.assertEqual(len(paths), 2, "同名并发上传必须落到不同路径")
        self.assertIn("pair.png", {path.rsplit("/", 1)[-1] for path in paths})
        self.assertEqual({row["filename"] for row in rows}, {"pair.png"})

    def test_pagination_with_identical_timestamps_does_not_lose_rows(self) -> None:
        conn = self.h.conn()
        try:
            first = assets_service.add_bytes(
                conn, self.user["id"], self.project["id"], data=png_bytes(), filename="a.png"
            )
            second = assets_service.add_bytes(
                conn, self.user["id"], self.project["id"], data=png_bytes(color=(9, 9, 9)), filename="b.png"
            )
            conn.execute("UPDATE assets SET created_at = ?", ("2026-11-05T00:00:00Z",))
            conn.commit()
            page = assets_service.list_for_project(
                conn, self.user["id"], self.project["id"], limit=1
            )
            self.assertTrue(page["has_more"])
            following = assets_service.list_for_project(
                conn, self.user["id"], self.project["id"], limit=10, before=page["next_before"]
            )
        finally:
            conn.close()
        ids = {row["id"] for row in page["assets"] + following["assets"]}
        self.assertEqual(ids, {first["id"], second["id"]})

    def test_unreviewed_symbol_is_not_imported(self) -> None:
        """未审核符号必须“连 import 都不发生”，不是 import 之后再拒绝。"""
        try:
            import torch
        except Exception:  # pragma: no cover
            self.skipTest("本机没有 torch")
        import sys
        import zipfile

        from backend.vision import safe_weight

        sandbox = self.h.make_dir("import-probe")
        package = f"hx17_probe_{secrets.token_hex(4)}"
        marker_file = sandbox / "imported.txt"
        pkg_dir = sandbox / package
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
        (pkg_dir / "evil.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker_file)!r}).write_text('imported')\n"
            "class Probe://n    pass\n",
            encoding="utf-8",
        )

        base = sandbox / "base.pt"
        torch.save({"model": 1}, base)
        crafted = sandbox / "crafted.pt"
        payload = b"\x80\x02c" + f"{package}.evil\nProbe\n".encode("utf-8") + b")R."
        with zipfile.ZipFile(base) as source:
            entries = {name: source.read(name) for name in source.namelist()}
        target = next(name for name in entries if name.endswith("data.pkl"))
        entries[target] = payload
        with zipfile.ZipFile(crafted, "w") as output:
            for name, blob in entries.items():
                output.writestr(name, blob)

        sys.path.insert(0, str(sandbox))
        try:
            with self.assertRaises(vision_runtime.VisionError) as ctx:
                vision_runtime.inspect_weight(crafted)
        finally:
            sys.path.remove(str(sandbox))
        self.assertEqual(ctx.exception.code, "VISION_WEIGHT_UNTRUSTED")
        self.assertIn("ALLOWED_SYMBOLS", ctx.exception.message)
        self.assertFalse(marker_file.exists(), "未审核符号的模块不能被执行/导入")
        self.assertNotIn(f"{package}.evil", sys.modules)
        self.assertTrue(safe_weight.ALLOWED_SYMBOLS)

    def test_untrusted_weight_is_rejected_without_running_pickle(self) -> None:
        try:
            import torch
        except Exception:  # pragma: no cover - 无 torch 的环境直接跳过
            self.skipTest("本机没有 torch，跳过权重反序列化反例")
        from backend.vision import safe_weight

        marker = self.root / "unpickle-marker.txt"

        class Probe:
            def __reduce__(self):
                return (Path.write_text, (marker, "test-only marker"))

        weight = self.root / "untrusted-probe.pt"
        torch.save({"model": Probe()}, weight)

        with self.assertRaises(vision_runtime.VisionError) as ctx:
            vision_runtime.inspect_weight(weight)
        self.assertEqual(ctx.exception.code, "VISION_WEIGHT_UNTRUSTED")
        self.assertFalse(marker.exists(), "受限加载不能执行 pickle 里的可调用对象")

        # 推理入口同样受管：vendor 内部走 torch.load(weights_only=False)，也被闸门拦住。
        vision_runtime.activate_vendor()
        with self.assertRaises(safe_weight.WeightRejected):
            torch.load(str(weight), map_location="cpu", weights_only=False)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()

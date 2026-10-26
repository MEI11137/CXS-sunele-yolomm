"""C-03/C-04/C-05/C-06: the detection → review → feedback → training chain.

These tests drive the tools exactly as the planner would: prepare, inspect the
preview, execute with a request key. They use the real service layer and the
real SQLite schema; only the model runtime and the user's model client are
fakes, because those cannot run in a test.
"""

import hashlib
import json
import sqlite3
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
import yaml

from clients.file_client import FileClient
from services.business_tools import BusinessToolAdapter, ResourceSnapshot
from services.file_access import FileAccess, LocalFileAssetGateway
from services.tool_common import ToolConflict, ToolRefused
from utils.db import Database


class Fixture:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.db_path = tmp_path / "app.sqlite3"
        database = Database()
        database.db_path = str(self.db_path)
        database.initialize_db()

        self.store_dir = tmp_path / "store"
        self.store_dir.mkdir()
        self.storage = FileClient(base_path=self.store_dir)
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.projects = {}
        self.denied = set()
        self.events = []
        self.policy = lambda user_id, project_id, action: (
            user_id == 7 and project_id == "project-1"
        )
        self.access = FileAccess(self.db_path, project_access=self.policy)
        self.gateway = LocalFileAssetGateway(
            self.db_path, self.storage, project_access=self.policy, work_dir=self.work,
        )
        self.model_client = lambda: SimpleNamespace(name="user-client")
        self.launched = []

    # -------------------------------------------------------------- helpers

    def require_access(self, principal, action, resource):
        if principal != 7 or action in self.denied:
            return False
        return True

    def resolve_resource(self, principal, kind, resource_id):
        if kind == "workspace":
            return ResourceSnapshot(str(principal), "project-1", 7, "v1")
        return ResourceSnapshot(resource_id, "project-1", 7,
                                self.projects.get(resource_id, "v1"))

    def adapter(self, **overrides) -> BusinessToolAdapter:
        options = dict(
            require_access=self.require_access,
            resolve_resource=self.resolve_resource,
            storage_exists=lambda bucket, key: True,
            file_assets=self.gateway,
            storage_client=self.storage,
            model_client_factory=self.model_client,
            detector_loader=lambda path, mode, yaml_path: SimpleNamespace(),
            notify=lambda principal, event: self.events.append(event),
            work_dir=str(self.work),
            candidate_root=str(self.root / "candidates"),
            training_launcher=lambda **payload: self.launched.append(payload),
        )
        options.update(overrides)
        return BusinessToolAdapter(self.db_path, **options)

    def put(self, bucket: str, name: str, data: bytes) -> dict:
        local = self.root / f"put-{uuid.uuid4().hex[:8]}-{name}"
        local.write_bytes(data)
        return self.storage.upload(bucket, str(local))

    def upload(self, local: Path, kind: str, mime: str = "application/octet-stream",
               project_id: str = "project-1") -> str:
        stored = self.storage.upload("project_uploads", str(local))
        digest = hashlib.sha256(local.read_bytes()).hexdigest()
        asset_id = self.access.register_upload(
            7, stored["bucket"], stored["objectKey"], kind, mime,
            local.stat().st_size, project_id, original_name=local.name,
            sha256=digest, status="uploaded",
        )
        self.projects.setdefault(project_id, "v1")
        return asset_id

    def sql(self, statement: str, params=()):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(statement, params).fetchall()

    def dataset(self, *, name="sea-v1", classes=("crack", "hotspot"), mode="single",
                secondary=None, bucket="datasets", key="stored.zip", status=1) -> str:
        dataset_id = str(uuid.uuid4())
        structure = {"names": list(classes), "class_count": len(classes)}
        self.sql(
            "INSERT INTO datasets (id, name, description, zip_bucket, zip_object_key, "
            "structure_info, upload_time, file_size, status, dataset_mode, "
            "primary_modality, secondary_modality, runtime_mapping) "
            "VALUES (?, ?, '', ?, ?, ?, '2026-01-01', 10, ?, ?, 'rgb', ?, '{}')",
            (dataset_id, name, bucket, key, json.dumps(structure, ensure_ascii=False),
             status, mode, secondary),
        )
        self.projects[dataset_id] = "v1"
        return dataset_id

    def model(self, dataset_id, *, name="model-v1", status=3, mode="single",
              secondary=None, parent=None, version=1, bucket=None,
              key=None) -> str:
        stored = self.put("models", "weight.pt", b"PK\x03\x04fake-weight")
        bucket = bucket or stored["bucket"]
        key = key or stored["objectKey"]
        model_id = str(uuid.uuid4())
        self.sql(
            "INSERT INTO models (id, name, description, dataset_id, base_model, epochs, "
            "batch_size, status, model_bucket, model_object_key, create_time, model_mode, "
            "primary_modality, secondary_modality, publish_status, version, parent_model_id, "
            "metrics_json) VALUES (?, ?, '', ?, 'base.pt', 10, 4, ?, ?, ?, "
            "'2026-01-01', ?, 'rgb', ?, 'published', ?, ?, ?)",
            (model_id, name, dataset_id, status, bucket, key, mode, secondary,
             version, parent, json.dumps({"mAP50": 0.5})),
        )
        self.projects[model_id] = "v1"
        return model_id

    def record(self, model_id, *, version=0, status=None, annotations=None) -> str:
        record_id = str(uuid.uuid4())
        original = self.put("detection_original", "a.jpg", b"jpeg-bytes")
        result = self.put("detection_results", "r.jpg", b"jpeg-result")
        self.sql(
            "INSERT INTO detection_records (id, user_id, model_id, model_name, "
            "original_image_bucket, original_image_object_key, result_image_bucket, "
            "result_image_object_key, detection_result, confidence_threshold, create_time, "
            "human_review_status, review_version, corrected_annotations, record_status) "
            "VALUES (?, 7, ?, 'model-v1', ?, ?, ?, ?, '[]', 0.3, '2026-01-01', ?, ?, ?, "
            "'completed')",
            (record_id, model_id, original["bucket"], original["objectKey"],
             result["bucket"], result["objectKey"], status or "pending", version,
             json.dumps(annotations or [], ensure_ascii=False)),
        )
        self.projects[record_id] = "v1"
        return record_id


def _png(path: Path) -> Path:
    image = np.zeros((64, 64, 3), dtype=np.uint8)
    image[:, :] = (10, 20, 30)
    cv2.imwrite(str(path), image)
    return path


def _dataset_zip(path: Path, *, classes=("crack", "hotspot"), images=None) -> Path:
    images = images or {"train/a": "0 0.5 0.5 0.2 0.2", "val/b": "1 0.5 0.5 0.2 0.2"}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("data.yaml", yaml.safe_dump({
            "path": ".", "train": "./images/train", "val": "./images/val",
            "names": list(classes),
        }))
        for key, label in images.items():
            archive.writestr(f"images/{key}.jpg", b"image-" + key.encode())
            archive.writestr(f"labels/{key}.txt", label)
    return path


@pytest.fixture
def fixture(tmp_path):
    return Fixture(tmp_path)


# ------------------------------------------------------------------- C-03b


def test_weight_import_requires_a_real_class_mapping(fixture):
    dataset_id = fixture.dataset(classes=())
    fixture.sql("UPDATE datasets SET structure_info=? WHERE id=?",
                (json.dumps({"names": []}), dataset_id))
    weight = fixture.root / "weight.pt"
    weight.write_bytes(b"PK\x03\x04fake")
    asset_id = fixture.upload(weight, "model_weight")
    adapter = fixture.adapter()

    prepared = adapter.prepare_tool(7, "model.import_weight", {
        "asset_id": asset_id, "dataset_id": dataset_id, "name": "imported-v1",
    })
    preview = prepared["preview"]
    assert preview["blocked"] is True
    assert {item["code"] for item in preview["blocked_reasons"]} == {"CLASS_MAPPING_UNKNOWN"}
    with pytest.raises(ToolRefused):
        adapter.execute_tool(7, prepared["operation_id"], "weight-1")


def test_weight_import_registers_model_and_binds_the_upload(fixture):
    dataset_id = fixture.dataset()
    weight = fixture.root / "weight.pt"
    weight.write_bytes(b"PK\x03\x04fake")
    asset_id = fixture.upload(weight, "model_weight")
    adapter = fixture.adapter()

    prepared = adapter.prepare_tool(7, "model.import_weight", {
        "asset_id": asset_id, "dataset_id": dataset_id, "name": "imported-v1",
    })
    assert prepared["preview"]["weight_contract"]["class_names"] == ["crack", "hotspot"]
    assert prepared["preview"]["blocked"] is False

    result = adapter.execute_tool(7, prepared["operation_id"], "weight-1")
    assert result["status"] == "succeeded"
    assert result["outputs"]["model_mode"] == "single"
    assert result["outputs"]["source_file_id"] == asset_id
    rows = fixture.sql("SELECT status, model_mode, dataset_id FROM models WHERE id=?",
                       (result["outputs"]["model_id"],))
    assert rows[0][0] == 1 and rows[0][1] == "single" and rows[0][2] == dataset_id
    assert fixture.access.get_binding(asset_id) == {
        "business_type": "model", "business_id": result["outputs"]["model_id"],
    }
    assert fixture.events and fixture.events[-1]["tool"] == "model.import_weight"


# --------------------------------------------------------------------- C-04


def test_multimodal_detection_stops_without_a_real_pair(fixture):
    dataset_id = fixture.dataset(mode="multimodal", secondary="infrared")
    model_id = fixture.model(dataset_id, mode="multimodal", secondary="infrared")
    image = fixture.upload(_png(fixture.root / "a.png"), "image", "image/png")
    adapter = fixture.adapter()

    prepared = adapter.prepare_tool(7, "detection.run", {
        "asset_id": image, "model_id": model_id,
    })
    assert prepared["preview"]["blocked"] is True
    assert prepared["preview"]["blocked_reasons"][0]["code"] == "MODALITY_PAIR_REQUIRED"


def test_detection_uses_a_published_model_and_stores_a_record(fixture):
    dataset_id = fixture.dataset()
    model_id = fixture.model(dataset_id, status=3)
    image = fixture.upload(_png(fixture.root / "a.png"), "image", "image/png")
    adapter = fixture.adapter(
        detector_predictor=lambda detector, resolved, confidence: SimpleNamespace(
            detections=[{"class_id": 0, "confidence": 0.9, "bbox": [1, 2, 20, 30]}]
        ),
    )

    prepared = adapter.prepare_tool(7, "detection.run", {
        "asset_id": image, "model_id": model_id, "confidence": 0.4,
    })
    assert prepared["preview"]["blocked"] is False
    result = adapter.execute_tool(7, prepared["operation_id"], "detect-1")
    assert result["status"] == "succeeded"
    assert result["outputs"]["detection_count"] == 1
    record_id = result["outputs"]["record_id"]
    assert result["business_refs"][0]["id"] == record_id
    rows = fixture.sql("SELECT model_id, user_id, detection_result FROM detection_records "
                       "WHERE id=?", (record_id,))
    assert rows[0][0] == model_id and rows[0][1] == 7
    assert json.loads(rows[0][2])[0]["class_id"] == 0

    # An unpublished model is refused before any inference happens.
    draft_model = fixture.model(dataset_id, name="draft", status=1)
    blocked = adapter.prepare_tool(7, "detection.run",
                                   {"asset_id": image, "model_id": draft_model})
    assert blocked["preview"]["blocked"] is True
    assert blocked["preview"]["blocked_reasons"][0]["code"] == "MODEL_NOT_PUBLISHED"
    with pytest.raises(ToolRefused):
        adapter.execute_tool(7, blocked["operation_id"], "detect-draft")


def test_ai_review_is_one_paid_call_per_record_with_per_item_results(fixture):
    dataset_id = fixture.dataset()
    model_id = fixture.model(dataset_id)
    first = fixture.record(model_id)
    second = fixture.record(model_id, version=2)

    class Client:
        def __init__(self):
            self.calls = 0

        def review_detection_structured(self, image_bytes, detections):
            self.calls += 1
            return {"decision": "needs_human_review", "summary": "存在疑点",
                    "suspicions": [{"bbox": [0.1, 0.1, 0.2, 0.2], "reason": "边缘模糊"}]}

    client = Client()
    adapter = fixture.adapter(model_client_factory=lambda: client)

    prepared = adapter.prepare_tool(7, "review.ai", {"record_ids": [first, second, first]})
    preview = prepared["preview"]
    assert preview["billable"] is True
    assert [item["record_id"] for item in preview["records"]] == [first, second]
    result = adapter.execute_tool(7, prepared["operation_id"], "review-1")
    assert result["status"] == "succeeded"
    assert result["outputs"]["requested"] == 2
    assert result["outputs"]["succeeded"] == 2
    assert client.calls == 2


# --------------------------------------------------------------------- C-05


def test_human_review_then_feedback_only_accepts_confirmed_samples(fixture):
    dataset_id = fixture.dataset()
    model_id = fixture.model(dataset_id)
    record_id = fixture.record(model_id, version=0)
    adapter = fixture.adapter()

    feedback = adapter.prepare_tool(7, "feedback.add_confirmed", {
        "record_id": record_id, "dataset_id": dataset_id,
    })
    assert feedback["preview"]["blocked"] is True
    with pytest.raises(ToolRefused):
        adapter.execute_tool(7, feedback["operation_id"], "feedback-early")

    review = adapter.prepare_tool(7, "review.submit_human", {
        "record_id": record_id, "expected_version": 0, "decision": "no_defect",
        "annotations": [],
    })
    result = adapter.execute_tool(7, review["operation_id"], "review-submit")
    assert result["outputs"]["human_review_status"] == "confirmed"
    assert result["outputs"]["review_version"] == 1

    stale = adapter.prepare_tool(7, "review.submit_human", {
        "record_id": record_id, "expected_version": 0, "decision": "rejected",
    })
    assert stale["preview"]["blocked"] is True

    accepted = adapter.prepare_tool(7, "feedback.add_confirmed", {
        "record_id": record_id, "dataset_id": dataset_id,
    })
    done = adapter.execute_tool(7, accepted["operation_id"], "feedback-ok")
    assert done["outputs"]["created"] is True
    assert fixture.sql("SELECT COUNT(*) FROM feedback_samples")[0][0] == 1


# --------------------------------------------------------------------- C-06


def test_training_refuses_a_second_run_instead_of_queueing(fixture):
    dataset_id = fixture.dataset()
    parent = fixture.model(dataset_id, status=1, version=3)
    fixture.sql("UPDATE models SET epochs=5, batch_size=2 WHERE id=?", (parent,))
    with sqlite3.connect(fixture.db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS training_lock ("
            "lock_id INTEGER PRIMARY KEY CHECK (lock_id = 1), "
            "owner TEXT NOT NULL, holder_pid INTEGER, acquired_at TEXT NOT NULL)"
        )
        conn.execute("DELETE FROM training_lock")
        conn.execute("INSERT INTO training_lock VALUES (1, 'other-model', ?, 'now')",
                     (__import__("os").getpid(),))

    adapter = fixture.adapter()
    prepared = adapter.prepare_tool(7, "training.start", {
        "parent_model_id": parent, "dataset_id": dataset_id,
    })
    assert prepared["preview"]["blocked"] is True
    assert prepared["preview"]["blocked_reasons"][0]["code"] == "TRAINING_BUSY"
    with pytest.raises(ToolRefused):
        adapter.execute_tool(7, prepared["operation_id"], "train-busy")


def test_training_creates_a_new_version_and_reports_persisted_state(fixture):
    dataset_id = fixture.dataset()
    parent = fixture.model(dataset_id, status=1, version=3)
    fixture.sql("UPDATE models SET epochs=5, batch_size=2 WHERE id=?", (parent,))
    adapter = fixture.adapter()

    prepared = adapter.prepare_tool(7, "training.start", {
        "parent_model_id": parent, "dataset_id": dataset_id, "epochs": 7,
    })
    assert prepared["preview"]["new_model"]["version"] == 4
    result = adapter.execute_tool(7, prepared["operation_id"], "train-1")
    assert result["status"] == "succeeded"
    child = result["outputs"]["model_id"]
    assert fixture.launched and fixture.launched[0]["model_id"] == child
    rows = fixture.sql("SELECT status, version, parent_model_id, epochs FROM models WHERE id=?",
                       (child,))
    assert rows[0] == (0, 4, parent, 7)
    state = adapter.execute_tool(
        7, adapter.prepare_tool(7, "training.get_state", {"model_id": child})["operation_id"],
        "train-state",
    )
    assert state["outputs"]["state"] == "training"

    with sqlite3.connect(fixture.db_path) as conn:
        locked = conn.execute("SELECT owner FROM training_lock WHERE lock_id=1").fetchone()
    assert locked[0] == child


def test_model_comparison_reports_that_conditions_differ(fixture):
    dataset_id = fixture.dataset()
    parent = fixture.model(dataset_id, status=3)
    child = fixture.model(dataset_id, name="child", status=1, parent=parent, version=2)
    fixture.sql("UPDATE models SET epochs=50 WHERE id=?", (parent,))
    fixture.sql("UPDATE models SET epochs=10 WHERE id=?", (child,))
    adapter = fixture.adapter()

    prepared = adapter.prepare_tool(7, "model.compare", {
        "parent_model_id": parent, "child_model_id": child,
    })
    result = adapter.execute_tool(7, prepared["operation_id"], "compare-1")
    assert result["outputs"]["conditions"]["comparable"] is False
    assert "不能直接归因" in result["outputs"]["conditions"]["note"]

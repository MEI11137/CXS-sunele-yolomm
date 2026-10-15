import io
import json
from types import SimpleNamespace

from flask import Flask
from PIL import Image

from utils.jwt_util import generate_token


def _jpeg(color=(120, 120, 120)):
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), color).save(buffer, "JPEG")
    buffer.seek(0)
    return buffer


def test_detect_route_returns_automatically_saved_record_and_accepts_x_image(monkeypatch, tmp_path):
    import routes.detection as detection_route

    captured = {}

    class FakeDb:
        def query(self, sql, params=(), fetchone=False):
            return {
                "id": "model-mm",
                "name": "dual model",
                "model_bucket": "models",
                "model_object_key": "dual.pt",
                "model_mode": "multimodal",
                "primary_modality": "rgb",
                "secondary_modality": "ir",
                "model_yaml": "baseline.yaml",
            }

    def fake_execute_detection(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            record_id="record-auto",
            original_image_url="http://files/original.jpg",
            result_image_url="http://files/result.jpg",
            secondary_image_url="http://files/ir.jpg",
            secondary_image_origin="uploaded",
            detections=[],
            detection_count=0,
            model_id="model-mm",
            model_name="dual model",
            confidence_threshold=0.5,
            storage={
                "original": {"bucket": "original", "objectKey": "original.jpg"},
                "secondary": {"bucket": "secondary", "objectKey": "ir.jpg"},
                "result": {"bucket": "results", "objectKey": "result.jpg"}
            },
        )

    monkeypatch.setattr(detection_route, "db", FakeDb())
    monkeypatch.setattr(detection_route, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(detection_route, "execute_detection", fake_execute_detection, raising=False)

    app = Flask(__name__)
    app.register_blueprint(detection_route.detection_bp)
    response = app.test_client().post(
        "/api/detection/detect",
        headers={"Authorization": f"Bearer {generate_token(3)}"},
        data={
            "model_id": "model-mm",
            "confidence_threshold": "0.5",
            "image": (_jpeg(), "rgb.jpg"),
            "x_image": (_jpeg((50, 50, 50)), "ir.jpg"),
        },
        content_type="multipart/form-data",
    )
    body = json.loads(response.data)

    assert body["code"] == 200
    assert body["data"]["record_id"] == "record-auto"
    assert body["data"]["record_saved"] is True
    assert captured["user_id"] == 3
    assert captured["model_meta"]["secondary_modality"] == "ir"
    assert captured["x_path"] is not None


def test_detect_route_reports_missing_ir_as_client_error(monkeypatch, tmp_path):
    import routes.detection as detection_route
    from services.yolo_runtime import MissingModalityError

    class FakeDb:
        def query(self, sql, params=(), fetchone=False):
            return {
                "id": "model-mm",
                "name": "dual model",
                "model_bucket": "models",
                "model_object_key": "dual.pt",
                "model_mode": "multimodal",
                "primary_modality": "rgb",
                "secondary_modality": "ir",
                "model_yaml": "baseline.yaml",
            }

    monkeypatch.setattr(detection_route, "db", FakeDb())
    monkeypatch.setattr(detection_route, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(
        detection_route,
        "execute_detection",
        lambda **kwargs: (_ for _ in ()).throw(MissingModalityError("ir模态必须上传")),
        raising=False,
    )

    app = Flask(__name__)
    app.register_blueprint(detection_route.detection_bp)
    response = app.test_client().post(
        "/api/detection/detect",
        headers={"Authorization": f"Bearer {generate_token(3)}"},
        data={
            "model_id": "model-mm",
            "image": (_jpeg(), "rgb.jpg"),
        },
        content_type="multipart/form-data",
    )
    body = json.loads(response.data)

    assert body["code"] == 400
    assert "ir模态" in body["msg"]


def test_legacy_save_updates_auto_saved_record_instead_of_duplicating(monkeypatch):
    import routes.detection as detection_route

    captured = {}

    class FakeDb:
        def query(self, sql, params=(), fetchone=False):
            return {"id": "record-auto", "user_id": 3}

        def execute(self, sql, params=()):
            captured["sql"] = sql
            captured["params"] = params
            return 1

    monkeypatch.setattr(detection_route, "db", FakeDb())
    app = Flask(__name__)
    app.register_blueprint(detection_route.detection_bp)
    response = app.test_client().post(
        "/api/detection/save",
        headers={"Authorization": f"Bearer {generate_token(3)}"},
        json={
            "title": "renamed",
            "description": "kept",
            "save_data": {"record_id": "record-auto"},
        },
    )
    body = json.loads(response.data)

    assert body["code"] == 200
    assert body["data"]["id"] == "record-auto"
    assert "UPDATE detection_records" in captured["sql"]
    assert "INSERT" not in captured["sql"]


def test_detection_record_exposes_multimodal_and_review_state():
    from utils.models import DetectionRecord

    payload = DetectionRecord(
        {
            "id": "r1",
            "user_id": 1,
            "model_id": "m1",
            "model_name": "dual",
            "original_image_bucket": "original",
            "original_image_object_key": "rgb.jpg",
            "secondary_image_bucket": "secondary",
            "secondary_image_object_key": "edge.jpg",
            "secondary_image_origin": "generated",
            "record_status": "completed",
            "ai_review_status": "not_requested",
            "human_review_status": "pending",
        }
    ).to_dict()

    assert payload["secondary_image_url"].endswith("/secondary/edge.jpg")
    assert payload["secondary_image_origin"] == "generated"
    assert payload["record_status"] == "completed"
    assert payload["ai_review_status"] == "not_requested"
    assert payload["human_review_status"] == "pending"

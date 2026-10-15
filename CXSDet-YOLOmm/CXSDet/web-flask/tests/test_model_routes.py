import json
from io import BytesIO

from flask import Flask

from utils.jwt_util import generate_token


def test_import_model_route_accepts_authenticated_multipart_weight(monkeypatch):
    import routes.model as model_route
    import utils.security_utils as security_utils

    captured = {}

    class Imported:
        def to_dict(self):
            return {
                "id": "imported-1",
                "name": "external edge",
                "dataset_id": "dataset-mm",
                "status": 1,
                "model_mode": "multimodal",
                "secondary_modality": "edge",
                "publish_status": "draft",
            }

    def fake_import(name, description, dataset_id, upload, database, storage, loader):
        captured.update(
            name=name,
            description=description,
            dataset_id=dataset_id,
            filename=upload.filename,
            database=database,
            storage=storage,
            loader=loader,
        )
        return Imported()

    monkeypatch.setattr(model_route, "register_imported_model", fake_import, raising=False)
    monkeypatch.setattr(security_utils, "is_admin", lambda: True)

    app = Flask(__name__)
    app.register_blueprint(model_route.model_bp)
    response = app.test_client().post(
        "/api/model/import",
        headers={"Authorization": f"Bearer {generate_token(1)}"},
        data={
            "name": "external edge",
            "description": "trained elsewhere",
            "dataset_id": "dataset-mm",
            "weight": (BytesIO(b"checkpoint"), "best.pt"),
        },
        content_type="multipart/form-data",
    )
    body = json.loads(response.data)

    assert response.status_code == 200
    assert body["code"] == 200
    assert body["data"]["id"] == "imported-1"
    assert body["data"]["publish_status"] == "draft"
    assert captured["name"] == "external edge"
    assert captured["dataset_id"] == "dataset-mm"
    assert captured["filename"] == "best.pt"


def test_create_model_persists_dataset_modality_and_version(monkeypatch):
    import routes.model as model_route
    import utils.security_utils as security_utils

    captured = {}

    class FakeDb:
        def query(self, sql, params=(), fetchone=False):
            if "FROM models WHERE name" in sql:
                return None
            if "FROM datasets" in sql:
                return {
                    "id": "dataset-mm",
                    "dataset_mode": "multimodal",
                    "primary_modality": "rgb",
                    "secondary_modality": "edge",
                }
            return None

        def execute(self, sql, params=()):
            captured["sql"] = sql
            captured["params"] = params
            return 1

    class FakeProcess:
        def __init__(self, target, args):
            captured["target"] = target
            captured["args"] = args
            self.daemon = False

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(model_route, "db", FakeDb())
    monkeypatch.setattr(model_route.multiprocessing, "Process", FakeProcess)
    monkeypatch.setattr(security_utils, "is_admin", lambda: True)

    app = Flask(__name__)
    app.register_blueprint(model_route.model_bp)
    response = app.test_client().post(
        "/api/model/create",
        headers={"Authorization": f"Bearer {generate_token(1)}"},
        json={
            "name": "dual-edge-v1",
            "dataset_id": "dataset-mm",
            "base_model": "yolo11n.pt",
            "epochs": 1,
            "batch_size": 2,
        },
    )
    body = json.loads(response.data)

    assert body["code"] == 200
    assert body["data"]["model_mode"] == "multimodal"
    assert body["data"]["secondary_modality"] == "edge"
    assert body["data"]["version"] == 1
    assert "model_mode" in captured["sql"]
    assert "model_yaml" in captured["sql"]
    assert "publish_status" in captured["sql"]
    assert captured["started"] is True


def test_model_vo_exposes_version_and_modality_metadata():
    from utils.models import Model, ModelVO

    payload = ModelVO(
        Model(
            {
                "id": "model-2",
                "name": "rgb-edge",
                "dataset_id": "dataset-mm",
                "base_model": "yolo11n.pt",
                "model_mode": "multimodal",
                "primary_modality": "rgb",
                "secondary_modality": "edge",
                "model_yaml": "baseline.yaml",
                "parent_model_id": "model-1",
                "version": 2,
                "metrics_json": '{"mAP50": 0.6}',
                "publish_status": "draft",
            }
        ),
        "dual dataset",
    ).to_dict()

    assert payload["model_mode"] == "multimodal"
    assert payload["secondary_modality"] == "edge"
    assert payload["parent_model_id"] == "model-1"
    assert payload["version"] == 2
    assert payload["publish_status"] == "draft"

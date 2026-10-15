import io
import json
import zipfile

import yaml
from flask import Flask

from utils.jwt_util import generate_token


def _valid_single_zip() -> io.BytesIO:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "data.yaml",
            yaml.safe_dump(
                {
                    "path": ".",
                    "train": "./images/train",
                    "val": "./images/val",
                    "names": ["crack"],
                }
            ),
        )
        archive.writestr("images/train/a.jpg", b"train")
        archive.writestr("labels/train/a.txt", "0 0.5 0.5 0.2 0.2")
        archive.writestr("images/val/b.jpg", b"val")
        archive.writestr("labels/val/b.txt", "0 0.5 0.5 0.2 0.2")
    buffer.seek(0)
    return buffer


def _valid_multi_zip_without_yaml() -> io.BytesIO:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for split in ("train", "val"):
            archive.writestr(f"nested/images/{split}/a.jpg", b"rgb")
            archive.writestr(f"nested/images_edge/{split}/a.png", b"edge")
            archive.writestr(f"nested/images_depth/{split}/a.png", b"depth")
            archive.writestr(f"nested/labels/{split}/a.txt", "0 0.5 0.5 0.2 0.2")
    buffer.seek(0)
    return buffer


def test_upload_persists_selected_dataset_mode_and_runtime_mapping(monkeypatch):
    import routes.dataset as dataset_route
    import utils.security_utils as security_utils

    captured = {}

    class FakeDb:
        def query(self, sql, params=(), fetchone=False):
            return None

        def execute(self, sql, params=()):
            captured["sql"] = sql
            captured["params"] = params
            return 1

    class FakeFileClient:
        def upload(self, bucket, file_path):
            return {"bucket": bucket, "objectKey": "uploaded.zip"}

    monkeypatch.setattr(dataset_route, "db", FakeDb())
    monkeypatch.setattr(dataset_route, "file_client", FakeFileClient())
    monkeypatch.setattr(security_utils, "is_admin", lambda: True)

    app = Flask(__name__)
    app.register_blueprint(dataset_route.dataset_bp)
    client = app.test_client()
    response = client.post(
        "/api/dataset/upload",
        headers={"Authorization": f"Bearer {generate_token(1)}"},
        data={
            "name": "single-v2",
            "description": "new contract",
            "dataset_mode": "single",
            "file": (_valid_single_zip(), "dataset.zip"),
        },
        content_type="multipart/form-data",
    )
    body = json.loads(response.data)

    assert body["code"] == 200
    assert body["data"]["dataset_mode"] == "single"
    assert body["data"]["primary_modality"] == "rgb"
    assert "dataset_mode" in captured["sql"]
    assert "runtime_mapping" in captured["sql"]
    assert "single" in captured["params"]
    assert "rgb" in captured["params"]


def test_upload_rejects_missing_mode_before_file_storage(monkeypatch):
    import routes.dataset as dataset_route
    import utils.security_utils as security_utils

    class FailIfCalledFileClient:
        def upload(self, bucket, file_path):
            raise AssertionError("invalid upload must not reach file storage")

    monkeypatch.setattr(dataset_route.file_client, "upload", FailIfCalledFileClient().upload)
    monkeypatch.setattr(security_utils, "is_admin", lambda: True)

    app = Flask(__name__)
    app.register_blueprint(dataset_route.dataset_bp)
    response = app.test_client().post(
        "/api/dataset/upload",
        headers={"Authorization": f"Bearer {generate_token(1)}"},
        data={"name": "missing-mode", "file": (_valid_single_zip(), "dataset.zip")},
        content_type="multipart/form-data",
    )
    body = json.loads(response.data)

    assert body["code"] == 400
    assert "数据集类型" in body["msg"]


def test_upload_generates_yaml_for_multimodal_archive_when_fields_are_supplied(monkeypatch):
    import routes.dataset as dataset_route
    import utils.security_utils as security_utils

    captured = {}

    class FakeDb:
        def query(self, sql, params=(), fetchone=False):
            return None

        def execute(self, sql, params=()):
            return 1

    class FakeFileClient:
        def upload(self, bucket, file_path):
            with zipfile.ZipFile(file_path) as archive:
                captured["yaml"] = yaml.safe_load(archive.read("nested/data.yaml"))
            return {"bucket": bucket, "objectKey": "generated.zip"}

    monkeypatch.setattr(dataset_route, "db", FakeDb())
    monkeypatch.setattr(dataset_route, "file_client", FakeFileClient())
    monkeypatch.setattr(security_utils, "is_admin", lambda: True)

    app = Flask(__name__)
    app.register_blueprint(dataset_route.dataset_bp)
    response = app.test_client().post(
        "/api/dataset/upload",
        headers={"Authorization": f"Bearer {generate_token(1)}"},
        data={
            "name": "generated-rgb-edge",
            "dataset_mode": "multimodal",
            "secondary_modality": "edge",
            "class_names": "hotspot,crack",
            "file": (_valid_multi_zip_without_yaml(), "dataset.zip"),
        },
        content_type="multipart/form-data",
    )
    body = json.loads(response.data)

    assert body["code"] == 200
    assert body["data"]["secondary_modality"] == "edge"
    assert captured["yaml"]["modality_used"] == ["rgb", "edge"]
    assert captured["yaml"]["names"] == ["hotspot", "crack"]

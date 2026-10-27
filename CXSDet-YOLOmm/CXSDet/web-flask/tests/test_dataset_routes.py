import io
import json
import sqlite3
import zipfile
from types import SimpleNamespace

import yaml
from flask import Flask

from utils.jwt_util import generate_token

from tests import guard_support


def _dataset_db(tmp_path):
    """Minimal datasets table so the shared import service can register."""

    class TempDatabase:
        def __init__(self, path):
            self.path = str(path)
            with sqlite3.connect(self.path) as conn:
                conn.execute(
                    """
                    CREATE TABLE datasets (
                        id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
                        zip_bucket TEXT NOT NULL, zip_object_key TEXT NOT NULL,
                        structure_info TEXT, upload_time TEXT NOT NULL,
                        file_size INTEGER, status INTEGER DEFAULT 0,
                        dataset_mode TEXT NOT NULL DEFAULT 'single',
                        primary_modality TEXT NOT NULL DEFAULT 'rgb',
                        secondary_modality TEXT, runtime_mapping TEXT
                    )
                    """
                )

        def get_connection(self):
            return sqlite3.connect(self.path)

        def query(self, sql, params=(), fetchone=False):
            with sqlite3.connect(self.path) as conn:
                cursor = conn.execute(sql, params)
                return cursor.fetchone() if fetchone else cursor.fetchall()

        def execute(self, sql, params=()):
            with sqlite3.connect(self.path) as conn:
                return conn.execute(sql, params).rowcount

    return TempDatabase(tmp_path / "routes.sqlite3")


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


def test_upload_persists_selected_dataset_mode_and_runtime_mapping(monkeypatch, tmp_path):
    import routes.dataset as dataset_route
    import utils.security_utils as security_utils

    database = _dataset_db(tmp_path)
    captured = {}

    class FakeFileClient:
        def upload(self, bucket, file_path):
            captured["bucket"] = bucket
            return {"bucket": bucket, "objectKey": "uploaded.zip"}

    monkeypatch.setattr(dataset_route, "db", database)
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
    assert body["data"]["class_names"] == ["crack"]
    assert body["data"]["samples"] == 2
    assert captured["bucket"] == "datasets"
    with sqlite3.connect(database.path) as conn:
        row = conn.execute(
            "SELECT dataset_mode, primary_modality, runtime_mapping, structure_info, "
            "zip_object_key, status FROM datasets WHERE name='single-v2'"
        ).fetchone()
    assert row[0] == "single"
    assert row[1] == "rgb"
    assert json.loads(row[2])["rgb_directory"] == "images"
    assert json.loads(row[3])["names"] == ["crack"]
    assert row[4] == "uploaded.zip"
    assert row[5] == 1


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


def test_upload_generates_yaml_for_multimodal_archive_when_fields_are_supplied(monkeypatch, tmp_path):
    import routes.dataset as dataset_route
    import utils.security_utils as security_utils

    captured = {}

    class FakeFileClient:
        def upload(self, bucket, file_path):
            with zipfile.ZipFile(file_path) as archive:
                captured["yaml"] = yaml.safe_load(archive.read("nested/data.yaml"))
            return {"bucket": bucket, "objectKey": "generated.zip"}

    monkeypatch.setattr(dataset_route, "db", _dataset_db(tmp_path))
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


def test_feedback_version_endpoint_returns_a_draft_for_manual_splitting(monkeypatch, tmp_path):
    import routes.dataset as dataset_route
    import utils.security_utils as security_utils
    from services.dataset_draft_service import DraftDatasetResult

    captured = {}

    def create_draft(dataset_id, work_dir, creator_id, database, storage_client):
        captured.update(
            {
                "dataset_id": dataset_id,
                "creator_id": creator_id,
                "database": database,
                "storage_client": storage_client,
            }
        )
        return DraftDatasetResult(
            id="draft-1",
            name="feedback-draft",
            source_type="feedback",
            source_dataset_id=dataset_id,
            workflow_status="draft",
        )

    monkeypatch.setattr(dataset_route, "create_feedback_dataset_draft", create_draft)
    monkeypatch.setattr(dataset_route, "db", object())
    monkeypatch.setattr(dataset_route, "file_client", object())
    monkeypatch.setattr(dataset_route, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(security_utils, "is_admin", lambda: True)

    app = Flask(__name__)
    # 拿本人数据集做反馈版本：授权按归属判断，先把 d1 登记给用户 7
    guard_support.install(
        app,
        tmp_path / "guard.sqlite3",
        resources=[guard_support.resource("datasets", "d1", 7)],
    )
    app.register_blueprint(dataset_route.dataset_bp)
    response = app.test_client().post(
        "/api/dataset/d1/feedback-version",
        headers={"Authorization": f"Bearer {generate_token(7)}"},
    )
    body = json.loads(response.data)

    assert body["code"] == 200
    assert body["msg"] == "反馈样本已生成待划分数据集"
    assert body["data"]["workflow_status"] == "draft"
    assert body["data"]["source_dataset_id"] == "d1"
    assert captured["creator_id"] == 7


def _draft_client(monkeypatch, db_path, *, admin=True, accounts=None, resources=None,
                  owner_id=8):
    """建一个带临时库的客户端：授权现在按真实归属判断，角色开关不够用。"""
    import utils.security_utils as security_utils
    from flask import Flask
    import routes.dataset as dataset_route

    monkeypatch.setattr(security_utils, "is_admin", lambda: admin)
    app = Flask(__name__)
    guard_support.install(
        app,
        db_path,
        accounts=accounts,
        resources=(
            resources
            if resources is not None
            else [guard_support.resource("dataset_drafts", "draft-1", owner_id)]
        ),
    )
    app.register_blueprint(dataset_route.dataset_bp)
    return app.test_client()


def test_draft_samples_route_is_owner_only(monkeypatch, tmp_path):
    # 不是本人创建的草稿：普通用户拿不到样本清单（原来是"只有管理员能看"）
    client = _draft_client(
        monkeypatch,
        tmp_path / "guard.sqlite3",
        admin=False,
        accounts={8: 0},
        resources=[guard_support.resource("dataset_drafts", "draft-1", 99)],
    )
    response = client.get(
        "/api/dataset/drafts/draft-1/samples",
        headers={"Authorization": f"Bearer {generate_token(8)}"},
    )
    assert json.loads(response.data)["code"] == 403


def test_dataset_list_unifies_records_and_excludes_finalized_drafts(monkeypatch, tmp_path):
    import routes.dataset as dataset_route

    captured = []
    formal = {
        "id": "draft-final", "name": "finalized", "description": None,
        "structure_info": "{}", "upload_time": "2026-10-01", "file_size": 2,
        "status": 1, "dataset_mode": "single", "primary_modality": "rgb",
    }
    draft = {
        "id": "draft-open", "name": "pending split", "description": None,
        "create_time": "2026-10-02", "workflow_status": "draft",
        "metadata_json": "{}", "sample_count": 4, "unassigned_count": 2,
        "train_count": 1, "val_count": 1, "test_count": 0, "excluded_count": 0,
        "dataset_mode": "single", "primary_modality": "rgb",
    }

    class FakeDb:
        def query(self, sql, params=(), fetchone=False):
            captured.append(sql)
            if "FROM datasets" in sql:
                return [formal]
            if "FROM dataset_drafts" in sql:
                return [draft]
            raise AssertionError(sql)

    monkeypatch.setattr(dataset_route, "db", FakeDb())
    # 列表按归属过滤；管理员不过滤，这里只校验两种记录怎么合并、草稿状态怎么筛
    client = _draft_client(monkeypatch, tmp_path / "guard.sqlite3", accounts={8: 1})
    response = client.get(
        "/api/dataset/list?record_kind=all&page=1&page_size=10",
        headers={"Authorization": f"Bearer {generate_token(8)}"},
    )
    body = json.loads(response.data)
    assert body["code"] == 200
    assert [item["record_kind"] for item in body["data"]["datasets"]] == ["draft", "formal"]
    assert body["data"]["datasets"][0]["split_counts"]["unassigned"] == 2
    draft_query = next(sql for sql in captured if "FROM dataset_drafts" in sql)
    assert "workflow_status != 'finalized'" in draft_query

    filtered = client.get(
        "/api/dataset/list?record_kind=draft&workflow_status=draft",
        headers={"Authorization": f"Bearer {generate_token(8)}"},
    )
    filtered_body = json.loads(filtered.data)
    assert filtered_body["code"] == 200
    assert len(filtered_body["data"]["datasets"]) == 1
    assert "workflow_status = ?" in captured[-1]


def test_draft_assignment_route_passes_atomic_sample_selection(monkeypatch, tmp_path):
    import routes.dataset as dataset_route

    captured = {}
    monkeypatch.setattr(dataset_route, "assign_draft_samples", lambda *args: captured.update({"args": args}) or {"updated": 2})
    monkeypatch.setattr(dataset_route, "db", object())
    client = _draft_client(monkeypatch, tmp_path / "guard.sqlite3")
    response = client.post(
        "/api/dataset/drafts/draft-1/assign",
        headers={"Authorization": f"Bearer {generate_token(8)}"},
        json={"sample_ids": ["s1", "s2"], "split": "train"},
    )
    body = json.loads(response.data)
    assert body["code"] == 200
    assert captured["args"][:3] == ("draft-1", ["s1", "s2"], "train")


def test_sample_preview_streams_stored_image_with_derived_mime(monkeypatch, tmp_path):
    import routes.dataset as dataset_route
    from services.dataset_draft_service import DraftSampleResult

    sample = DraftSampleResult(
        id="sample-1", draft_id="draft-1", sample_key="frame_000001",
        rgb_bucket="images", rgb_object_key="path/frame_000001.png",
    )
    monkeypatch.setattr(dataset_route, "get_draft", lambda *_args: {"id": "draft-1"})
    monkeypatch.setattr(dataset_route, "get_draft_sample", lambda *_args: sample)
    monkeypatch.setattr(dataset_route.file_client, "get", lambda bucket, key: b"png bytes")
    monkeypatch.setattr(dataset_route, "db", object())
    client = _draft_client(monkeypatch, tmp_path / "guard.sqlite3")
    response = client.get(
        "/api/dataset/drafts/draft-1/samples/sample-1/preview/rgb",
        headers={"Authorization": f"Bearer {generate_token(8)}"},
    )
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data == b"png bytes"


def test_secondary_repair_reports_sample_ownership_validation(monkeypatch, tmp_path):
    import routes.dataset as dataset_route
    from services.dataset_draft_service import DraftSampleResult, DraftValidationError

    class FakeFileClient:
        def __init__(self):
            self.deleted = []

        def upload(self, bucket, path):
            return {"bucket": bucket, "objectKey": "repair.jpg"}

        def delete(self, bucket, key):
            self.deleted.append((bucket, key))

    storage = FakeFileClient()
    monkeypatch.setattr(dataset_route, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(dataset_route, "file_client", storage)
    monkeypatch.setattr(
        dataset_route,
        "get_draft_sample",
        lambda *_args: DraftSampleResult(
            id="foreign-sample", draft_id="draft-1", sample_key="frame-1"
        ),
    )
    monkeypatch.setattr(
        dataset_route,
        "repair_draft_sample_secondary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(DraftValidationError("样本不存在或不属于该数据集")),
    )
    monkeypatch.setattr(dataset_route, "db", object())
    client = _draft_client(monkeypatch, tmp_path / "guard.sqlite3")
    response = client.post(
        "/api/dataset/drafts/draft-1/samples/foreign-sample/secondary",
        headers={"Authorization": f"Bearer {generate_token(8)}"},
        data={"file": (io.BytesIO(b"image bytes"), "repair.jpg")},
        content_type="multipart/form-data",
    )
    body = json.loads(response.data)
    assert body["code"] == 400
    assert "不属于" in body["msg"]
    assert storage.deleted == [("dataset_images", "repair.jpg")]


def test_finalize_draft_route_returns_formal_dataset(monkeypatch, tmp_path):
    import routes.dataset as dataset_route

    result = {"dataset_id": "draft-1", "name": "ready", "sample_count": 4}
    monkeypatch.setattr(dataset_route, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(dataset_route, "finalize_draft", lambda *_args: SimpleNamespace(to_dict=lambda: result))
    monkeypatch.setattr(dataset_route, "db", object())
    monkeypatch.setattr(dataset_route, "file_client", object())
    client = _draft_client(monkeypatch, tmp_path / "guard.sqlite3")
    response = client.post(
        "/api/dataset/drafts/draft-1/finalize",
        headers={"Authorization": f"Bearer {generate_token(8)}"},
    )
    body = json.loads(response.data)
    assert body["code"] == 200
    assert body["data"]["dataset_id"] == "draft-1"


def test_retry_rejects_non_failed_draft(monkeypatch, tmp_path):
    import routes.dataset as dataset_route
    monkeypatch.setattr(dataset_route, "get_draft", lambda *_args: {"id": "draft-1", "workflow_status": "draft"})
    monkeypatch.setattr(dataset_route, "db", object())
    client = _draft_client(monkeypatch, tmp_path / "guard.sqlite3")
    response = client.post(
        "/api/dataset/drafts/draft-1/retry",
        headers={"Authorization": f"Bearer {generate_token(8)}"},
    )
    body = json.loads(response.data)
    assert body["code"] == 400
    assert "失败" in body["msg"]

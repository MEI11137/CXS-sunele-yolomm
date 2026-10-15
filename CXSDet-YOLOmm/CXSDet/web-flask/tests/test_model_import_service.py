from io import BytesIO
from types import SimpleNamespace

import pytest
from werkzeug.datastructures import FileStorage


_DEFAULT_DATASET = object()


class FakeDatabase:
    def __init__(self, dataset=_DEFAULT_DATASET, duplicate=False):
        self.dataset = {
            "id": "dataset-mm",
            "dataset_mode": "multimodal",
            "primary_modality": "rgb",
            "secondary_modality": "edge",
            "status": 1,
        } if dataset is _DEFAULT_DATASET else dataset
        self.duplicate = duplicate
        self.executed = []

    def query(self, sql, params=(), fetchone=False):
        if "FROM models WHERE name" in sql:
            return {"id": "existing"} if self.duplicate else None
        if "FROM datasets" in sql:
            return self.dataset
        return None

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        return 1


class FakeStorage:
    def __init__(self, failure=None):
        self.failure = failure
        self.uploaded = []
        self.deleted = []

    def upload(self, bucket, path):
        if self.failure:
            raise RuntimeError(self.failure)
        self.uploaded.append((bucket, path))
        return {"bucket": bucket, "objectKey": "imported/best.pt"}

    def delete(self, bucket, object_key):
        self.deleted.append((bucket, object_key))


def _upload(filename="best.pt", content=b"checkpoint"):
    return FileStorage(stream=BytesIO(content), filename=filename)


def _multimodal_detector():
    return SimpleNamespace(model=SimpleNamespace(mm_router=object()))


def test_imported_weight_is_validated_stored_and_registered_with_dataset_modality(
    tmp_path, monkeypatch
):
    from services import model_import_service as service

    monkeypatch.setattr(service, "TEMP_DIR", str(tmp_path))
    database = FakeDatabase()
    storage = FakeStorage()
    loader_calls = []

    result = service.import_model_weight(
        "edge detector",
        "trained elsewhere",
        "dataset-mm",
        _upload(),
        database,
        storage,
        lambda path, mode, yaml: loader_calls.append((path, mode, yaml))
        or _multimodal_detector(),
    )

    assert result.name == "edge detector"
    assert result.model_mode == "multimodal"
    assert result.secondary_modality == "edge"
    assert result.status == 1
    assert result.publish_status == "draft"
    assert len(loader_calls) == 1
    assert loader_calls[0][1] == "multimodal"
    assert loader_calls[0][2].endswith("yolo11n-mm-early.yaml")
    assert storage.uploaded[0][0] == "models"
    assert len(database.executed) == 1
    sql, params = database.executed[0]
    assert "INSERT INTO models" in sql
    assert "dataset-mm" in params
    assert "best.pt" in params
    assert "multimodal" in params
    assert "edge" in params
    assert not list(tmp_path.glob("model-import-*"))


def test_import_rejects_non_pt_before_validation_or_storage(tmp_path, monkeypatch):
    from services import model_import_service as service

    monkeypatch.setattr(service, "TEMP_DIR", str(tmp_path))
    database = FakeDatabase()
    storage = FakeStorage()

    with pytest.raises(service.ModelImportError, match="只支持 .pt"):
        service.import_model_weight(
            "bad",
            "",
            "dataset-mm",
            _upload("model.onnx"),
            database,
            storage,
            lambda *_: object(),
        )

    assert storage.uploaded == []
    assert database.executed == []


def test_import_rejects_unknown_dataset_without_storage(tmp_path, monkeypatch):
    from services import model_import_service as service

    monkeypatch.setattr(service, "TEMP_DIR", str(tmp_path))
    database = FakeDatabase(dataset={})
    storage = FakeStorage()

    with pytest.raises(service.ModelImportError, match="数据集不存在"):
        service.import_model_weight(
            "orphan",
            "",
            "missing-dataset",
            _upload(),
            database,
            storage,
            lambda *_: object(),
        )

    assert storage.uploaded == []
    assert database.executed == []


def test_import_rejects_unapproved_dataset_without_loading_or_storage(
    tmp_path, monkeypatch
):
    from services import model_import_service as service

    monkeypatch.setattr(service, "TEMP_DIR", str(tmp_path))
    dataset = {
        "id": "dataset-pending",
        "dataset_mode": "single",
        "primary_modality": "rgb",
        "secondary_modality": None,
        "status": 0,
    }
    database = FakeDatabase(dataset=dataset)
    storage = FakeStorage()
    loader_calls = []

    with pytest.raises(service.ModelImportError, match="数据集尚未验证通过"):
        service.import_model_weight(
            "pending model",
            "",
            "dataset-pending",
            _upload(),
            database,
            storage,
            lambda *_: loader_calls.append(True) or object(),
        )

    assert loader_calls == []
    assert storage.uploaded == []
    assert database.executed == []


def test_import_rejects_runtime_incompatible_weight_without_storage(tmp_path, monkeypatch):
    from services import model_import_service as service

    monkeypatch.setattr(service, "TEMP_DIR", str(tmp_path))
    database = FakeDatabase()
    storage = FakeStorage()

    def reject(*_):
        raise RuntimeError("unsupported checkpoint")

    with pytest.raises(service.ModelImportError, match="权重加载验证失败"):
        service.import_model_weight(
            "broken",
            "",
            "dataset-mm",
            _upload(),
            database,
            storage,
            reject,
        )

    assert storage.uploaded == []
    assert database.executed == []
    assert not list(tmp_path.glob("model-import-*"))


def test_import_rejects_rgb_only_detector_for_multimodal_dataset(tmp_path, monkeypatch):
    from services import model_import_service as service

    monkeypatch.setattr(service, "TEMP_DIR", str(tmp_path))
    database = FakeDatabase()
    storage = FakeStorage()
    rgb_only = SimpleNamespace(
        model=SimpleNamespace(mm_router=None, multimodal_router=None)
    )

    with pytest.raises(service.ModelImportError, match=r"权重与所选 RGB\+X 数据集不兼容"):
        service.import_model_weight(
            "wrong modality",
            "",
            "dataset-mm",
            _upload(),
            database,
            storage,
            lambda *_: rgb_only,
        )

    assert storage.uploaded == []
    assert database.executed == []


def test_import_storage_failure_does_not_create_database_row(tmp_path, monkeypatch):
    from services import model_import_service as service

    monkeypatch.setattr(service, "TEMP_DIR", str(tmp_path))
    database = FakeDatabase()
    storage = FakeStorage("file service unavailable")

    with pytest.raises(service.ModelImportError, match="权重保存失败"):
        service.import_model_weight(
            "store fail",
            "",
            "dataset-mm",
            _upload(),
            database,
            storage,
            lambda *_: _multimodal_detector(),
        )

    assert database.executed == []

import io
import json
from types import SimpleNamespace

from flask import Flask

from utils.jwt_util import generate_token


def _post(monkeypatch, data, *, admin=True, models=None, preserve_name_check=False):
    import routes.video_detection as route

    class FakeDb:
        def query(self, sql, params=(), fetchone=False):
            if "WHERE id = ? AND status = 3" in sql:
                model_id = params[0]
                return (models or {}).get(model_id)
            return None

    monkeypatch.setattr(route, "db", FakeDb())
    monkeypatch.setattr(route, "is_admin", lambda: admin)
    if not preserve_name_check:
        monkeypatch.setattr(route, "ensure_dataset_name_available", lambda *args, **kwargs: None)
    app = Flask(__name__)
    app.register_blueprint(route.video_detection_bp)
    response = app.test_client().post(
        "/api/video-detection/detect",
        headers={"Authorization": f"Bearer {generate_token(3)}"},
        data=data,
        content_type="multipart/form-data",
    )
    return json.loads(response.data)


def _video():
    return (io.BytesIO(b"mock video"), "clip.mp4")


def _base_form(**extra):
    return {
        "generate_dataset": "true",
        "dataset_name": "new draft",
        "dataset_mode": "single",
        "frame_interval": "2",
        "model_id": "detect-model",
        "title": "test video",
        "video": _video(),
        **extra,
    }


def test_non_admin_cannot_enable_video_dataset_generation(monkeypatch):
    result = _post(monkeypatch, _base_form(), admin=False)
    assert result["code"] == 403
    assert "管理员" in result["msg"]


def test_single_mode_rejects_a_second_video(monkeypatch):
    model = {"id": "detect-model", "name": "detector", "model_mode": "single"}
    result = _post(monkeypatch, _base_form(secondary_video=_video()), models={"detect-model": model})
    assert result["code"] == 400, result
    assert "单模态" in result["msg"]


def test_multimodal_mode_requires_second_video(monkeypatch):
    model = {
        "id": "detect-model", "name": "detector", "model_mode": "multimodal",
        "secondary_modality": "thermal",
    }
    form = _base_form(dataset_mode="multimodal", secondary_modality="thermal")
    result = _post(monkeypatch, form, models={"detect-model": model})
    assert result["code"] == 400, result
    assert "第二路视频" in result["msg"]


def test_annotation_model_must_match_selected_mode_and_modality(monkeypatch):
    detect_model = {"id": "detect-model", "name": "detector", "model_mode": "single"}
    annotation_model = {
        "id": "annotator", "name": "single annotator", "model_mode": "single",
        "secondary_modality": None,
    }
    form = _base_form(
        dataset_mode="multimodal", secondary_modality="thermal",
        annotation_model_id="annotator", secondary_video=_video(),
    )
    result = _post(
        monkeypatch, form,
        models={"detect-model": detect_model, "annotator": annotation_model},
    )
    assert result["code"] == 400, result
    assert "不兼容" in result["msg"]


def test_dataset_name_conflict_is_reported_before_upload(monkeypatch):
    import routes.video_detection as route
    from services.dataset_draft_service import DraftValidationError

    monkeypatch.setattr(
        route,
        "ensure_dataset_name_available",
        lambda *args, **kwargs: (_ for _ in ()).throw(DraftValidationError("数据集名称已存在")),
    )
    model = {"id": "detect-model", "name": "detector", "model_mode": "single"}
    result = _post(
        monkeypatch, _base_form(), models={"detect-model": model}, preserve_name_check=True
    )
    assert result["code"] == 400
    assert "名称已存在" in result["msg"]


def test_compatible_detection_model_is_default_annotation_model(monkeypatch, tmp_path):
    import routes.video_detection as route

    model = {
        "id": "detect-model", "name": "detector", "model_mode": "single",
        "model_bucket": "models", "model_object_key": "detector.pt",
        "primary_modality": "rgb", "secondary_modality": None, "model_yaml": None,
    }
    captured = {}

    class FakeDb:
        def query(self, _sql, params=(), fetchone=False):
            return model if params and params[0] == "detect-model" else None

        def execute(self, _sql, _params=()):
            return 1

    class FakeCapture:
        def __init__(self, _path):
            pass

        def isOpened(self):
            return True

        def release(self):
            pass

    class FakeProcess:
        def __init__(self, target, args):
            captured["args"] = args

        def start(self):
            pass

    monkeypatch.setattr(route, "db", FakeDb())
    monkeypatch.setattr(route, "is_admin", lambda: True)
    monkeypatch.setattr(route, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(route, "ensure_dataset_name_available", lambda *args, **kwargs: None)
    monkeypatch.setattr(route, "create_draft", lambda *_args, **_kwargs: SimpleNamespace(id="draft-1"))
    monkeypatch.setattr(route.file_client, "upload", lambda *_args, **_kwargs: {"bucket": "videos", "objectKey": "original.mp4"})
    monkeypatch.setattr(route.multiprocessing, "Process", FakeProcess)
    monkeypatch.setattr("cv2.VideoCapture", FakeCapture)

    app = Flask(__name__)
    app.register_blueprint(route.video_detection_bp)
    response = app.test_client().post(
        "/api/video-detection/detect",
        headers={"Authorization": f"Bearer {generate_token(3)}"},
        data=_base_form(),
        content_type="multipart/form-data",
    )
    body = json.loads(response.data)
    assert body["code"] == 200, body
    assert body["data"]["dataset_draft_id"] == "draft-1"
    assert captured["args"][7]["config"]["annotation_model_id"] == "detect-model"
    assert captured["args"][8]["id"] == "detect-model"

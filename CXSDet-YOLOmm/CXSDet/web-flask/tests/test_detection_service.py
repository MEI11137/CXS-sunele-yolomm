from pathlib import Path

import numpy as np
import pytest
from PIL import Image


def _image(path: Path):
    pixels = np.full((40, 40, 3), 180, dtype=np.uint8)
    pixels[10:30, 10:30] = 20
    Image.fromarray(pixels).save(path)


class MemoryStorage:
    def __init__(self):
        self.uploads = []
        self.deleted = []

    def upload(self, bucket, file_path):
        item = {"bucket": bucket, "objectKey": f"{bucket}-{len(self.uploads)}.jpg"}
        self.uploads.append(item)
        return item

    def get(self, bucket, object_key):
        return b"model"

    def delete(self, bucket, object_key):
        self.deleted.append((bucket, object_key))

    def get_file_url(self, bucket, object_key):
        return f"http://files/{bucket}/{object_key}"


class CapturingDb:
    def __init__(self, fail=False):
        self.fail = fail
        self.sql = None
        self.params = None

    def execute(self, sql, params=()):
        if self.fail:
            raise RuntimeError("database unavailable")
        self.sql = sql
        self.params = params
        return 1


def _model(mode="single", secondary=None):
    return {
        "id": "model-1",
        "name": "model",
        "model_bucket": "models",
        "model_object_key": "model.pt",
        "model_mode": mode,
        "primary_modality": "rgb",
        "secondary_modality": secondary,
        "model_yaml": None,
    }


def test_detection_uploads_outputs_and_automatically_inserts_record(tmp_path):
    from services.detection_service import execute_detection
    from services.yolo_runtime import DetectionOutput

    rgb = tmp_path / "rgb.jpg"
    _image(rgb)
    storage = MemoryStorage()
    database = CapturingDb()

    result = execute_detection(
        user_id=7,
        model_meta=_model(),
        rgb_path=str(rgb),
        x_path=None,
        confidence=0.45,
        work_dir=str(tmp_path / "work"),
        storage_client=storage,
        database=database,
        detector_loader=lambda *args, **kwargs: object(),
        predictor=lambda detector, inputs, confidence: DetectionOutput(
            [
                {
                    "bbox": [10.0, 10.0, 30.0, 30.0],
                    "confidence": 0.9,
                    "class_id": 0,
                    "class_name": "crack",
                }
            ],
            [],
        ),
    )

    assert result.record_id
    assert result.detection_count == 1
    assert [item["bucket"] for item in storage.uploads] == [
        "detection_original",
        "detection_results",
    ]
    assert "INSERT INTO detection_records" in database.sql
    assert result.record_id in database.params
    assert "completed" in database.params


def test_edge_detection_generates_and_persists_x_input(tmp_path):
    from services.detection_service import execute_detection
    from services.yolo_runtime import DetectionOutput

    rgb = tmp_path / "rgb.jpg"
    _image(rgb)
    storage = MemoryStorage()
    database = CapturingDb()

    result = execute_detection(
        user_id=7,
        model_meta=_model("multimodal", "edge"),
        rgb_path=str(rgb),
        x_path=None,
        confidence=0.5,
        work_dir=str(tmp_path / "work"),
        storage_client=storage,
        database=database,
        detector_loader=lambda *args, **kwargs: object(),
        predictor=lambda *args, **kwargs: DetectionOutput([], []),
    )

    assert result.secondary_image_origin == "generated"
    assert [item["bucket"] for item in storage.uploads] == [
        "detection_original",
        "detection_secondary",
        "detection_results",
    ]


def test_database_failure_removes_newly_uploaded_objects(tmp_path):
    from services.detection_service import execute_detection
    from services.yolo_runtime import DetectionOutput

    rgb = tmp_path / "rgb.jpg"
    _image(rgb)
    storage = MemoryStorage()

    with pytest.raises(RuntimeError, match="database unavailable"):
        execute_detection(
            user_id=7,
            model_meta=_model(),
            rgb_path=str(rgb),
            x_path=None,
            confidence=0.5,
            work_dir=str(tmp_path / "work"),
            storage_client=storage,
            database=CapturingDb(fail=True),
            detector_loader=lambda *args, **kwargs: object(),
            predictor=lambda *args, **kwargs: DetectionOutput([], []),
        )

    assert set(storage.deleted) == {
        (item["bucket"], item["objectKey"]) for item in storage.uploads
    }

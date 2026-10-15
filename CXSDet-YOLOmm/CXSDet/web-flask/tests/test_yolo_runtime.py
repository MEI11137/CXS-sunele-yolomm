from pathlib import Path
import os

import numpy as np
import pytest
from PIL import Image


def _write_rgb(path: Path) -> None:
    pixels = np.zeros((16, 16, 3), dtype=np.uint8)
    pixels[:, 8:, :] = 255
    Image.fromarray(pixels, mode="RGB").save(path)


def test_vendored_ultralytics_is_selected_for_the_backend_process():
    from services.yolo_runtime import activate_vendored_ultralytics

    vendor_dir = activate_vendored_ultralytics()
    import ultralytics

    assert Path(vendor_dir).name == "vendor"
    assert "web-flask\\vendor\\ultralytics" in str(Path(ultralytics.__file__))
    assert ultralytics.__version__ == "8.3.163"
    assert Path(os.environ["YOLO_CONFIG_DIR"]).is_dir()
    assert Path(os.environ["MPLCONFIGDIR"]).is_dir()


def test_single_model_uses_only_rgb_input(tmp_path):
    from services.yolo_runtime import resolve_inference_inputs

    rgb = tmp_path / "rgb.png"
    _write_rgb(rgb)
    resolved = resolve_inference_inputs(
        {"model_mode": "single", "primary_modality": "rgb"},
        str(rgb),
        None,
        str(tmp_path / "work"),
    )

    assert resolved.rgb_path == str(rgb)
    assert resolved.x_path is None
    assert resolved.x_origin is None


def test_edge_model_generates_secondary_image_when_missing(tmp_path):
    from services.yolo_runtime import resolve_inference_inputs

    rgb = tmp_path / "rgb.png"
    _write_rgb(rgb)
    resolved = resolve_inference_inputs(
        {
            "model_mode": "multimodal",
            "primary_modality": "rgb",
            "secondary_modality": "edge",
        },
        str(rgb),
        None,
        str(tmp_path / "work"),
    )

    assert resolved.x_origin == "generated"
    assert Path(resolved.x_path).is_file()
    generated = np.asarray(Image.open(resolved.x_path))
    assert generated.max() > generated.min()


@pytest.mark.parametrize("modality", ["ir", "depth", "thermal"])
def test_non_derivable_secondary_modality_is_never_zero_filled(tmp_path, modality):
    from services.yolo_runtime import MissingModalityError, resolve_inference_inputs

    rgb = tmp_path / "rgb.png"
    _write_rgb(rgb)
    with pytest.raises(MissingModalityError, match=modality):
        resolve_inference_inputs(
            {
                "model_mode": "multimodal",
                "primary_modality": "rgb",
                "secondary_modality": modality,
            },
            str(rgb),
            None,
            str(tmp_path / "work"),
        )


def test_uploaded_secondary_image_is_preserved(tmp_path):
    from services.yolo_runtime import resolve_inference_inputs

    rgb = tmp_path / "rgb.png"
    ir = tmp_path / "ir.png"
    _write_rgb(rgb)
    _write_rgb(ir)
    resolved = resolve_inference_inputs(
        {
            "model_mode": "multimodal",
            "primary_modality": "rgb",
            "secondary_modality": "ir",
        },
        str(rgb),
        str(ir),
        str(tmp_path / "work"),
    )

    assert resolved.x_path == str(ir)
    assert resolved.x_origin == "uploaded"


def test_load_detector_selects_standard_or_multimodal_class(monkeypatch):
    from services.yolo_runtime import activate_vendored_ultralytics, load_detector

    activate_vendored_ultralytics()
    import ultralytics

    calls = []

    class FakeYOLO:
        def __init__(self, path):
            calls.append(("single", path))

    class FakeYOLOMM:
        def __init__(self, path):
            calls.append(("multimodal", path))

    monkeypatch.setattr(ultralytics, "YOLO", FakeYOLO)
    monkeypatch.setattr(ultralytics, "YOLOMM", FakeYOLOMM)

    load_detector("single.pt", "single")
    load_detector("dual.pt", "multimodal")
    assert calls == [("single", "single.pt"), ("multimodal", "dual.pt")]


def test_predict_image_calls_dual_input_api(tmp_path):
    from services.yolo_runtime import ResolvedInputs, predict_image

    calls = []

    class FakeDetector:
        def predict(self, **kwargs):
            calls.append(kwargs)
            return []

    inputs = ResolvedInputs(
        rgb_path="rgb.png",
        x_path="edge.png",
        x_origin="uploaded",
        model_mode="multimodal",
        secondary_modality="edge",
    )
    output = predict_image(FakeDetector(), inputs, 0.42)

    assert calls == [
        {
            "rgb_source": "rgb.png",
            "x_source": "edge.png",
            "conf": 0.42,
            "save": False,
            "verbose": False,
        }
    ]
    assert output.detections == []


def test_multimodal_numpy_boxes_are_serialized_like_standard_boxes():
    import numpy as np

    from services.yolo_runtime import _serialize_results

    result = type(
        "MultiModalResult",
        (),
        {
            "names": {1: "crack"},
            "boxes": np.array([[1.0, 2.0, 30.0, 40.0, 0.75, 1.0]], dtype=np.float32),
        },
    )()
    detections = _serialize_results([result])
    assert detections == [
        {
            "class_id": 1,
            "class_name": "crack",
            "confidence": 0.75,
            "bbox": [1.0, 2.0, 30.0, 40.0],
        }
    ]

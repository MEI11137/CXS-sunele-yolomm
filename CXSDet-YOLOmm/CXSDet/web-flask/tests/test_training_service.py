from pathlib import Path


def test_single_training_loads_standard_yolo_and_passes_runtime_yaml(tmp_path):
    from services.training_service import TrainingRun, run_training

    calls = []

    class FakeDetector:
        def train(self, **kwargs):
            calls.append(kwargs)
            return type("Result", (), {"results_dict": {"metrics/mAP50(B)": 0.5}})()

    def loader(model_source, model_mode, model_yaml=None):
        assert model_source == "weights/yolo11n.pt"
        assert model_mode == "single"
        return FakeDetector()

    output_dir = tmp_path / "output"
    best = output_dir / "train" / "weights" / "best.pt"
    best.parent.mkdir(parents=True)
    best.write_bytes(b"weight")
    run = TrainingRun(
        model_mode="single",
        model_source="weights/yolo11n.pt",
        model_yaml=None,
        runtime_yaml=str(tmp_path / "data.yaml"),
        epochs=2,
        batch_size=4,
        output_dir=str(output_dir),
        secondary_modality=None,
    )

    result = run_training(run, detector_loader=loader)

    assert calls[0]["data"] == run.runtime_yaml
    assert calls[0]["epochs"] == 2
    assert calls[0]["batch"] == 4
    assert result.best_weight_path == str(best)
    assert result.metrics["metrics/mAP50(B)"] == 0.5


def test_multimodal_training_uses_vendored_baseline_and_modality_contract(tmp_path):
    from services.training_service import MULTIMODAL_BASELINE_YAML, TrainingRun, run_training

    calls = []

    class FakeDetector:
        def train(self, **kwargs):
            calls.append(kwargs)
            return type("Result", (), {"results_dict": {}})()

    def loader(model_source, model_mode, model_yaml=None):
        assert model_mode == "multimodal"
        assert model_source == str(MULTIMODAL_BASELINE_YAML)
        assert model_yaml == str(MULTIMODAL_BASELINE_YAML)
        return FakeDetector()

    output_dir = tmp_path / "output"
    best = output_dir / "train" / "weights" / "best.pt"
    best.parent.mkdir(parents=True)
    best.write_bytes(b"weight")
    run = TrainingRun(
        model_mode="multimodal",
        model_source=str(MULTIMODAL_BASELINE_YAML),
        model_yaml=str(MULTIMODAL_BASELINE_YAML),
        runtime_yaml=str(tmp_path / "data.yaml"),
        epochs=1,
        batch_size=2,
        output_dir=str(output_dir),
        secondary_modality="edge",
    )

    result = run_training(run, detector_loader=loader)

    assert calls == [
        {
            "data": run.runtime_yaml,
            "epochs": 1,
            "batch": 2,
            "imgsz": 640,
            "project": str(output_dir),
            "name": "train",
            "exist_ok": True,
            "x_modality": "edge",
        }
    ]
    assert result.best_weight_path == str(best)


def test_training_fails_if_best_weight_was_not_created(tmp_path):
    from services.training_service import TrainingError, TrainingRun, run_training

    class FakeDetector:
        def train(self, **kwargs):
            return type("Result", (), {"results_dict": {}})()

    run = TrainingRun(
        model_mode="single",
        model_source="weights/yolo11n.pt",
        model_yaml=None,
        runtime_yaml=str(tmp_path / "data.yaml"),
        epochs=1,
        batch_size=1,
        output_dir=str(tmp_path / "output"),
        secondary_modality=None,
    )

    import pytest

    with pytest.raises(TrainingError, match="best.pt"):
        run_training(run, detector_loader=lambda *args, **kwargs: FakeDetector())

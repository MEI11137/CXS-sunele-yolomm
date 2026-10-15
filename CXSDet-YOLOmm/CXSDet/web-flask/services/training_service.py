"""Training orchestration shared by initial and incremental model runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from services.yolo_runtime import load_detector


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MULTIMODAL_BASELINE_YAML = (
    BACKEND_ROOT
    / "vendor"
    / "ultralytics"
    / "cfg"
    / "models"
    / "mm"
    / "detect"
    / "yolo11"
    / "base"
    / "yolo11n-mm-early.yaml"
)


class TrainingError(RuntimeError):
    """A training run did not produce a usable model."""


@dataclass(frozen=True)
class TrainingRun:
    model_mode: str
    model_source: str
    model_yaml: str | None
    runtime_yaml: str
    epochs: int
    batch_size: int
    output_dir: str
    secondary_modality: str | None


@dataclass(frozen=True)
class TrainingResult:
    best_weight_path: str
    metrics: dict[str, Any]


def _json_safe_metrics(metrics: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in dict(metrics or {}).items():
        if hasattr(value, "item"):
            value = value.item()
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[str(key)] = value
        else:
            result[str(key)] = str(value)
    return result


def run_training(
    run: TrainingRun,
    detector_loader: Callable = load_detector,
) -> TrainingResult:
    """Execute one training run and require a concrete best.pt artifact."""
    detector = detector_loader(run.model_source, run.model_mode, run.model_yaml)
    train_args = {
        "data": run.runtime_yaml,
        "epochs": run.epochs,
        "batch": run.batch_size,
        "imgsz": 640,
        "project": run.output_dir,
        "name": "train",
        "exist_ok": True,
    }
    if run.model_mode == "multimodal":
        train_args["x_modality"] = run.secondary_modality
    result = detector.train(**train_args)

    best = Path(run.output_dir) / "train" / "weights" / "best.pt"
    if not best.is_file():
        trainer_best = getattr(getattr(detector, "trainer", None), "best", None)
        if trainer_best and Path(trainer_best).is_file():
            best = Path(trainer_best)
        else:
            raise TrainingError(f"训练结束但未生成best.pt: {best}")
    metrics = _json_safe_metrics(getattr(result, "results_dict", None))
    if not metrics:
        metrics = _json_safe_metrics(getattr(detector, "metrics", None))
    return TrainingResult(str(best), metrics)

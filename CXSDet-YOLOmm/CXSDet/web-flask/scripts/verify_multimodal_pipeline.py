"""Reproducible smoke verification for the standard YOLO and RGB+X pipeline."""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sqlite3
import sys
import time
import uuid
import zipfile
from pathlib import Path

import cv2
import numpy as np
import yaml
from PIL import Image

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.ai_review_service import _parse_review_payload
from services.training_service import MULTIMODAL_BASELINE_YAML
from services.yolo_runtime import (
    MissingModalityError,
    activate_vendored_ultralytics,
    load_detector,
    resolve_inference_inputs,
)


class _SmokeDatabase:
    def __init__(self, path: Path):
        self.path = path

    def get_connection(self):
        return sqlite3.connect(self.path)

    def execute(self, sql, params=()):
        conn = self.get_connection()
        try:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def query(self, sql, params=(), fetchone=False):
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(sql, params)
            row = cursor.fetchone() if fetchone else cursor.fetchall()
            if fetchone:
                return dict(row) if row else None
            return [dict(item) for item in row]
        finally:
            conn.close()


class _LocalStorage:
    def __init__(self, root: Path):
        self.root = root

    def upload(self, bucket, file_path):
        source = Path(file_path)
        key = f"{uuid.uuid4().hex}{source.suffix.lower()}"
        target = self.root / bucket / key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return {"bucket": bucket, "objectKey": key, "url": target.as_uri()}

    def get(self, bucket, object_key):
        return (self.root / bucket / object_key).read_bytes()

    def delete(self, bucket, object_key):
        (self.root / bucket / object_key).unlink(missing_ok=True)

    def get_file_url(self, bucket, object_key):
        return (self.root / bucket / object_key).as_uri()


def _create_loop_database(path: Path) -> _SmokeDatabase:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE datasets (
          id TEXT PRIMARY KEY, name TEXT, description TEXT, zip_bucket TEXT,
          zip_object_key TEXT, structure_info TEXT, upload_time TEXT,
          file_size INTEGER, status INTEGER, dataset_mode TEXT,
          primary_modality TEXT, secondary_modality TEXT, runtime_mapping TEXT,
          parent_dataset_id TEXT
        );
        CREATE TABLE models (
          id TEXT PRIMARY KEY, name TEXT, description TEXT, dataset_id TEXT,
          base_model TEXT, epochs INTEGER, batch_size INTEGER, status INTEGER,
          model_bucket TEXT, model_object_key TEXT, train_result TEXT,
          create_time TEXT, start_time TEXT, complete_time TEXT,
          model_mode TEXT, primary_modality TEXT, secondary_modality TEXT,
          model_yaml TEXT, parent_model_id TEXT, version INTEGER,
          metrics_json TEXT, publish_status TEXT
        );
        CREATE TABLE detection_records (
          id TEXT PRIMARY KEY, user_id INTEGER, model_id TEXT, model_name TEXT,
          original_image_bucket TEXT, original_image_object_key TEXT,
          secondary_image_bucket TEXT, secondary_image_object_key TEXT,
          secondary_image_origin TEXT, result_image_bucket TEXT,
          result_image_object_key TEXT, detection_result TEXT,
          confidence_threshold REAL, create_time TEXT, title TEXT,
          description TEXT, record_status TEXT, request_key TEXT,
          ai_review_status TEXT DEFAULT 'not_requested', ai_review_result TEXT,
          human_review_status TEXT DEFAULT 'pending', corrected_annotations TEXT,
          reviewed_by INTEGER, reviewed_at TEXT, feedback_dataset_id TEXT,
          review_version INTEGER DEFAULT 0
        );
        CREATE TABLE detection_record_reviews (
          id TEXT PRIMARY KEY, record_id TEXT, reviewer_id INTEGER,
          decision TEXT, annotations TEXT, previous_version INTEGER,
          new_version INTEGER, create_time TEXT
        );
        CREATE TABLE feedback_samples (
          id TEXT PRIMARY KEY, record_id TEXT, dataset_id TEXT,
          annotation_hash TEXT, export_status TEXT, create_time TEXT
        );
        CREATE UNIQUE INDEX uq_feedback_record_annotation
          ON feedback_samples(record_id, annotation_hash);
        CREATE TABLE model_publish_audit (
          id TEXT PRIMARY KEY, model_id TEXT, operator_id INTEGER,
          previous_model_id TEXT, create_time TEXT
        );
        INSERT INTO datasets VALUES (
          'feedback', 'smoke-feedback', '', 'none', 'none.zip', '{}',
          '2026-10-14', 0, 1, 'multimodal', 'rgb', 'edge', '{}', NULL
        );
        """
    )
    conn.commit()
    conn.close()
    return _SmokeDatabase(path)


def run_learning_loop_smoke(
    artifacts: dict[str, Path], multimodal_weight: Path, output_dir: Path
) -> dict:
    from services.ai_review_service import review_detection
    from services.detection_service import execute_detection
    from services.feedback_dataset_service import (
        add_confirmed_record,
        create_feedback_dataset_version,
    )
    from services.model_registry_service import compare_models, start_incremental_training
    from services.review_service import submit_human_review

    output_dir.mkdir(parents=True, exist_ok=True)
    db = _create_loop_database(output_dir / "loop.db")
    storage = _LocalStorage(output_dir / "storage")
    stored_weight = storage.upload("models", str(multimodal_weight))
    conn = db.get_connection()
    conn.execute(
        """
        INSERT INTO models VALUES (
          'parent', 'smoke-parent', '', 'feedback', 'yolo11n.pt', 1, 1, 3,
          ?, ?, NULL, '2026-10-14', NULL, NULL, 'multimodal', 'rgb', 'edge',
          ?, NULL, 1, '{"metrics/mAP50(B)":0.0}', 'published'
        )
        """,
        (
            stored_weight["bucket"],
            stored_weight["objectKey"],
            str(MULTIMODAL_BASELINE_YAML),
        ),
    )
    conn.commit()
    conn.close()
    detected = execute_detection(
        user_id=1,
        model_meta={
            "id": "parent",
            "name": "smoke-parent",
            "model_bucket": stored_weight["bucket"],
            "model_object_key": stored_weight["objectKey"],
            "model_mode": "multimodal",
            "primary_modality": "rgb",
            "secondary_modality": "edge",
            "model_yaml": str(MULTIMODAL_BASELINE_YAML),
        },
        rgb_path=str(artifacts["rgb_image"]),
        x_path=None,
        confidence=0.25,
        work_dir=str(output_dir / "detect"),
        storage_client=storage,
        database=db,
    )

    class FakeAI:
        def review_detection_structured(self, image_bytes, detections):
            return json.dumps(
                {
                    "status": "consistent",
                    "summary": "smoke review",
                    "possible_false_positives": [],
                    "possible_missed_detections": [],
                    "attention_regions": [],
                }
            )

    ai = review_detection(detected.record_id, FakeAI(), db, storage)
    human = submit_human_review(detected.record_id, 1, 0, [], "no_defect", db)
    sample = add_confirmed_record(detected.record_id, "feedback", db)
    version = create_feedback_dataset_version(
        "feedback", output_dir / "feedback-version", db, storage
    )
    child = start_incremental_training(
        "parent", version.dataset_id, {"epochs": 1, "batch_size": 1}, db
    )
    comparison = compare_models("parent", child.model_id, db)
    record = db.query(
        "SELECT record_status, ai_review_status, human_review_status FROM detection_records WHERE id=?",
        (detected.record_id,),
        fetchone=True,
    )
    return {
        "record_id_created": bool(detected.record_id),
        "secondary_origin": detected.secondary_image_origin,
        "record_status": record["record_status"],
        "ai_review_status": ai.status,
        "human_review_status": human.status,
        "feedback_sample_created": sample.created,
        "feedback_dataset_version": bool(version.dataset_id),
        "child_model_created": bool(child.model_id),
        "comparison_unknown_preserved": comparison.metrics["precision"]["status"] == "unknown",
    }


def _sample_image(path: Path, shift: int = 0) -> None:
    canvas = np.full((96, 96, 3), 205, dtype=np.uint8)
    cv2.rectangle(canvas, (20 + shift, 24), (72 + shift, 70), (55, 55, 55), -1)
    cv2.line(canvas, (25 + shift, 28), (65 + shift, 65), (245, 245, 245), 3)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), canvas):
        raise RuntimeError(f"cannot write smoke image: {path}")


def _edge(rgb: Path, output: Path) -> None:
    image = cv2.imread(str(rgb), cv2.IMREAD_GRAYSCALE)
    edges = cv2.Canny(image, 60, 140)
    output.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output), cv2.cvtColor(edges, cv2.COLOR_GRAY2BGR))


def _archive(root: Path, output: Path) -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(root).as_posix())


def build_tiny_datasets(work_dir) -> dict[str, Path]:
    """Create non-sensitive, deterministic single and RGB+edge datasets."""
    work_dir = Path(work_dir).resolve()
    if work_dir.exists():
        shutil.rmtree(work_dir)
    single = work_dir / "single"
    multi = work_dir / "multimodal"
    for root, multimodal in ((single, False), (multi, True)):
        for split in ("train", "val"):
            for index in range(2):
                name = f"sample_{split}_{index}.jpg"
                rgb = root / "images" / split / name
                _sample_image(rgb, index * 2)
                label = root / "labels" / split / f"sample_{split}_{index}.txt"
                label.parent.mkdir(parents=True, exist_ok=True)
                label.write_text("1 0.479167 0.489583 0.541667 0.479167\n", encoding="utf-8")
                if multimodal:
                    _edge(rgb, root / "images_edge" / split / name)
        data = {
            "path": str(root),
            "train": "./images/train",
            "val": "./images/val",
            "names": {0: "bird_shit", 1: "crack", 2: "dirt", 3: "panel"},
        }
        if multimodal:
            data.update(
                {
                    "modalities": {
                        "rgb": str(root / "images"),
                        "edge": str(root / "images_edge"),
                    },
                    "modality_used": ["rgb", "edge"],
                }
            )
        (root / "data.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    single_zip = work_dir / "single.zip"
    multimodal_zip = work_dir / "multimodal.zip"
    _archive(single, single_zip)
    _archive(multi, multimodal_zip)
    return {
        "single_root": single,
        "multimodal_root": multi,
        "single_yaml": single / "data.yaml",
        "multimodal_yaml": multi / "data.yaml",
        "single_zip": single_zip,
        "multimodal_zip": multimodal_zip,
        "rgb_image": multi / "images/train/sample_train_0.jpg",
        "edge_image": multi / "images_edge/train/sample_train_0.jpg",
    }


def run_inference_smoke(artifacts: dict[str, Path], output_dir: Path) -> dict:
    activate_vendored_ultralytics()
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {}
    started = time.perf_counter()
    single = load_detector(str(BACKEND_ROOT / "weights/yolo11n.pt"), "single")
    prediction = single.predict(
        source=str(artifacts["rgb_image"]), imgsz=96, device="cpu", verbose=False
    )
    result["single"] = {
        "ok": bool(prediction),
        "seconds": round(time.perf_counter() - started, 3),
    }
    started = time.perf_counter()
    dual = load_detector(
        str(MULTIMODAL_BASELINE_YAML), "multimodal", str(MULTIMODAL_BASELINE_YAML)
    )
    dual_prediction = dual.predict(
        rgb_source=str(artifacts["rgb_image"]),
        x_source=str(artifacts["edge_image"]),
        imgsz=96,
        device="cpu",
        verbose=False,
    )
    result["multimodal_uploaded_edge"] = {
        "ok": bool(dual_prediction),
        "seconds": round(time.perf_counter() - started, 3),
    }
    return result


def run_trained_inference_smoke(
    artifacts: dict[str, Path], single_weight: Path, multimodal_weight: Path, output_dir: Path
) -> dict:
    activate_vendored_ultralytics()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not single_weight.is_file() or not multimodal_weight.is_file():
        raise FileNotFoundError("trained smoke weights are missing")
    single = load_detector(str(single_weight), "single")
    started = time.perf_counter()
    single_result = single.predict(
        source=str(artifacts["rgb_image"]), imgsz=96, device="cpu", verbose=False
    )
    dual = load_detector(str(multimodal_weight), "multimodal")
    generated = resolve_inference_inputs(
        {"model_mode": "multimodal", "secondary_modality": "edge"},
        str(artifacts["rgb_image"]),
        None,
        str(output_dir),
    )
    uploaded_result = dual.predict(
        rgb_source=str(artifacts["rgb_image"]),
        x_source=str(artifacts["edge_image"]),
        imgsz=96,
        device="cpu",
        verbose=False,
    )
    generated_result = dual.predict(
        rgb_source=generated.rgb_path,
        x_source=generated.x_path,
        imgsz=96,
        device="cpu",
        verbose=False,
    )
    ir_rejected = False
    try:
        resolve_inference_inputs(
            {"model_mode": "multimodal", "secondary_modality": "ir"},
            str(artifacts["rgb_image"]),
            None,
            str(output_dir),
        )
    except MissingModalityError:
        ir_rejected = True
    return {
        "single": bool(single_result),
        "multimodal_uploaded_edge": bool(uploaded_result),
        "multimodal_generated_edge": bool(generated_result),
        "generated_edge_origin": generated.x_origin,
        "ir_without_x_rejected": ir_rejected,
        "seconds": round(time.perf_counter() - started, 3),
    }


def run_training_smoke(
    artifacts: dict[str, Path], output_dir: Path, target: str = "both"
) -> dict:
    activate_vendored_ultralytics()
    result = {}
    if target in {"both", "single"}:
        single = load_detector(str(BACKEND_ROOT / "weights/yolo11n.pt"), "single")
        started = time.perf_counter()
        single.train(
            data=str(artifacts["single_yaml"]),
            epochs=1,
            batch=1,
            imgsz=96,
            workers=0,
            device="cpu",
            cache=False,
            plots=False,
            project=str(output_dir),
            name="single",
            exist_ok=True,
        )
        single_best = output_dir / "single/weights/best.pt"
        result["single"] = {
            "ok": single_best.is_file(),
            "seconds": round(time.perf_counter() - started, 3),
            "best": str(single_best),
        }

    if target == "single":
        return result
    dual = load_detector(
        str(MULTIMODAL_BASELINE_YAML), "multimodal", str(MULTIMODAL_BASELINE_YAML)
    )
    started = time.perf_counter()
    dual.train(
        data=str(artifacts["multimodal_yaml"]),
        epochs=1,
        batch=1,
        imgsz=96,
        workers=0,
        device="cpu",
        cache=False,
        plots=False,
        project=str(output_dir),
        name="multimodal",
        exist_ok=True,
    )
    dual_best = output_dir / "multimodal/weights/best.pt"
    result["multimodal"] = {
        "ok": dual_best.is_file(),
        "seconds": round(time.perf_counter() - started, 3),
        "best": str(dual_best),
    }
    return result


def run_api_smoke(rgb_path: Path) -> dict:
    from algo.llm.llm_vl_client import LLMVLClient

    started = time.perf_counter()
    client = LLMVLClient()
    raw = client.review_detection_structured(rgb_path.read_bytes(), [])
    parsed = _parse_review_payload(raw)
    return {
        "ok": parsed.status in {
            "consistent",
            "possible_false_positive",
            "possible_missed_detection",
            "needs_human_review",
        },
        "seconds": round(time.perf_counter() - started, 3),
        "status": parsed.status,
        "model": client.config.get("model"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=(
            "contract",
            "inference",
            "trained-inference",
            "loop",
            "train",
            "train-mm",
            "api",
            "all",
        ),
        default="contract",
    )
    parser.add_argument("--work-dir", default=str(BACKEND_ROOT / ".verification"))
    parser.add_argument("--single-weight")
    parser.add_argument("--multimodal-weight")
    args = parser.parse_args()
    work_dir = Path(args.work_dir).resolve()
    artifacts = build_tiny_datasets(work_dir / "data")
    report: dict[str, object] = {
        "python": sys.version.split()[0],
        "mode": args.mode,
        "work_dir": str(work_dir),
        "dataset_contract": {"single": True, "multimodal": True},
    }
    if args.mode in {"inference", "all"}:
        report["inference"] = run_inference_smoke(artifacts, work_dir / "inference")
    if args.mode == "trained-inference":
        if not args.single_weight or not args.multimodal_weight:
            parser.error("trained-inference requires --single-weight and --multimodal-weight")
        report["trained_inference"] = run_trained_inference_smoke(
            artifacts,
            Path(args.single_weight).resolve(),
            Path(args.multimodal_weight).resolve(),
            work_dir / "trained-inference",
        )
    if args.mode == "loop":
        if not args.multimodal_weight:
            parser.error("loop requires --multimodal-weight")
        report["learning_loop"] = run_learning_loop_smoke(
            artifacts,
            Path(args.multimodal_weight).resolve(),
            work_dir / "learning-loop",
        )
    if args.mode in {"train", "train-mm", "all"}:
        report["training"] = run_training_smoke(
            artifacts,
            work_dir / "training",
            target="multimodal" if args.mode == "train-mm" else "both",
        )
    if args.mode in {"api", "all"}:
        report["api"] = run_api_smoke(artifacts["rgb_image"])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Single shared implementation of one training run.

The page routes and the business tool layer both start training through this
module so that:

* one machine runs at most one training at a time (no queue: a second request is
  refused while a lock is held),
* the worker's state is written to the ``models`` row it was created for, which
  makes a restart recoverable by reading the row instead of re-submitting a run,
* the weight, metrics and failure text land in the same columns either way.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from config import TEMP_DIR, DB_PATH
from services.dataset_contract import (
    build_runtime_yaml,
    safe_extract_dataset,
    validate_dataset_archive,
)
from services.training_service import MULTIMODAL_BASELINE_YAML, TrainingRun, run_training
from utils.models import MODEL_STATUS


class TrainingBusy(RuntimeError):
    """Another training run currently owns the machine."""

    code = "TRAINING_BUSY"

    def __init__(self, holder: dict | None = None) -> None:
        self.holder = holder or {}
        super().__init__("本机正在训练其他模型，请等待该训练结束后再发起")


def _connect(database_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(database_path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS training_lock (
            lock_id INTEGER PRIMARY KEY CHECK (lock_id = 1),
            owner TEXT NOT NULL,
            holder_pid INTEGER,
            acquired_at TEXT NOT NULL
        )"""
    )


def _process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    except Exception:  # pragma: no cover - platform specific
        return True
    return True


def read_lock(database_path: str | Path = DB_PATH) -> dict | None:
    with _connect(database_path) as conn:
        _ensure_table(conn)
        row = conn.execute("SELECT * FROM training_lock WHERE lock_id=1").fetchone()
    return dict(row) if row else None


def acquire_training_lock(
    database_path: str | Path, owner: str, holder_pid: int | None = None,
) -> None:
    """Take the machine-wide training slot or refuse with the current holder."""
    with _connect(database_path) as conn:
        _ensure_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM training_lock WHERE lock_id=1").fetchone()
        if row is not None and _process_alive(row["holder_pid"]):
            raise TrainingBusy(dict(row))
        conn.execute(
            "INSERT INTO training_lock (lock_id, owner, holder_pid, acquired_at) "
            "VALUES (1, ?, ?, ?) ON CONFLICT(lock_id) DO UPDATE SET "
            "owner=excluded.owner, holder_pid=excluded.holder_pid, "
            "acquired_at=excluded.acquired_at",
            (str(owner), holder_pid, datetime.now().isoformat()),
        )


def set_lock_holder(database_path: str | Path, owner: str, pid: int) -> None:
    """Point the lock at the worker process so a dead worker can be reclaimed."""
    with _connect(database_path) as conn:
        _ensure_table(conn)
        conn.execute(
            "UPDATE training_lock SET holder_pid=? WHERE lock_id=1 AND owner=?",
            (int(pid), str(owner)),
        )


def transfer_training_lock(database_path: str | Path, from_owner: str, to_owner: str) -> None:
    """Hand the slot from a reservation to the model row the worker will release."""
    with _connect(database_path) as conn:
        _ensure_table(conn)
        conn.execute(
            "UPDATE training_lock SET owner=? WHERE lock_id=1 AND owner=?",
            (str(to_owner), str(from_owner)),
        )


def release_training_lock(database_path: str | Path, owner: str) -> None:
    """Release only if this owner still holds the slot."""
    with _connect(database_path) as conn:
        _ensure_table(conn)
        conn.execute("DELETE FROM training_lock WHERE lock_id=1 AND owner=?", (str(owner),))


def run_training_job(
    model_id: str,
    dataset_id: str,
    base_model: str,
    epochs: int,
    batch_size: int,
    parent_model_id: str | None = None,
    database_path: str | Path = DB_PATH,
) -> dict:
    """Execute one run for an already registered model row.

    Runs in a worker process, so it opens its own database connection and its
    own storage client. Every outcome is persisted; nothing is retried.
    """
    from clients.file_client import FileClient
    from utils.db import Database

    database = Database()
    storage = FileClient()
    temp_dir = os.path.join(TEMP_DIR, f"model_training_{model_id}_{uuid.uuid4().hex[:8]}")
    outcome: dict = {"model_id": model_id, "status": "failed"}
    try:
        database.execute(
            "UPDATE models SET status=?, start_time=? WHERE id=?",
            (MODEL_STATUS["TRAINING"], datetime.now().isoformat(), model_id),
        )
        dataset = database.query(
            "SELECT zip_bucket, zip_object_key, dataset_mode, primary_modality, "
            "secondary_modality FROM datasets WHERE id=?",
            (dataset_id,), fetchone=True,
        )
        if not dataset:
            raise RuntimeError("数据集不存在")

        os.makedirs(temp_dir, exist_ok=True)
        dataset_path = os.path.join(temp_dir, "dataset.zip")
        with open(dataset_path, "wb") as handle:
            handle.write(storage.get(dataset["zip_bucket"], dataset["zip_object_key"]))

        model_mode = dataset.get("dataset_mode") or "single"
        validation = validate_dataset_archive(dataset_path, model_mode)
        extracted_root = safe_extract_dataset(dataset_path, os.path.join(temp_dir, "dataset"))
        runtime_yaml = build_runtime_yaml(
            extracted_root, validation, os.path.join(temp_dir, "data.runtime.yaml")
        )

        if parent_model_id:
            parent = database.query(
                "SELECT model_bucket, model_object_key, model_mode, model_yaml "
                "FROM models WHERE id=?", (parent_model_id,), fetchone=True,
            )
            if not parent or not parent.get("model_object_key"):
                raise RuntimeError("父模型权重不存在")
            if (parent.get("model_mode") or "single") != model_mode:
                raise RuntimeError("父模型与反馈数据集模式不一致")
            model_source = os.path.join(temp_dir, "parent_model.pt")
            with open(model_source, "wb") as handle:
                handle.write(storage.get(parent["model_bucket"], parent["model_object_key"]))
            model_yaml = parent.get("model_yaml")
        elif model_mode == "multimodal":
            model_source = str(MULTIMODAL_BASELINE_YAML)
            model_yaml = model_source
        else:
            model_source = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "weights", base_model)
            )
            model_yaml = None
            if not os.path.exists(model_source):
                raise RuntimeError(f"基础模型文件不存在: {model_source}")

        training_result = run_training(TrainingRun(
            model_mode=model_mode,
            model_source=model_source,
            model_yaml=model_yaml,
            runtime_yaml=runtime_yaml,
            epochs=epochs,
            batch_size=batch_size,
            output_dir=os.path.join(temp_dir, "output"),
            secondary_modality=dataset.get("secondary_modality"),
        ))
        upload = storage.upload("models", training_result.best_weight_path)
        database.execute(
            "UPDATE models SET status=?, complete_time=?, model_bucket=?, "
            "model_object_key=?, train_result=?, metrics_json=? WHERE id=?",
            (
                MODEL_STATUS["COMPLETED"], datetime.now().isoformat(),
                upload["bucket"], upload["objectKey"],
                json.dumps({
                    "final_epoch": epochs,
                    "results_dict": training_result.metrics,
                    "training_metrics": [],
                }, ensure_ascii=False),
                json.dumps(training_result.metrics, ensure_ascii=False),
                model_id,
            ),
        )
        outcome["status"] = "completed"
        outcome["metrics"] = training_result.metrics
    except Exception as exc:
        try:
            database.execute(
                "UPDATE models SET status=?, complete_time=?, train_result=? WHERE id=?",
                (MODEL_STATUS["FAILED"], datetime.now().isoformat(),
                 json.dumps({"error": str(exc)}, ensure_ascii=False), model_id),
            )
        finally:
            outcome["error"] = str(exc)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        release_training_lock(database_path, model_id)
    return outcome

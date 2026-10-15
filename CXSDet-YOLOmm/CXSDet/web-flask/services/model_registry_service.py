"""Model lineage, metric comparison, and atomic manual publication."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable


class ModelRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class IncrementalModel:
    model_id: str
    parent_model_id: str
    dataset_id: str
    version: int
    model_mode: str
    epochs: int
    batch_size: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModelComparison:
    parent_model_id: str
    child_model_id: str
    metrics: dict[str, dict[str, Any]]
    per_class: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PublishResult:
    model_id: str
    previous_model_id: str | None
    publish_status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _row(conn, sql: str, params=()):
    return conn.execute(sql, params).fetchone()


def start_incremental_training(
    parent_model_id: str,
    feedback_dataset_id: str,
    overrides: dict[str, Any] | None,
    database,
) -> IncrementalModel:
    overrides = overrides or {}
    conn = database.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        parent = _row(conn, "SELECT * FROM models WHERE id = ?", (parent_model_id,))
        if parent is None:
            raise ModelRegistryError("父模型不存在")
        if parent["status"] not in {1, 3} or not parent["model_object_key"]:
            raise ModelRegistryError("父模型必须训练成功且具有可用权重")
        dataset = _row(conn, "SELECT * FROM datasets WHERE id = ?", (feedback_dataset_id,))
        if dataset is None:
            raise ModelRegistryError("反馈数据集不存在")
        if (dataset["dataset_mode"] or "single") != (parent["model_mode"] or "single"):
            raise ModelRegistryError("反馈数据集与父模型的单/多模态模式不一致")
        if (dataset["primary_modality"] or "rgb") != (parent["primary_modality"] or "rgb"):
            raise ModelRegistryError("反馈数据集主模态与父模型不一致")
        if (dataset["secondary_modality"] or None) != (parent["secondary_modality"] or None):
            raise ModelRegistryError("反馈数据集第二模态与父模型不一致")
        epochs = int(overrides.get("epochs", parent["epochs"] or 1))
        batch_size = int(overrides.get("batch_size", parent["batch_size"] or 1))
        if not 1 <= epochs <= 10000 or not 1 <= batch_size <= 1024:
            raise ModelRegistryError("训练轮数或批大小超出允许范围")
        version = int(parent["version"] or 1) + 1
        model_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO models
                (id, name, description, dataset_id, base_model, epochs, batch_size,
                 status, model_bucket, model_object_key, train_result, create_time,
                 start_time, complete_time, model_mode, primary_modality,
                 secondary_modality, model_yaml, parent_model_id, version,
                 metrics_json, publish_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, ?, NULL, NULL,
                    ?, ?, ?, ?, ?, ?, NULL, 'draft')
            """,
            (
                model_id,
                str(overrides.get("name") or f"{parent['name']} v{version}"),
                str(overrides.get("description") or f"由 {parent['name']} 增量训练"),
                feedback_dataset_id,
                parent["base_model"],
                epochs,
                batch_size,
                now,
                parent["model_mode"] or "single",
                parent["primary_modality"] or "rgb",
                parent["secondary_modality"],
                parent["model_yaml"],
                parent_model_id,
                version,
            ),
        )
        conn.commit()
        return IncrementalModel(
            model_id,
            parent_model_id,
            feedback_dataset_id,
            version,
            parent["model_mode"] or "single",
            epochs,
            batch_size,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _metrics(row: sqlite3.Row) -> dict[str, Any]:
    raw = row["metrics_json"]
    if raw:
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    try:
        train = json.loads(row["train_result"] or "{}")
    except json.JSONDecodeError:
        return {}
    return train.get("results_dict", {}) if isinstance(train, dict) else {}


def _first(metrics: dict[str, Any], *keys: str):
    for key in keys:
        value = metrics.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def compare_models(parent_model_id: str, child_model_id: str, database) -> ModelComparison:
    conn = database.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        parent = _row(conn, "SELECT * FROM models WHERE id = ?", (parent_model_id,))
        child = _row(conn, "SELECT * FROM models WHERE id = ?", (child_model_id,))
    finally:
        conn.close()
    if parent is None or child is None:
        raise ModelRegistryError("父模型或子模型不存在")
    if child["parent_model_id"] != parent_model_id:
        raise ModelRegistryError("所选模型不属于同一父子链")
    parent_metrics, child_metrics = _metrics(parent), _metrics(child)
    aliases = {
        "mAP50": ("metrics/mAP50(B)", "mAP50", "map50"),
        "mAP50-95": ("metrics/mAP50-95(B)", "mAP50-95", "map"),
        "precision": ("metrics/precision(B)", "precision"),
        "recall": ("metrics/recall(B)", "recall"),
        "inference_ms": ("inference_ms", "speed/inference"),
    }
    comparison: dict[str, dict[str, Any]] = {}
    for label, keys in aliases.items():
        before, after = _first(parent_metrics, *keys), _first(child_metrics, *keys)
        known = before is not None and after is not None
        comparison[label] = {
            "parent": before,
            "child": after,
            "delta": after - before if known else None,
            "status": "known" if known else "unknown",
        }
    return ModelComparison(
        parent_model_id,
        child_model_id,
        comparison,
        {
            "parent": parent_metrics.get("per_class"),
            "child": child_metrics.get("per_class"),
            "status": "known"
            if parent_metrics.get("per_class") is not None
            and child_metrics.get("per_class") is not None
            else "unknown",
        },
    )


def publish_model(
    model_id: str,
    operator_id: int,
    database,
    storage_exists: Callable[[str, str], bool] | None = None,
) -> PublishResult:
    conn = database.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        candidate = _row(conn, "SELECT * FROM models WHERE id = ?", (model_id,))
        if candidate is None:
            raise ModelRegistryError("模型不存在")
        if candidate["status"] != 1:
            raise ModelRegistryError("只能发布训练完成且尚未发布的模型")
        bucket, key = candidate["model_bucket"], candidate["model_object_key"]
        if not bucket or not key:
            raise ModelRegistryError("模型权重不存在")
        if storage_exists is not None and not storage_exists(bucket, key):
            raise ModelRegistryError("文件服务中找不到模型权重")
        dataset = _row(conn, "SELECT * FROM datasets WHERE id = ?", (candidate["dataset_id"],))
        if dataset is None:
            raise ModelRegistryError("模型对应的数据集不存在")
        if (dataset["dataset_mode"] or "single") != (candidate["model_mode"] or "single"):
            raise ModelRegistryError("模型模式与数据集不匹配")
        if (dataset["secondary_modality"] or None) != (candidate["secondary_modality"] or None):
            raise ModelRegistryError("模型第二模态与数据集不匹配")
        previous = _row(
            conn,
            """
            SELECT id FROM models
            WHERE status = 3 AND id != ? AND model_mode = ?
              AND primary_modality = ? AND secondary_modality IS ?
            ORDER BY create_time DESC LIMIT 1
            """,
            (
                model_id,
                candidate["model_mode"],
                candidate["primary_modality"],
                candidate["secondary_modality"],
            ),
        )
        conn.execute(
            """
            UPDATE models SET status = 1, publish_status = 'archived'
            WHERE status = 3 AND id != ? AND model_mode = ?
              AND primary_modality = ? AND secondary_modality IS ?
            """,
            (
                model_id,
                candidate["model_mode"],
                candidate["primary_modality"],
                candidate["secondary_modality"],
            ),
        )
        conn.execute(
            "UPDATE models SET status = 3, publish_status = 'published' WHERE id = ?",
            (model_id,),
        )
        conn.execute(
            """
            INSERT INTO model_publish_audit
                (id, model_id, operator_id, previous_model_id, create_time)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                model_id,
                operator_id,
                previous["id"] if previous else None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return PublishResult(
            model_id,
            previous["id"] if previous else None,
            "published",
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

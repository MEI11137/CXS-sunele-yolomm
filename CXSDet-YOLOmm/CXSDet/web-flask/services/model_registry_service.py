"""Model lineage, metric comparison, and atomic manual publication."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


class ModelRegistryError(ValueError):
    pass


class ModelPublishConflict(ModelRegistryError):
    """The published model set changed after a caller's preview."""


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
    # 本次真正被归档的模型（精确清单，便于页面与 Agent 核对）
    archived_model_ids: list[str] = field(default_factory=list)

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
    *,
    expected_archived_model_ids: list[str] | None = None,
    scope_model_ids: list[str] | None = None,
) -> PublishResult:
    """发布一个模型，并只归档 ``scope_model_ids`` 范围内的同模态旧模型。

    ``scope_model_ids`` 是**调用方按真实归属（owner + project）算出的合法范围**，
    默认只包含该模型自己——即"不确认归属就不归档任何别人的模型"。历史实现按整张
    ``models`` 表同模态筛选，会把其他用户/项目的已发布模型一起归档。
    """
    conn = database.get_connection()
    conn.row_factory = sqlite3.Row
    scope = {str(item) for item in (scope_model_ids or [model_id])}
    scope.add(model_id)
    scope_placeholders = ", ".join("?" for _ in scope)
    scope_params = tuple(sorted(scope))
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
        published = conn.execute(
            f"""
            SELECT id FROM models
            WHERE status = 3 AND id != ? AND model_mode = ?
              AND primary_modality = ? AND secondary_modality IS ?
              AND id IN ({scope_placeholders})
            ORDER BY create_time DESC
            """,
            (
                model_id,
                candidate["model_mode"],
                candidate["primary_modality"],
                candidate["secondary_modality"],
            )
            + scope_params,
        ).fetchall()
        if (expected_archived_model_ids is not None and
                sorted(row["id"] for row in published) != sorted(expected_archived_model_ids)):
            raise ModelPublishConflict("待归档模型已变化，请重新预检")
        previous = published[0] if published else None
        archived_ids = [row["id"] for row in published]
        if archived_ids:
            # 精确归档清单：只更新事务内重新核对过的那几条，绝不再按模态整表更新
            exact_placeholders = ", ".join("?" for _ in archived_ids)
            conn.execute(
                f"""
                UPDATE models SET status = 1, publish_status = 'archived'
                WHERE status = 3 AND id IN ({exact_placeholders})
                """,
                tuple(archived_ids),
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
            sorted(archived_ids),
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

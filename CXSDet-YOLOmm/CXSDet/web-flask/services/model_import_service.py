"""Validation and registration for externally trained model weights."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from config import TEMP_DIR
from services.training_service import MULTIMODAL_BASELINE_YAML
from services.yolo_runtime import load_detector


class ModelImportError(ValueError):
    """The uploaded weight cannot be safely registered."""


@dataclass(frozen=True)
class ImportedModel:
    id: str
    name: str
    description: str
    dataset_id: str
    base_model: str
    status: int
    model_mode: str
    primary_modality: str
    secondary_modality: str | None
    model_yaml: str | None
    version: int
    publish_status: str
    create_time: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _require_text(value: str | None, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ModelImportError(f"{label}不能为空")
    return normalized


def _validate_detector_contract(detector, model_mode: str) -> None:
    """Reject a weight whose loaded model cannot consume the declared inputs."""
    if model_mode != "multimodal":
        return
    loaded_model = getattr(detector, "model", None)
    owners = (detector, loaded_model)
    has_router = any(
        getattr(owner, attribute, None) is not None
        for owner in owners
        if owner is not None
        for attribute in ("mm_router", "multimodal_router")
    )
    if not has_router:
        raise ModelImportError(
            "权重与所选 RGB+X 数据集不兼容：未检测到多模态路由器"
        )


def import_model_weight(
    name: str,
    description: str,
    dataset_id: str,
    upload: FileStorage | None,
    database,
    storage_client,
    detector_loader: Callable = load_detector,
) -> ImportedModel:
    """Validate, store and register one external `.pt` weight as a draft model."""
    model_name = _require_text(name, "模型名称")
    source_dataset_id = _require_text(dataset_id, "数据集")
    if upload is None or not upload.filename:
        raise ModelImportError("请选择模型权重文件")
    original_filename = Path(upload.filename).name
    if Path(original_filename).suffix.lower() != ".pt":
        raise ModelImportError("只支持 .pt 模型权重文件")

    duplicate = database.query(
        "SELECT id FROM models WHERE name = ?", (model_name,), fetchone=True
    )
    if duplicate:
        raise ModelImportError("模型名称已存在")
    dataset = database.query(
        """
        SELECT id, dataset_mode, primary_modality, secondary_modality, status
        FROM datasets WHERE id = ?
        """,
        (source_dataset_id,),
        fetchone=True,
    )
    if not dataset:
        raise ModelImportError("选择的数据集不存在")
    if int(dataset.get("status", 0)) != 1:
        raise ModelImportError("数据集尚未验证通过，不能用于导入模型")

    model_mode = str(dataset.get("dataset_mode") or "single").lower()
    primary_modality = str(dataset.get("primary_modality") or "rgb").lower()
    secondary_modality = dataset.get("secondary_modality") or None
    model_yaml = (
        str(MULTIMODAL_BASELINE_YAML) if model_mode == "multimodal" else None
    )

    temp_root = Path(TEMP_DIR)
    temp_root.mkdir(parents=True, exist_ok=True)
    safe_name = secure_filename(original_filename) or "weight.pt"
    temp_path = temp_root / f"model-import-{uuid.uuid4().hex}-{safe_name}"
    stored: dict[str, str] | None = None
    try:
        upload.save(str(temp_path))
        if not temp_path.is_file() or temp_path.stat().st_size <= 0:
            raise ModelImportError("模型权重文件为空")
        try:
            detector = detector_loader(str(temp_path), model_mode, model_yaml)
            _validate_detector_contract(detector, model_mode)
            del detector
        except ModelImportError:
            raise
        except Exception as exc:
            raise ModelImportError(f"权重加载验证失败: {exc}") from exc

        try:
            stored = storage_client.upload("models", str(temp_path))
        except Exception as exc:
            raise ModelImportError(f"权重保存失败: {exc}") from exc

        model_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        train_result = json.dumps(
            {
                "source": "imported",
                "weight_filename": original_filename,
                "validation": "runtime_loaded",
            },
            ensure_ascii=False,
        )
        try:
            affected = database.execute(
                """
                INSERT INTO models (
                    id, name, description, dataset_id, base_model, epochs,
                    batch_size, status, model_bucket, model_object_key,
                    train_result, create_time, start_time, complete_time,
                    model_mode, primary_modality, secondary_modality,
                    model_yaml, version, publish_status
                ) VALUES (?, ?, ?, ?, ?, 0, 0, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'draft')
                """,
                (
                    model_id,
                    model_name,
                    str(description or "").strip(),
                    source_dataset_id,
                    original_filename,
                    stored["bucket"],
                    stored["objectKey"],
                    train_result,
                    now,
                    now,
                    now,
                    model_mode,
                    primary_modality,
                    secondary_modality,
                    model_yaml,
                ),
            )
            if affected <= 0:
                raise RuntimeError("模型记录未写入")
        except Exception as exc:
            try:
                storage_client.delete(stored["bucket"], stored["objectKey"])
            except Exception:
                pass
            raise ModelImportError(f"模型登记失败: {exc}") from exc

        return ImportedModel(
            id=model_id,
            name=model_name,
            description=str(description or "").strip(),
            dataset_id=source_dataset_id,
            base_model=original_filename,
            status=1,
            model_mode=model_mode,
            primary_modality=primary_modality,
            secondary_modality=secondary_modality,
            model_yaml=model_yaml,
            version=1,
            publish_status="draft",
            create_time=now,
        )
    finally:
        temp_path.unlink(missing_ok=True)

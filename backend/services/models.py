"""模型导入与发布（最小可用）。

只做三件事：把一份 PT 放进项目、**真读一遍**确认它是可加载的权重并拿到类别表、
把“权重里的类别索引”和“数据集里的类别索引”用**显式映射**对上。

不做的：训练、自动下载、跨项目共享、按类名翻译猜映射。
类名中英文不同（``single_hotspot`` vs ``单一热斑``）时，要么调用方给出映射，
要么工具返回 ``NEED_CLASS_MAPPING`` 让对话去问用户——不猜。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from backend import security
from backend.errors import ApiError
from backend.services import datasets as dataset_service
from backend.services import storage
from backend.vision import runtime as vision_runtime

FAMILIES = {"yolo": "single", "yolomm": "multimodal"}


def public(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "project_id": data["project_id"],
        "name": data["name"],
        "family": data["family"],
        "model_mode": data["model_mode"],
        "secondary_modality": data["secondary_modality"],
        "dataset_id": data["dataset_id"],
        "weight_sha256": data["weight_sha256"],
        "weight_size": int(data["weight_size"]),
        "classes": _loads(data["classes"], []),
        "class_map": _loads(data["class_map"], {}),
        "status": data["status"],
        "published_at": data["published_at"],
        "notes": data["notes"],
        "created_at": data["created_at"],
    }


def _loads(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (ValueError, TypeError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def get_owned(conn, user_id: int, project_id: str, model_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM models WHERE id = ? AND project_id = ? AND owner_id = ?",
        (str(model_id), str(project_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise ApiError(404, "MODEL_NOT_FOUND", "模型不存在或不属于当前项目")
    return row


def list_for_project(conn, user_id: int, project_id: str) -> dict:
    rows = conn.execute(
        "SELECT * FROM models WHERE project_id = ? AND owner_id = ? ORDER BY created_at DESC",
        (str(project_id), int(user_id)),
    ).fetchall()
    return {"models": [public(row) for row in rows]}


def import_model(
    conn,
    user_id: int,
    project_id: str,
    *,
    name: str,
    family: str,
    model_mode: str | None = None,
    secondary_modality: str | None = None,
    dataset_id: str | None = None,
    class_map: dict | None = None,
    asset_id: str | None = None,
    rel_path: str | None = None,
    notes: str = "",
) -> dict:
    label = str(name or "").strip()
    if not label:
        raise ApiError(400, "VALIDATION", "模型名称不能为空")
    fam = str(family or "").strip().lower()
    if fam not in FAMILIES:
        raise ApiError(400, "VALIDATION", "family 只能是 yolo 或 yolomm")
    mode = str(model_mode or FAMILIES[fam]).strip().lower()
    if mode != FAMILIES[fam]:
        raise ApiError(400, "VALIDATION", f"{fam} 只能是 {FAMILIES[fam]} 模式")
    secondary = str(secondary_modality or "").strip().lower() or None
    if mode == "multimodal" and not secondary:
        raise ApiError(400, "VALIDATION", "多模态模型必须声明第二模态（例如 ir / edge）")

    root = storage.project_root(conn, user_id, project_id)
    if asset_id:
        asset = _weight_asset(conn, user_id, project_id, asset_id)
        weight_path = storage.resolve_rel(root, asset["rel_path"], must_exist=True)
    elif rel_path:
        weight_path = storage.resolve_rel(root, rel_path, must_exist=True)
    else:
        raise ApiError(400, "SOURCE_REQUIRED", "需要提供权重资产或项目内 .pt 路径")
    if weight_path.suffix.lower() != ".pt":
        raise ApiError(409, "NOT_A_WEIGHT", "只接受 .pt 权重文件")

    info = vision_runtime.inspect_weight(weight_path)
    native = info["classes"]
    mapping: dict[str, int] = {}
    dataset_classes: list[str] = []
    if dataset_id:
        dataset = dataset_service.get_owned(conn, user_id, project_id, dataset_id)
        dataset_classes = _loads(dataset["classes"], [])
        mapping = _build_mapping(native, dataset_classes, class_map)

    row_id = security.new_id("mdl")
    try:
        conn.execute(
            """
            INSERT INTO models
                (id, project_id, owner_id, name, family, model_mode, secondary_modality, dataset_id,
                 weight_rel, weight_sha256, weight_size, classes, class_map, status, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'imported', ?, ?)
            """,
            (
                row_id,
                str(project_id),
                int(user_id),
                label,
                fam,
                mode,
                secondary,
                dataset_id,
                storage.relative(root, weight_path),
                _sha256(weight_path),
                weight_path.stat().st_size,
                json.dumps(native, ensure_ascii=False),
                json.dumps(mapping, ensure_ascii=False),
                str(notes or ""),
                security.now_iso(),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise ApiError(409, "MODEL_NAME_TAKEN", "这个项目里已经有同名模型，请换个名字") from None
    return public(get_owned(conn, user_id, project_id, row_id))


def publish(conn, user_id: int, project_id: str, model_id: str) -> dict:
    row = get_owned(conn, user_id, project_id, model_id)
    if not row["dataset_id"]:
        raise ApiError(
            409,
            "DATASET_REQUIRED",
            "发布前必须绑定数据集：否则无法确定输出的类别索引对应什么类别",
        )
    mapping = _loads(row["class_map"], {})
    native = _loads(row["classes"], [])
    if not mapping and native:
        raise ApiError(409, "CLASS_MAPPING_REQUIRED", "类别映射未完成，不能发布")
    conn.execute(
        "UPDATE models SET status = 'published', published_at = ? WHERE id = ? AND owner_id = ?",
        (security.now_iso(), str(model_id), int(user_id)),
    )
    conn.commit()
    return public(get_owned(conn, user_id, project_id, model_id))


def archive(conn, user_id: int, project_id: str, model_id: str) -> dict:
    get_owned(conn, user_id, project_id, model_id)
    conn.execute(
        "UPDATE models SET status = 'archived' WHERE id = ? AND owner_id = ?",
        (str(model_id), int(user_id)),
    )
    conn.commit()
    return public(get_owned(conn, user_id, project_id, model_id))


def published_for_project(conn, user_id: int, project_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM models WHERE project_id = ? AND owner_id = ? AND status = 'published' "
        "ORDER BY published_at DESC",
        (str(project_id), int(user_id)),
    ).fetchall()


def _weight_asset(conn, user_id: int, project_id: str, asset_id: str):
    row = conn.execute(
        "SELECT * FROM assets WHERE id = ? AND project_id = ? AND owner_id = ?",
        (str(asset_id), str(project_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise ApiError(404, "ASSET_NOT_FOUND", "权重资产不存在或不属于当前项目")
    if str(row["kind"]) != "weight":
        raise ApiError(409, "NOT_A_WEIGHT", "该资产不是 .pt 权重")
    return row


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_mapping(native: list[dict], dataset_classes: list[str], explicit: dict | None) -> dict[str, int]:
    """把权重类别索引映射到数据集类别索引；不靠翻译猜。"""
    if not dataset_classes:
        raise ApiError(409, "DATASET_NO_CLASSES", "绑定的数据集没有类别表")
    if explicit:
        mapping: dict[str, int] = {}
        for key, value in dict(explicit).items():
            try:
                source, target = int(key), int(value)
            except (TypeError, ValueError):
                raise ApiError(400, "BAD_CLASS_MAP", "类别映射必须是 {权重索引: 数据集索引}") from None
            if not 0 <= source < len(native):
                raise ApiError(400, "BAD_CLASS_MAP", f"权重索引 {source} 不存在")
            if not 0 <= target < len(dataset_classes):
                raise ApiError(400, "BAD_CLASS_MAP", f"数据集索引 {target} 不存在")
            mapping[str(source)] = target
        missing = [item["index"] for item in native if str(item["index"]) not in mapping]
        if missing:
            raise ApiError(400, "CLASS_MAP_INCOMPLETE", f"这些权重类别还没有映射：{missing}")
        return mapping

    # 没有显式映射时，只有“同名同索引且数量一致”才敢自动对上。
    dataset_index = {str(name).strip().casefold(): index for index, name in enumerate(dataset_classes)}
    auto: dict[str, int] = {}
    for item in native:
        key = str(item["name"]).strip().casefold()
        if key not in dataset_index:
            raise ApiError(
                409,
                "NEED_CLASS_MAPPING",
                "权重类别与数据集类别名称不一致，无法自动对应，请让用户确认映射",
                details={
                    "native_classes": native,
                    "dataset_classes": dataset_classes,
                    "hint": "用 class_map 提供 {权重索引: 数据集索引}",
                },
            )
        auto[str(item["index"])] = dataset_index[key]
    return auto


def class_labels(row: sqlite3.Row) -> dict[int, str]:
    """输出侧展示用的类别名：优先数据集类别（映射后），否则权重自带名称。"""
    mapping = _loads(row["class_map"], {})
    native = {int(item["index"]): str(item["name"]) for item in _loads(row["classes"], [])}
    if not mapping:
        return native
    return {int(target): native.get(int(source), str(target)) for source, target in mapping.items()}


__all__ = [
    "archive",
    "class_labels",
    "get_owned",
    "import_model",
    "list_for_project",
    "public",
    "publish",
    "published_for_project",
]

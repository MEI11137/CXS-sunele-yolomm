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
        "source": str(data.get("source") or "imported"),
        "parent_model_id": data.get("parent_model_id"),
        "training_run_id": data.get("training_run_id"),
        "metrics": _loads(data.get("metrics"), {}),
        "deleted_at": data.get("deleted_at"),
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


def list_for_project(conn, user_id: int, project_id: str, *, include_deleted: bool = False) -> dict:
    clause = "" if include_deleted else " AND deleted_at IS NULL"
    rows = conn.execute(
        "SELECT * FROM models WHERE project_id = ? AND owner_id = ?" + clause
        + " ORDER BY created_at DESC",
        (str(project_id), int(user_id)),
    ).fetchall()
    items = [public(row) for row in rows]
    # 模型绑定的数据集主模态：单 IR 模型的输入原图就是 IR，页面与 Agent 都要
    # 如实说明，不能默认写成 RGB。一次批量查询，不逐行查库。
    dataset_ids = sorted({str(item.get("dataset_id") or "") for item in items} - {""})
    primaries: dict[str, str] = {}
    if dataset_ids:
        from backend.services import datasets as dataset_service

        marks = ", ".join("?" for _ in dataset_ids)
        for row in conn.execute(
            f"SELECT id, modalities, reference_modality FROM datasets WHERE id IN ({marks})",
            tuple(dataset_ids),
        ).fetchall():
            # 参考模态以显式列为准：legacy 行 modalities=[rgb,ir] 也可能是 IR 参考。
            primaries[str(row["id"])] = dataset_service.reference_modality(row)
    for item in items:
        item["dataset_primary_modality"] = primaries.get(str(item.get("dataset_id") or ""))
    return {"models": items}


def _require_family_matches(family: str, info: dict, *, code: str = "MODEL_FAMILY_MISMATCH") -> None:
    """family 必须与权重里**真实**的多模态结构一致。

    证据来自 vendor 自己的判定（YAML 路由注解 / MultiModalRouter / metadata），
    不是文件名里的 "mm"，也不是旧版只看类名的布尔值——真实 YOLOMM 权重反序列化后
    类名仍是 DetectionModel，按类名判断会把它们误拒。
    """
    actual = bool(info.get("multimodal"))
    evidence = info.get("multimodal_evidence") or {}
    if family == "yolomm" and not actual:
        raise ApiError(
            409,
            code,
            "这个权重里没有多模态路由（YAML 路由注解 / MultiModalRouter / 模态 metadata 都没有），"
            "不能按 YOLOMM 双模态登记；请用 family=yolo，或换真实的双模态权重",
        )
    if family == "yolo" and actual:
        raise ApiError(
            409,
            code,
            "这个权重带真实多模态路由（{}），不能按单模态 YOLO 登记；"
            "请用 family=yolomm 并声明第二模态".format(
                "、".join(key for key, value in evidence.items() if value) or "vendor 判定"
            ),
        )


def _require_secondary_matches(secondary: str | None, info: dict, *, code: str = "MODEL_MODALITY_MISMATCH") -> None:
    """权重 metadata 写了第二模态时，声明必须与它一致；没写就不猜。"""
    actual = str(info.get("secondary_modality") or "").strip().lower()
    declared = str(secondary or "").strip().lower()
    if not actual or not declared:
        return
    if actual != declared:
        raise ApiError(
            409,
            code,
            f"权重里的第二模态是 {actual}，与声明的 {declared} 不一致；"
            "请按权重真实模态登记，不要把 IR 标成别的模态",
        )


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
    _require_family_matches(fam, info)
    _require_secondary_matches(secondary, info)
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
        gone = conn.execute(
            "SELECT deleted_at FROM models WHERE project_id = ? AND name = ?",
            (str(project_id), label),
        ).fetchone()
        if gone is not None and gone["deleted_at"]:
            raise ApiError(
                409, "MODEL_NAME_TAKEN", "同名模型在回收站里，请先恢复它或换个名字"
            ) from None
        raise ApiError(409, "MODEL_NAME_TAKEN", "这个项目里已经有同名模型，请换个名字") from None
    return public(get_owned(conn, user_id, project_id, row_id))


def _class_signature(items: Any) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or "index" not in item:
                continue
            try:
                out.append((int(item["index"]), str(item.get("name") or "")))
            except (TypeError, ValueError):
                continue
    return sorted(out)


def _verify_publishable_weight(conn, user_id: int, project_id: str, row: sqlite3.Row) -> None:
    """发布前**再验一次真实权重**：文件在、能加载出模型、类别表与登记一致。

    导入/训练登记已经验过一次，但发布是另一个人工动作，中间文件可能被删除或替换。
    这里复用同一套 ``inspect_weight``（安全加载 + 模型对象/架构校验），
    绝不因为“登记时通过过”就放行。
    """
    rel = str(row["weight_rel"] or "")
    if not rel:
        raise ApiError(409, "MODEL_WEIGHT_MISSING", "这个模型没有绑定权重文件，不能发布")
    root = storage.project_root(conn, user_id, project_id)
    try:
        path = storage.resolve_rel(root, rel, must_exist=True)
    except ApiError:
        raise ApiError(
            409,
            "MODEL_WEIGHT_MISSING",
            "模型的权重文件不存在（可能被删除，或项目目录当前不可用），不能发布",
        ) from None
    try:
        info = vision_runtime.inspect_weight(path)
    except Exception as exc:  # noqa: BLE001 - VisionError 等统一映射成发布失败
        raise ApiError(
            409,
            "MODEL_WEIGHT_INVALID",
            f"模型的权重文件没通过校验（{getattr(exc, 'code', type(exc).__name__)}）："
            f"{getattr(exc, 'message', exc)}"[:500],
        ) from None
    if _class_signature(_loads(row["classes"], [])) != _class_signature(info["classes"]):
        raise ApiError(
            409,
            "MODEL_WEIGHT_CHANGED",
            "权重文件里的类别表与登记时不一致（文件可能被替换），拒绝发布",
        )
    # 文件被换成另一种模态结构的权重时，发布同样要拦住：否则页面会按单模态去跑
    # 一个双模态权重（或反过来），结果不可解释。
    _require_family_matches(
        str(row["family"] or "yolo"), info, code="MODEL_WEIGHT_CHANGED"
    )
    _require_secondary_matches(
        row["secondary_modality"], info, code="MODEL_WEIGHT_CHANGED"
    )


def publish(conn, user_id: int, project_id: str, model_id: str) -> dict:
    """发布模型，并**明确说出**它取代了本项目里哪些旧模型。

    取代范围只有“同一项目、同一所有者、当前已发布”的模型：不碰别的项目，
    也不碰公共资源或默认权重。被取代的旧模型保留全部记录与权重，只是不再是
    本项目当前用于检测的版本（状态记为 ``superseded``，与用户主动“归档”分开）。
    """
    row = get_owned(conn, user_id, project_id, model_id)
    if row["deleted_at"]:
        raise ApiError(409, "MODEL_DELETED", "这个模型在回收站里，先恢复再发布")
    if not row["dataset_id"]:
        raise ApiError(
            409,
            "DATASET_REQUIRED",
            "发布前必须绑定数据集：否则无法确定输出的类别索引对应什么类别",
        )
    mapping = _loads(row["class_map"], {})
    native = _loads(row["classes"], [])
    if not native:
        raise ApiError(
            409,
            "MODEL_NO_CLASSES",
            "这个模型没有类别表（权重解析失败或登记缺失），发布后检测端无法解释输出，拒绝发布",
        )
    if not mapping:
        raise ApiError(409, "CLASS_MAPPING_REQUIRED", "类别映射未完成，不能发布")
    missing = [item["index"] for item in native if str(item["index"]) not in mapping] if all(
        isinstance(item, dict) and "index" in item for item in native
    ) else []
    if missing:
        raise ApiError(409, "CLASS_MAPPING_REQUIRED", f"这些类别还没有映射：{missing}")
    _verify_publishable_weight(conn, user_id, project_id, row)
    superseded = conn.execute(
        "SELECT id, name FROM models WHERE project_id = ? AND owner_id = ?"
        " AND status = 'published' AND id <> ? AND deleted_at IS NULL",
        (str(project_id), int(user_id), str(model_id)),
    ).fetchall()
    now = security.now_iso()
    conn.execute(
        "UPDATE models SET status = 'superseded' WHERE project_id = ? AND owner_id = ?"
        " AND status = 'published' AND id <> ?",
        (str(project_id), int(user_id), str(model_id)),
    )
    conn.execute(
        "UPDATE models SET status = 'published', published_at = ? WHERE id = ? AND owner_id = ?",
        (now, str(model_id), int(user_id)),
    )
    conn.commit()
    result = public(get_owned(conn, user_id, project_id, model_id))
    result["replaced"] = [{"id": str(item["id"]), "name": str(item["name"])} for item in superseded]
    return result


def unpublish(conn, user_id: int, project_id: str, model_id: str) -> dict:
    """撤销发布：回到“可用但未发布”，不影响其他模型，也不删除任何文件。"""
    row = get_owned(conn, user_id, project_id, model_id)
    if str(row["status"]) != "published":
        raise ApiError(409, "MODEL_NOT_PUBLISHED", "这个模型当前不是发布状态，无需撤销")
    conn.execute(
        "UPDATE models SET status = 'imported', published_at = NULL WHERE id = ? AND owner_id = ?",
        (str(model_id), int(user_id)),
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


def delete(conn, user_id: int, project_id: str, model_id: str) -> dict:
    """软删：只标记时间，权重文件与训练记录都保留，可恢复。

    发布中的模型不能直接进回收站——先撤销发布，避免检测端指针悬空。
    """
    row = get_owned(conn, user_id, project_id, model_id)
    if row["deleted_at"]:
        return public(row)
    if str(row["status"]) == "published":
        raise ApiError(409, "MODEL_PUBLISHED", "这个模型还在发布中，请先撤销发布再删除")
    conn.execute(
        "UPDATE models SET deleted_at = ? WHERE id = ? AND owner_id = ?",
        (security.now_iso(), str(model_id), int(user_id)),
    )
    conn.commit()
    return public(get_owned(conn, user_id, project_id, model_id))


def restore(conn, user_id: int, project_id: str, model_id: str) -> dict:
    row = get_owned(conn, user_id, project_id, model_id)
    if not row["deleted_at"]:
        return public(row)
    clash = conn.execute(
        "SELECT id FROM models WHERE project_id = ? AND name = ? AND id <> ? AND deleted_at IS NULL",
        (str(project_id), str(row["name"]), str(model_id)),
    ).fetchone()
    if clash is not None:
        raise ApiError(409, "MODEL_NAME_TAKEN", "同名模型已被别的记录占用，请先把那条改名")
    conn.execute(
        "UPDATE models SET deleted_at = NULL WHERE id = ? AND owner_id = ?",
        (str(model_id), int(user_id)),
    )
    conn.commit()
    return public(get_owned(conn, user_id, project_id, model_id))


def update_model(
    conn, user_id: int, project_id: str, model_id: str, *, name: str | None = None, notes: str | None = None
) -> dict:
    row = get_owned(conn, user_id, project_id, model_id)
    if row["deleted_at"]:
        raise ApiError(409, "MODEL_DELETED", "这个模型在回收站里，先恢复再修改")
    if name is not None:
        label = str(name).strip()
        if not label:
            raise ApiError(400, "VALIDATION", "模型名称不能为空")
        try:
            conn.execute(
                "UPDATE models SET name = ? WHERE id = ? AND owner_id = ?",
                (label, str(model_id), int(user_id)),
            )
        except sqlite3.IntegrityError:
            raise ApiError(409, "MODEL_NAME_TAKEN", "这个项目里已经有同名模型，请换个名字") from None
    if notes is not None:
        conn.execute(
            "UPDATE models SET notes = ? WHERE id = ? AND owner_id = ?",
            (str(notes), str(model_id), int(user_id)),
        )
    conn.commit()
    return public(get_owned(conn, user_id, project_id, model_id))


def unique_name(conn, project_id: str, owner_id: int, base: str) -> str:
    """训练产出的模型自动取名：同名就追加 -2/-3，绝不覆盖已有记录。"""
    label = str(base or "训练模型").strip() or "训练模型"
    candidate = label
    for index in range(2, 200):
        taken = conn.execute(
            "SELECT 1 FROM models WHERE project_id = ? AND name = ?",
            (str(project_id), candidate),
        ).fetchone()
        if taken is None:
            return candidate
        candidate = f"{label}-{index}"
    raise ApiError(409, "NAME_CONFLICT", "同名模型过多，请改名")


def create_trained(
    conn,
    user_id: int,
    project_id: str,
    *,
    name: str,
    dataset_id: str,
    weight_rel: str,
    training_run_id: str,
    parent_model_id: str | None,
    metrics: dict,
    family: str = "yolo",
    secondary_modality: str | None = None,
    notes: str = "",
) -> dict:
    """把一次**真实训练**产出的权重登记成模型。

    与导入共用同一张表与同一套发布规则；差别只在 ``source='trained'``、
    父模型与指标来源，以及权重由训练目录指向（不复制第二份大文件）。

    契约（与导入完全一致）：

    * 权重必须通过**同一套安全加载**真读一遍——文本文件改名成 ``best.pt``
      会被拒绝，不会登记成“训练成功”；
    * ``classes`` 存权重里解析出的 ``[{index, name}]`` 对象列表（``class_labels``
      / 前端 / 导入侧都用这个契约，不存字符串列表）；
    * ``class_map`` 是权重索引 → 数据集索引的恒等映射，且**数量必须与数据集
      类别一致**——训练用的就是这份标注，对不上说明产物可疑，拒绝登记；
    * ``family``（yolo / yolomm）由训练分派传入，决定 ``model_mode``。
    """
    fam = str(family or "yolo").strip().lower()
    if fam not in FAMILIES:
        raise ApiError(400, "VALIDATION", f"训练产物的架构只能是 {'/'.join(FAMILIES)}，收到 {fam}")
    mode = FAMILIES[fam]
    secondary = str(secondary_modality or "").strip().lower() or None
    if mode == "multimodal" and not secondary:
        raise ApiError(400, "VALIDATION", "多模态训练产物必须声明第二模态（例如 ir / edge）")

    root = storage.project_root(conn, user_id, project_id)
    weight_path = storage.resolve_rel(root, weight_rel, must_exist=True)
    if weight_path.suffix.lower() != ".pt":
        raise ApiError(409, "NOT_A_WEIGHT", "训练产出不是 .pt 权重，拒绝登记")
    # 真实权重校验：与导入共用同一套安全加载（safe_weight 白名单闸门）。
    try:
        info = vision_runtime.inspect_weight(weight_path)
    except Exception as exc:  # noqa: BLE001 - VisionError 等，统一映射成登记失败
        raise ApiError(
            409,
            "TRAINING_WEIGHT_INVALID",
            f"训练产出不是可加载的权重（{getattr(exc, 'code', type(exc).__name__)}）："
            f"{getattr(exc, 'message', exc)}"[:500],
        ) from None
    native = info["classes"]
    if not native:
        raise ApiError(
            409,
            "TRAINING_WEIGHT_INVALID",
            "训练产出的权重里没有类别表（names），无法登记：检测端会无法解释输出",
        )
    # 训练产物也要与声明的 family/第二模态一致：YOLOMM 训练出来的权重必须有真实路由，
    # 单模态训练不能偷偷产出双模态结构。
    _require_family_matches(fam, info, code="TRAINED_WEIGHT_MODALITY_MISMATCH")
    _require_secondary_matches(secondary, info, code="TRAINED_WEIGHT_MODALITY_MISMATCH")

    dataset = dataset_service.get_owned(conn, user_id, project_id, dataset_id)
    dataset_classes = [str(item) for item in _loads(dataset["classes"], [])]
    if len(native) != len(dataset_classes):
        raise ApiError(
            409,
            "TRAINED_CLASS_MISMATCH",
            f"训练产出类别数（{len(native)}）与数据集类别数（{len(dataset_classes)}）不一致，"
            "产物可疑，拒绝登记",
        )
    # 训练用的就是这份数据集的标注：权重索引与数据集索引一一对应。
    mapping = {str(index): index for index in range(len(native))}
    row_id = security.new_id("mdl")
    label = unique_name(conn, project_id, int(user_id), name)
    conn.execute(
        """
        INSERT INTO models
            (id, project_id, owner_id, name, family, model_mode, secondary_modality, dataset_id,
             weight_rel, weight_sha256, weight_size, classes, class_map, status, notes, created_at,
             source, parent_model_id, training_run_id, metrics)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'imported', ?, ?,
                'trained', ?, ?, ?)
        """,
        (
            row_id,
            str(project_id),
            int(user_id),
            label,
            fam,
            mode,
            secondary,
            str(dataset_id),
            storage.relative(root, weight_path),
            _sha256(weight_path),
            weight_path.stat().st_size,
            json.dumps(native, ensure_ascii=False),
            json.dumps(mapping, ensure_ascii=False),
            str(notes or ""),
            security.now_iso(),
            parent_model_id,
            training_run_id,
            json.dumps(metrics or {}, ensure_ascii=False),
        ),
    )
    conn.commit()
    return public(get_owned(conn, user_id, project_id, row_id))


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


def inspect_weight_source(
    conn,
    user_id: int,
    project_id: str,
    *,
    rel_path: str | None = None,
    asset_id: str | None = None,
) -> dict:
    """只读检查一份权重：返回类别、架构与**真实**模态，不登记、不推理。

    Agent 自动发现权重时先调它，再据结果选 family，避免靠文件名里的 "mm" 猜。
    来源只能是当前项目内的相对路径或本项目已上传的 weight 资产；反序列化走
    同一套 ``inspect_weight`` 安全闸门，不 dump 整个 checkpoint。
    """
    root = storage.project_root(conn, user_id, project_id)
    if asset_id:
        asset = _weight_asset(conn, user_id, project_id, asset_id)
        path = storage.resolve_rel(root, str(asset["rel_path"]), must_exist=True)
        source = {"asset_id": str(asset_id), "filename": str(asset["filename"])}
    elif rel_path:
        path = storage.resolve_rel(root, rel_path, must_exist=True)
        source = {"rel_path": str(rel_path)}
    else:
        raise ApiError(400, "SOURCE_REQUIRED", "需要提供项目内 .pt 路径或权重资产 ID")
    if path.suffix.lower() != ".pt":
        raise ApiError(409, "NOT_A_WEIGHT", "只检查 .pt 权重文件")
    try:
        info = vision_runtime.inspect_weight(path)
    except vision_runtime.VisionError as exc:
        raise ApiError(409, "WEIGHT_INSPECT_FAILED", f"{exc.code}：{exc.message}"[:500]) from None
    return {
        **source,
        "path": storage.relative(root, path),
        "size": path.stat().st_size,
        "sha256": _sha256(path),
        "classes": [item["name"] for item in info["classes"]],
        "class_count": int(info["class_count"]),
        "kind": info["kind"],
        "task": info.get("task") or "",
        "multimodal": bool(info["multimodal"]),
        "primary_modality": info.get("primary_modality"),
        "secondary_modality": info.get("secondary_modality"),
        "evidence": info.get("multimodal_evidence") or {},
        "suggested_family": "yolomm" if info["multimodal"] else "yolo",
        "note": "family 请按这里的结果选；不确定的第二模态仍由数据集或用户声明，不从文件名猜。",
    }


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


def class_labels(
    row: sqlite3.Row, dataset_classes: list[str] | None = None
) -> dict[int, str]:
    """输出侧展示用的类别名。

    键是**目标类别索引**（数据集口径），值优先取目标数据集的类别名；
    只有模型没绑定数据集时才回落到权重自带名称。之前这里把 native 名称放到
    target 键上，导致映射到「单一热斑」后界面仍显示 ``single_hotspot``。
    """
    mapping = _loads(row["class_map"], {})
    native = {int(item["index"]): str(item["name"]) for item in _loads(row["classes"], [])}
    if not mapping:
        return native
    labels: dict[int, str] = {}
    for source, target in mapping.items():
        source_id, target_id = int(source), int(target)
        if dataset_classes and 0 <= target_id < len(dataset_classes):
            labels[target_id] = str(dataset_classes[target_id])
        else:
            labels[target_id] = native.get(source_id, str(target_id))
    return labels


def labels_for(conn, row: sqlite3.Row) -> dict[int, str]:
    """按模型绑定的数据集取目标类别名（没有数据集才用权重自带名称）。"""
    classes: list[str] = []
    dataset_id = row["dataset_id"]
    if dataset_id:
        found = conn.execute(
            "SELECT classes FROM datasets WHERE id = ? AND project_id = ?",
            (str(dataset_id), str(row["project_id"])),
        ).fetchone()
        if found is not None:
            classes = [str(item) for item in _loads(found["classes"], [])]
    return class_labels(row, classes)


__all__ = [
    "archive",
    "class_labels",
    "create_trained",
    "delete",
    "labels_for",
    "get_owned",
    "import_model",
    "inspect_weight_source",
    "list_for_project",
    "public",
    "publish",
    "published_for_project",
    "restore",
    "unique_name",
    "unpublish",
    "update_model",
]

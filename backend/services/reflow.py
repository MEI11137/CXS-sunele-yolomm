"""数据回流：把人工确认过的记录变成**真实文件**的新数据集版本。

两种模式：

* ``revision``（回原修订）：以某个已有数据集为父版本，识别为同图的样本在**相同划分、
  相同相对位置**用人工最终框替换旧标注，样本数不增加；确认无缺陷就写空 label。
  真正的新图片只有在用户显式指定 ``action=append`` 与划分时才追加，且要过身份去重。
* ``new``（另建数据集）：只取用户选中的已确认样本，按 1/1/1 或比例 + 固定种子划分；
  图片、配图、label 一起移动，同一身份不会被分到两个划分（防泄漏）。

所有产物都落在项目工作区 ``datasets/<id>/``：``images/``、``images_ir/``（或
``images_edge/``）、``labels/``、``data.yaml``、``manifest.json``、``export/dataset.zip``。
库里的记录只是索引，导出与追溯以这些文件为准。

幂等：``(project_id, request_key)`` 唯一；同一次请求不会生成两份数据集。
歧义（同图多命中）不自动覆盖，返回冲突让用户选。
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import random
import sqlite3
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from backend import security
from backend.errors import ApiError
from backend.services import assets as asset_service
from backend.services import datasets as dataset_service
from backend.services import detection as detection_service
from backend.services import identity as identity_service
from backend.services import labels as label_service
from backend.services import review as review_service
from backend.services import storage

SPLITS = ("train", "val", "test")
DEFAULT_RATIOS = {"train": 1, "val": 1, "test": 1}
MAX_SAMPLES = 5000
# 目录名 <> 模态。导入侧读 data.yaml 的 modalities 映射，所以写出时必须用**真实目录**，
# 不能拿“划分名”当目录（历史上这里取过 parent.name，把 train 当成了模态）。
FOLDER_MODALITY = {
    "images": "rgb",
    "images_ir": "ir",
    "images_edge": "edge",
    "images_depth": "depth",
    # 参考模态不是 rgb 时，辅模态 rgb 不能也放进 images/（会和主图撞目录），
    # 统一写 images_rgb/ 并在 data.yaml 里映射回 rgb。
    "images_rgb": "rgb",
}
ROLE_FOLDER = {"rgb": "images", "ir": "images_ir", "edge": "images_edge", "depth": "images_depth"}


def _folder_of(path_like: Any) -> str:
    """从相对/绝对路径里认出模态目录（取最靠后的 images* 段）。"""
    parts = PurePosixPath(str(path_like).replace("\\", "/")).parts
    for part in reversed(parts):
        if part in FOLDER_MODALITY:
            return part
    return "images_ir"


def _modality_of(rel: Any) -> str:
    return FOLDER_MODALITY.get(_folder_of(rel), "ir")


def _secondary_folder(modality: str | None) -> str | None:
    """辅模态在数据集里的目录名；与参考模态的 images/ 冲突时改用 images_<modality>。"""
    mod = str(modality or "").strip().lower()
    if mod not in ROLE_FOLDER:
        return None
    folder = ROLE_FOLDER[mod]
    if folder == "images":
        folder = f"images_{mod}"
    return folder


def _loads(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (ValueError, TypeError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _dataset(conn, user_id: int, project_id: str, dataset_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM datasets WHERE id = ? AND project_id = ? AND owner_id = ?",
        (str(dataset_id), str(project_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise ApiError(404, "DATASET_NOT_FOUND", "数据集不存在或不属于当前项目")
    return row


def _samples(conn, dataset_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM dataset_samples WHERE dataset_id = ? ORDER BY split, stem",
        (str(dataset_id),),
    ).fetchall()


def _confirmed_version(conn, user_id: int, record_id: str, version_id: str | None) -> sqlite3.Row:
    if version_id:
        row = conn.execute(
            "SELECT * FROM review_versions WHERE id = ? AND detection_id = ? AND owner_id = ?",
            (str(version_id), str(record_id), int(user_id)),
        ).fetchone()
    else:
        # 默认只认**当前生效**的人工结论：最近一次已提交版本必须还是 confirmed。
        # 用户后来驳回了，历史里那条更早的 confirmed 已经失效，不能静默沿用。
        row = review_service.effective_review(conn, str(record_id), int(user_id))
    if row is None:
        raise ApiError(
            409,
            "REVIEW_REQUIRED",
            "这条记录还没有已确认的人工结论，不能回流",
            details={"record_id": str(record_id)},
        )
    if row["status"] != "confirmed":
        raise ApiError(
            409,
            "REVIEW_NOT_CONFIRMED",
            "这条记录当前生效的人工结论是“已驳回”，不能按旧确认回流；请重新确认或显式指定历史版本。",
            details={
                "record_id": str(record_id),
                "latest_version": int(row["version"]),
                "latest_status": row["status"],
            },
        )
    return row


def _asset_row(conn, user_id: int, project_id: str, asset_id: str) -> sqlite3.Row:
    return asset_service.get_owned(conn, user_id, project_id, asset_id)


def _target_dir(dataset_id: str, split: str) -> str:
    return f"{split}"


def _ratio_targets(total: int, ratios: dict[str, Any]) -> dict[str, int]:
    """按比例把 total 个样本分到各划分，保证总和等于 total 且不产生零配额集合。

    只保留**权重为正**的划分（train:0 就是“不要训练集”，不能悄悄塞进 train）；
    用最大余数法补齐四舍五入误差，避免 3 个样本 1/1/1 被算成 0/0/0。
    """
    weights: dict[str, float] = {}
    for name in SPLITS:
        try:
            value = float(ratios.get(name, 0) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            weights[name] = value
    if not weights:
        raise ApiError(400, "BAD_RATIOS", "按比例划分至少要有一个正数")
    weight_sum = sum(weights.values())
    raw = {name: total * value / weight_sum for name, value in weights.items()}
    base = {name: int(value) for name, value in raw.items()}
    rest = total - sum(base.values())
    for name in sorted(raw, key=lambda item: raw[item] - base[item], reverse=True)[: max(0, rest)]:
        base[name] += 1
    targets = {name: value for name, value in base.items() if value > 0}
    if not targets:
        first = max(weights, key=lambda item: weights[item])
        targets = {first: total}
    return targets


def _split_for(strategy: str, ratios: dict[str, Any], seed: int, items: list[dict]) -> list[dict]:
    """按身份分组后分配划分：同一张图不会同时进 train 与 val。

    显式指定划分的样本同样要做身份校验——用户把同一张图分别点进 train/val
    是泄漏，不是“他的自由”，必须报冲突让他改。返回冲突列表，不静默改动。
    """
    conflicts: list[dict] = []
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(str(item["identity_key"]), []).append(item)
    for key, group in groups.items():
        forced = {str(item["split"]) for item in group if item.get("split")}
        if len(forced) > 1:
            conflicts.append(
                {
                    "record_id": str(group[0].get("record_id") or ""),
                    "code": "SPLIT_LEAK",
                    "message": f"同一张图被指到了多个划分（{'、'.join(sorted(forced))}）；同图不能同时进训练与验证。",
                    "details": {
                        "identity": key,
                        "records": [str(item.get("record_id") or "") for item in group],
                        "splits": sorted(forced),
                    },
                }
            )
    if conflicts:
        return conflicts

    pending = [group for group in groups.values() if not any(item.get("split") for item in group)]
    pending.sort(key=lambda group: str(group[0]["identity_key"]))
    random.Random(int(seed or 0)).shuffle(pending)
    if strategy == "ratio":
        targets = _ratio_targets(sum(len(group) for group in pending), ratios)
        order = [name for name in SPLITS if name in targets]
        remaining = dict(targets)
        for group in pending:
            name = next((item for item in order if remaining[item] >= len(group)), None)
            if name is None:
                name = max(order, key=lambda item: remaining[item])
            for item in group:
                item["split"] = name
            remaining[name] = max(0, remaining[name] - len(group))
    elif strategy == "manual":
        for group in pending:
            conflicts.append(
                {
                    "record_id": str(group[0].get("record_id") or ""),
                    "code": "SPLIT_REQUIRED",
                    "message": "逐样本划分时每条记录都要指定 train/val/test",
                    "details": {"identity": str(group[0]["identity_key"])},
                }
            )
    else:
        for index, group in enumerate(pending):
            name = SPLITS[index % 3]
            for item in group:
                item["split"] = name
    for item in items:
        if not item.get("split"):
            item["split"] = "train"
    return conflicts


def preview(
    conn,
    user_id: int,
    project_id: str,
    *,
    mode: str,
    dataset_id: str | None,
    selections: list[dict],
    strategy: str = "even",
    ratios: dict[str, int] | None = None,
    seed: int = 0,
    reference_modality: str | None = None,
) -> dict:
    plan = _plan(
        conn,
        user_id,
        project_id,
        mode=mode,
        dataset_id=dataset_id,
        selections=selections,
        strategy=strategy,
        ratios=ratios,
        seed=seed,
        reference_modality=reference_modality,
    )
    result = {
        "mode": plan["mode"],
        "dataset_id": plan.get("dataset_id"),
        "parent_dataset_id": plan.get("parent_dataset_id"),
        # 数据集以哪一模态的像素坐标作为 labels/ 的参考，以及另一模态是什么。
        "reference_modality": plan.get("reference_modality"),
        "secondary_modality": plan.get("secondary_modality"),
        "counts": {
            "replace": sum(1 for item in plan["items"] if item["action"] == "replace"),
            "append": sum(1 for item in plan["items"] if item["action"] == "append"),
            "excluded": sum(1 for item in plan["items"] if item["action"] == "exclude"),
            "kept": len(plan["kept"]),
            "conflicts": len(plan["conflicts"]),
        },
        "items": [
            {
                "record_id": item["record_id"],
                "action": item["action"],
                "split": item.get("split"),
                "stem": item.get("stem"),
                "outcome": item.get("outcome"),
                "boxes": len(item.get("output_primary_boxes") or []),
                "x_boxes": len(item.get("output_secondary_boxes") or []),
                "reference_modality": item.get("output_reference_modality"),
                "secondary_modality": item.get("output_secondary_modality"),
                "target_sample_id": (item.get("target_sample") or {}).get("id"),
                "matches": item.get("matches") or [],
                "hints": item.get("hints") or [],
            }
            for item in plan["items"]
        ],
        "conflicts": plan["conflicts"],
        "classes": plan["classes"],
        "splits": _split_counts(plan),
        "note": "预览只是计划；有冲突时不会写入任何文件。",
    }
    result["signature"] = plan_signature(result)
    return result


def plan_signature(plan: dict) -> str:
    """预览指纹：定稿前用它确认“用户看到的计划”还是当前真实状态算出来的那一份。"""
    payload = {
        "mode": plan.get("mode"),
        "dataset_id": plan.get("dataset_id"),
        "parent_dataset_id": plan.get("parent_dataset_id"),
        "reference_modality": plan.get("reference_modality"),
        "counts": plan.get("counts"),
        "items": [
            {
                "record_id": item.get("record_id"),
                "action": item.get("action"),
                "split": item.get("split"),
                "stem": item.get("stem"),
                "target": item.get("target_sample_id"),
                "boxes": item.get("boxes"),
                "x_boxes": item.get("x_boxes"),
                "reference_modality": item.get("reference_modality"),
            }
            for item in plan.get("items") or []
        ],
        "conflicts": sorted(str(item.get("code")) for item in plan.get("conflicts") or []),
    }
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _split_counts(plan: dict) -> dict:
    counts = {name: 0 for name in SPLITS}
    replaced = {
        str(item["target_sample"]["id"])
        for item in plan["items"]
        if item.get("target_sample") is not None
    }
    for item in plan["items"]:
        if item["action"] in {"replace", "append"} and item.get("split") in counts:
            counts[item["split"]] += 1
    for row in plan["kept"]:
        if str(row["id"]) in replaced:
            continue
        counts[str(row["split"])] = counts.get(str(row["split"]), 0) + 1
    return counts


def _plan(
    conn,
    user_id: int,
    project_id: str,
    *,
    mode: str,
    dataset_id: str | None,
    selections: list[dict],
    strategy: str,
    ratios: dict[str, int] | None,
    seed: int,
    reference_modality: str | None = None,
) -> dict:
    if mode not in {"revision", "new"}:
        raise ApiError(400, "BAD_MODE", "mode 只能是 revision 或 new")
    strategy = str(strategy or "even").lower()
    if strategy not in {"even", "ratio", "manual"}:
        raise ApiError(400, "BAD_STRATEGY", "strategy 只能是 even、ratio 或 manual")
    ratios = ratios or DEFAULT_RATIOS
    if strategy == "ratio":
        checked: dict[str, float] = {}
        for name, value in dict(ratios).items():
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ApiError(400, "BAD_RATIOS", f"划分比例 {name} 不是数字") from None
            if not math.isfinite(number) or number < 0:
                raise ApiError(400, "BAD_RATIOS", f"划分比例 {name} 必须是非负有限数")
            checked[str(name)] = number
        if sum(checked.values()) <= 0:
            raise ApiError(400, "BAD_RATIOS", "按比例划分至少要有一个正数")
        ratios = checked
    if not isinstance(selections, list) or not selections:
        raise ApiError(400, "SELECTION_REQUIRED", "需要至少一条记录")
    if len(selections) > MAX_SAMPLES:
        raise ApiError(400, "TOO_MANY", f"一次最多处理 {MAX_SAMPLES} 条")

    parent = None
    kept: list[dict] = []
    classes: list[str] = []
    samples: list[dict] = []
    identity_problems: list[dict] = []
    if mode == "revision":
        if not dataset_id:
            raise ApiError(400, "DATASET_REQUIRED", "回原修订必须指定父数据集")
        parent = _dataset(conn, user_id, project_id, dataset_id)
        # 父样本的身份一律取自**真实文件**：03 导入的老行 sha/pixel 为 NULL，
        # 只比库里两列会把同字节的原图判成新图。这里同时把缺失/被改的文件挑出来。
        prepared = identity_service.prepare_samples(conn, user_id, project_id, str(parent["id"]))
        samples = prepared["samples"]
        identity_problems = prepared["problems"]
        classes = [str(item) for item in _loads(parent["classes"], [])]
        kept = list(samples)
    else:
        classes = []

    items: list[dict] = []
    conflicts: list[dict] = []
    # 父数据集里有文件读不到或内容变过：整批先停。既不能当新图追加，
    # 也不能拿旧指纹把变过的文件原样复制进新版本。
    conflicts.extend(identity_service.problem_conflicts(identity_problems))
    seen_records: set[str] = set()
    seen_identities: dict[str, str] = {}
    seen_targets: set[str] = set()
    for selection in selections:
        record_id = str(selection.get("record_id") or "").strip()
        action = str(selection.get("action") or ("append" if mode == "new" else "replace")).lower()
        if record_id in seen_records:
            conflicts.append({"record_id": record_id, "code": "DUPLICATE_SELECTION", "message": "同一条记录在一次回流里只能选一次"})
            continue
        seen_records.add(record_id)
        if not record_id:
            conflicts.append({"record_id": "", "code": "MISSING_RECORD_ID", "message": "缺少 record_id"})
            continue
        if action not in {"replace", "append", "exclude"}:
            conflicts.append({"record_id": record_id, "code": "BAD_ACTION", "message": "action 不合法"})
            continue
        record = conn.execute(
            "SELECT * FROM detections WHERE id = ? AND project_id = ? AND owner_id = ?",
            (record_id, str(project_id), int(user_id)),
        ).fetchone()
        if record is None or record["deleted_at"]:
            conflicts.append({"record_id": record_id, "code": "RECORD_MISSING", "message": "记录不存在或已删除"})
            continue
        try:
            version = _confirmed_version(conn, user_id, record_id, selection.get("review_version_id"))
        except ApiError as exc:
            conflicts.append({"record_id": record_id, "code": exc.code, "message": exc.msg})
            continue
        # 反馈池是“待回流”的意向，不是永久授权：如果这条记录在加入反馈之后又产生了
        # 新的驳回/确认版本，旧反馈已经过期，回流时必须让用户重新确认，不能默认沿用。
        #
        # 只拿“选中版本本身有没有 pending 反馈”判断，不再要求“表里所有 pending 行都
        # 是同一版本”：旧版本的 pending 行是历史（``hx_list_feedback`` 的 all 口径），
        # 不能因为它长期留在表里就把新版本的回流永远卡成 STALE_FEEDBACK。
        stale = conn.execute(
            "SELECT id, review_version_id FROM record_feedback WHERE detection_id = ?"
            " AND owner_id = ? AND status = 'pending'",
            (record_id, int(user_id)),
        ).fetchall()
        picked = str(version["id"])
        matched = [row for row in stale if str(row["review_version_id"]) == picked]
        if stale and not matched:
            conflicts.append(
                {
                    "record_id": record_id,
                    "code": "STALE_FEEDBACK",
                    "message": "这条记录加入反馈后人工结论又变过，旧反馈已经过期；请重新加入反馈或显式指定要回流的版本。",
                    "details": {
                        "current_version": int(version["version"]),
                        "feedback_versions": [str(row["review_version_id"]) for row in stale],
                    },
                }
            )
            continue
        rgb_row = _asset_row(conn, user_id, project_id, str(record["rgb_asset_id"]))
        _, rgb_bytes = asset_service.read_bytes(conn, user_id, project_id, str(record["rgb_asset_id"]))
        identity = identity_service.identity_of_bytes(rgb_bytes)
        x_bytes = None
        x_filename = ""
        x_folder = "images_ir"
        x_row = None
        x_modality = None
        if record["x_asset_id"]:
            x_row, x_bytes = asset_service.read_bytes(
                conn, user_id, project_id, str(record["x_asset_id"])
            )
            x_filename = str(x_row["filename"])
            x_folder = ROLE_FOLDER.get(str(x_row["role"]), "images_ir")
            x_modality = str(x_row["role"] or "").strip().lower()
            if x_modality not in ROLE_FOLDER:
                x_modality = FOLDER_MODALITY.get(x_folder, "ir")
        record_classes = [
            item["name"]
            for item in review_service.class_options(conn, user_id, project_id, record)
        ]
        if not classes:
            classes = record_classes
        elif classes != record_classes:
            conflicts.append(
                {
                    "record_id": record_id,
                    "code": "CLASS_TABLE_MISMATCH",
                    "message": "这批记录的类别表不一致，不能合成同一个数据集版本",
                    "details": {"expected": classes, "found": record_classes},
                }
            )
            continue
        boxes = _loads(version["boxes"], [])
        if any(not (0 <= int(box.get("class_id", -1)) < len(classes)) for box in boxes):
            conflicts.append({"record_id": record_id, "code": "CLASS_OUT_OF_RANGE", "message": "复核框的类别索引超出目标数据集类别表"})
            continue

        if action == "exclude":
            items.append({"record_id": record_id, "action": "exclude", "identity_key": identity["pixel_sha256"] or identity["sha256"]})
            continue

        target: dict | None = None
        matching = identity_service.match_record(
            conn, user_id, project_id, record_id, parent, samples
        )
        matches = matching["matches"]
        explicit_target = str(selection.get("target_sample_id") or "").strip()
        candidates = {str(row["id"]): row for row in samples}
        if explicit_target:
            chosen = candidates.get(explicit_target)
            if chosen is None:
                conflicts.append({"record_id": record_id, "code": "BAD_TARGET_SAMPLE", "message": "指定的目标样本不在这个父数据集里"})
                continue
            if action == "append":
                conflicts.append({"record_id": record_id, "code": "BAD_ACTION", "message": "追加为新图不能同时指定替换目标"})
                continue
            # 显式选择保留，但不能把一张图的坐标写给另一张图：
            # 文件被改过、或尺寸对不上，都先停下来说明，不静默写入。
            if str(chosen.get("identity_state") or "") == identity_service.IDENTITY_DRIFT:
                conflicts.append(
                    {
                        "record_id": record_id,
                        "code": "SAMPLE_CHANGED",
                        "message": "指定的目标样本文件内容已经和导入时不一致，坐标不再可信；请先重新核验这个样本。",
                        "details": {"sample_id": explicit_target},
                    }
                )
                continue
            target_width = int(chosen.get("width") or 0)
            target_height = int(chosen.get("height") or 0)
            version_width = int(version["image_width"] or 0)
            version_height = int(version["image_height"] or 0)
            if (
                target_width
                and target_height
                and version_width
                and version_height
                and (target_width, target_height) != (version_width, version_height)
            ):
                conflicts.append(
                    {
                        "record_id": record_id,
                        "code": "TARGET_SIZE_MISMATCH",
                        "message": (
                            f"目标样本是 {target_width}×{target_height}，这条记录的图是 "
                            f"{version_width}×{version_height}；把这张图的坐标写给它是错的，已拒绝。"
                        ),
                        "details": {
                            "sample_id": explicit_target,
                            "target_size": [target_width, target_height],
                            "record_size": [version_width, version_height],
                        },
                    }
                )
                continue
            target = chosen
            matches = [{"sample_id": chosen["id"], "split": chosen["split"], "stem": chosen["stem"], "matched_by": "user_selected"}]
        elif matching["status"] == "ambiguous":
            conflicts.append(
                {
                    "record_id": record_id,
                    "code": "AMBIGUOUS_MATCH",
                    "message": matching["note"] or "这张图有多张可能对应的样本，必须由用户选择覆盖哪一张",
                    "matches": [
                        {"sample_id": item["sample_id"], "split": item["split"], "stem": item["stem"]}
                        for item in matches
                    ],
                }
            )
            continue
        elif matching["status"] == "candidate":
            # 来源同名 / 视觉相近都不足以证明同图：默认不替换，列候选等用户选。
            if action == "replace":
                pool = matches + list(matching.get("candidates") or [])
                conflicts.append(
                    {
                        "record_id": record_id,
                        "code": "MATCH_NEEDS_CHOICE",
                        "message": matching["note"]
                        or "只找到来源同名或视觉相近的候选，字节/像素并不一致，不能自动替换；请指定目标样本，或改用“新图追加”。",
                        "matches": [
                            {"sample_id": item["sample_id"], "split": item["split"], "stem": item["stem"]}
                            for item in pool
                        ],
                    }
                )
                continue
            # 追加为新图是允许的：写入的是这条记录自己的字节与本图坐标，
            # 不会动别人的标注；候选仅作为预览提示。
        elif len(matches) == 1:
            target = candidates.get(matches[0]["sample_id"])
            if target is None:
                conflicts.append({"record_id": record_id, "code": "SAMPLE_MISSING", "message": "匹配到的样本已不存在"})
                continue
            # 自动命中的样本如果文件已被改过，同样不能拿它当替换目标：
            # 记录的指纹能匹配，但现在磁盘上的已经不是你标注时看的那张图。
            if str(target.get("identity_state") or "") == identity_service.IDENTITY_DRIFT:
                conflicts.append(
                    {
                        "record_id": record_id,
                        "code": "SAMPLE_CHANGED",
                        "message": "匹配到的样本文件内容已经和导入时不一致，不能自动替换；请先重新核验这个样本，或改用“新图追加”。",
                        "details": {"sample_id": str(target["id"])},
                    }
                )
                continue
        duplicate_of = seen_identities.get(identity["pixel_sha256"] or identity["sha256"])
        if duplicate_of:
            conflicts.append(
                {
                    "record_id": record_id,
                    "code": "DUPLICATE_SELECTION_IMAGE",
                    "message": f"这批选择里已有同图记录 {duplicate_of}；同一张图不能在本批里被处理两次",
                }
            )
            continue
        seen_identities[identity["pixel_sha256"] or identity["sha256"]] = record_id

        if action == "replace" and target is None:
            conflicts.append(
                {
                    "record_id": record_id,
                    "code": "NEW_IMAGE",
                    "message": "这是数据集里没有的新图片；回原修订不会增加样本，请改用“追加”并指定划分。",
                }
            )
            continue
        if action == "append" and target is not None:
            conflicts.append(
                {
                    "record_id": record_id,
                    "code": "DUPLICATE_IMAGE",
                    "message": "这张图在数据集里已经存在，不能当新图片追加；请用“回原替换”。",
                    "matches": [{"sample_id": target["id"], "split": target["split"], "stem": target["stem"]}],
                }
            )
            continue
        split = str(selection.get("split") or "").lower() or None
        if split is not None and split not in SPLITS:
            conflicts.append({"record_id": record_id, "code": "BAD_SPLIT", "message": "split 只能是 train/val/test"})
            continue
        if mode == "revision" and action == "append" and not split:
            conflicts.append({"record_id": record_id, "code": "SPLIT_REQUIRED", "message": "把真正的新图片追加到原数据集时必须指定 train/val/test"})
            continue
        if mode == "new" and strategy == "manual" and not split:
            conflicts.append({"record_id": record_id, "code": "SPLIT_REQUIRED", "message": "逐样本划分时每条记录都要指定 train/val/test"})
            continue
        if mode == "new" and strategy != "manual":
            split = None  # 自动划分：不让 UI 的默认值悄悄把所有样本塞进 train
        if mode == "revision" and action == "replace" and target is not None:
            split = str(target["split"])
        requested_stem = str(selection.get("stem") or "").strip()
        if requested_stem:
            problem = _stem_problem(requested_stem)
            if problem:
                conflicts.append(
                    {
                        "record_id": record_id,
                        "code": "BAD_STEM",
                        "message": f"指定的文件名不可用：{problem}",
                        "details": {"stem": requested_stem},
                    }
                )
                continue
            stem = str(Path(requested_stem.replace("\\", "/")).stem)
        else:
            stem = _stem(rgb_row["filename"], split or "train")
        if target is not None:
            target_id = str(target["id"])
            if target_id in seen_targets:
                conflicts.append(
                    {
                        "record_id": record_id,
                        "code": "DUPLICATE_TARGET",
                        "message": f"同一个样本（{str(target['split'])}/{str(target['stem'])}）在本批里被选了两次替换，请只保留一条。",
                        "details": {"sample_id": target_id},
                    }
                )
                continue
            seen_targets.add(target_id)
        items.append(
            {
                "record_id": record_id,
                "action": action,
                "identity_key": identity["pixel_sha256"] or identity["sha256"],
                "sha256": identity["sha256"],
                "pixel_sha256": identity["pixel_sha256"],
                "outcome": version["outcome"],
                # 参考模态 = 记录主图的模态；boxes 只在这个模态的像素坐标里有意义。
                "reference_modality": str(
                    version["reference_modality"]
                    or detection_service.primary_modality_of_record(conn, user_id, project_id, record)
                    or "rgb"
                ).lower(),
                "secondary_modality": x_modality,
                "boxes": boxes,
                "x_boxes": _loads(version["x_boxes"], []),
                "version_id": version["id"],
                "version": int(version["version"]),
                "image_width": int(version["image_width"]),
                "image_height": int(version["image_height"]),
                "x_image_width": int(
                    version["x_image_width"] or (int(x_row["width"] or 0) if x_row else 0)
                ),
                "x_image_height": int(
                    version["x_image_height"] or (int(x_row["height"] or 0) if x_row else 0)
                ),
                "rgb_row": dict(rgb_row),
                "model_id": record["model_id"],
                "rgb_bytes": rgb_bytes,
                "x_asset_id": record["x_asset_id"],
                "x_bytes": x_bytes,
                "x_filename": x_filename,
                "x_folder": x_folder,
                "split": split,
                "stem": stem,
                "target_sample": dict(target) if target is not None else None,
                "matches": [
                    {"sample_id": row["sample_id"], "split": row["split"], "stem": row["stem"]}
                    for row in matches
                ],
                "hints": [
                    {"sample_id": row["sample_id"], "split": row["split"], "stem": row["stem"]}
                    for row in matching.get("hints") or []
                ],
            }
        )

    # 数据集以哪一模态作为 labels/ 的参考：用户显式指定优先，否则跟随记录，
    # 绝不能因为内部列名还叫 rgb_rel 就把 IR 主模态当 rgb。
    reference = _resolve_reference(
        conn, user_id, project_id, parent, items, reference_modality, conflicts
    )
    for item in items:
        _apply_output_spaces(item, reference, conflicts)
    secondary = _plan_secondary(items, reference)

    if conflicts:
        return {
            "mode": mode,
            "dataset_id": str(parent["id"]) if parent else None,
            "parent_dataset_id": str(parent["id"]) if parent else None,
            "primary_modality": reference,
            "reference_modality": reference,
            "secondary_modality": secondary,
            "items": items,
            "kept": kept,
            "conflicts": conflicts,
            "classes": classes,
        }
    conflicts = _split_for(strategy, ratios or DEFAULT_RATIOS, seed, items)
    return {
        "mode": mode,
        "dataset_id": str(parent["id"]) if parent else None,
        "parent_dataset_id": str(parent["id"]) if parent else None,
        "primary_modality": reference,
        "reference_modality": reference,
        "secondary_modality": secondary,
        "items": items,
        "kept": kept,
        "conflicts": conflicts,
        "classes": classes,
    }


def _resolve_reference(conn, user_id: int, project_id: str, parent, items: list[dict],
                       requested: str | None, conflicts: list[dict]) -> str:
    """确定输出数据集的参考模态（labels/ 的像素坐标空间）。"""
    text = str(requested or "").strip().lower() or None
    if text is not None and text not in ROLE_FOLDER:
        conflicts.append({
            "record_id": "",
            "code": "REFERENCE_MODALITY_INVALID",
            "message": "参考模态只能是 rgb / ir / edge / depth 之一。",
            "details": {"requested": text},
        })
        return "rgb"
    if text is not None:
        chosen = text
    else:
        refs = sorted({
            str(item.get("reference_modality") or "").lower()
            for item in items
            if str(item.get("reference_modality") or "").strip()
        })
        if len(refs) > 1:
            conflicts.append({
                "record_id": "",
                "code": "REFERENCE_MODALITY_MIXED",
                "message": "这批记录的参考模态不一致（%s）；请显式指定用哪一模态作为 labels/ 的参考。" % "、".join(refs),
                "details": {"modalities": refs},
            })
            chosen = refs[0]
        elif refs:
            chosen = refs[0]
        elif parent is not None:
            chosen = dataset_service.reference_modality(parent)
        else:
            chosen = "rgb"
    if parent is not None:
        # 父数据集的主模态/参考模态以显式列优先，不能按 modalities 顺序猜。
        parent_primary = dataset_service.reference_modality(parent)
        if parent_primary != chosen:
            conflicts.append({
                "record_id": "",
                "code": "REFERENCE_MODALITY_MISMATCH",
                "message": "父数据集的主模态是 %s，本次标注参考模态是 %s；回原修订不能改父数据集的模态布局，请改用「另建数据集」或选择与父数据集一致的参考模态。" % (parent_primary, chosen),
                "details": {"parent_primary": parent_primary, "reference_modality": chosen},
            })
    return chosen


def _plan_secondary(items: list[dict], reference: str) -> str | None:
    for item in items:
        sec = item.get("output_secondary_modality")
        if sec and sec != reference:
            return str(sec).lower()
    return None


def _suffix_of(name) -> str:
    suffix = Path(str(name or "")).suffix.lower()
    return suffix if suffix else ".jpg"


def _apply_output_spaces(item: dict, reference: str, conflicts: list[dict]) -> None:
    """把一条记录的 boxes/x_boxes 映射到“参考模态 / 辅模态”两个真实输出空间。

    两模态图片尺寸或内容未配准，绝不做坐标换算：只交换整套框与整套图，
    换过去以后框仍然按各自原图像素坐标校验。
    """
    if item.get("action") == "exclude":
        item["output_reference_modality"] = None
        item["output_secondary_modality"] = None
        item["output_primary_boxes"] = []
        item["output_secondary_boxes"] = []
        item["output_primary_size"] = [0, 0]
        item["output_secondary_size"] = None
        item["output_primary_bytes"] = None
        item["output_secondary_bytes"] = None
        item["output_secondary_filename"] = ""
        item["output_secondary_folder"] = None
        return

    record_id = str(item.get("record_id") or "")
    ref_mod = str(item.get("reference_modality") or "rgb").lower()
    sec_mod = str(item.get("secondary_modality") or "").lower() or None
    primary = {
        "boxes": list(item.get("boxes") or []),
        "size": [int(item.get("image_width") or 0), int(item.get("image_height") or 0)],
        "bytes": item.get("rgb_bytes"),
        "suffix": _suffix_of((item.get("rgb_row") or {}).get("filename")),
    }
    secondary = None
    if item.get("x_bytes") is not None and sec_mod:
        secondary = {
            "boxes": list(item.get("x_boxes") or []),
            "size": [int(item.get("x_image_width") or 0), int(item.get("x_image_height") or 0)],
            "bytes": item.get("x_bytes"),
            "suffix": _suffix_of(item.get("x_filename")),
        }

    if reference == ref_mod:
        out_ref, out_sec = ref_mod, sec_mod
        out_primary, out_secondary = primary, secondary
    elif secondary is not None and reference == sec_mod:
        out_ref, out_sec = sec_mod, ref_mod
        out_primary, out_secondary = secondary, primary
    else:
        conflicts.append({
            "record_id": record_id,
            "code": "MODALITY_UNAVAILABLE",
            "message": "这条记录没有 %s 模态的图片，不能把它当参考模态；请改选含该模态的记录或换一个参考模态。" % reference,
            "details": {
                "record_id": record_id,
                "reference_modality": reference,
                "available": [ref_mod] + ([sec_mod] if sec_mod else []),
            },
        })
        return

    width, height = out_primary["size"]
    for box in out_primary["boxes"]:
        x1, y1, x2, y2 = [float(value) for value in box["bbox"]]
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            conflicts.append({
                "record_id": record_id,
                "code": "BOX_OUT_OF_REFERENCE_IMAGE",
                "message": "参考模态的标注框超出该模态原图尺寸，不能转换或裁切；请回到复核页按该模态原图重新标注。",
                "details": {"record_id": record_id, "reference_modality": reference, "size": [width, height]},
            })
            return

    if out_secondary is not None:
        width, height = out_secondary["size"]
        for box in out_secondary["boxes"]:
            x1, y1, x2, y2 = [float(value) for value in box["bbox"]]
            if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
                conflicts.append({
                    "record_id": record_id,
                    "code": "BOX_OUT_OF_SECONDARY_IMAGE",
                    "message": "辅模态的标注框超出该模态原图尺寸，不能转换或裁切；请回到复核页按该模态原图重新标注。",
                    "details": {"record_id": record_id, "secondary_modality": out_sec, "size": [width, height]},
                })
                return

    if item.get("outcome") == "defect" and not out_primary["boxes"] and out_secondary and out_secondary["boxes"]:
        conflicts.append({
            "record_id": record_id,
            "code": "REFERENCE_HAS_NO_BOXES",
            "message": "%s 参考模态没有标注框，只有 %s 模态有框；这样导出会把空标签写成“确认无缺陷”的负样本。请把含框的模态选为参考模态，或改用「另建数据集」。" % (reference, out_sec),
            "details": {"record_id": record_id, "reference_modality": reference, "secondary_modality": out_sec},
        })
        return

    item["output_reference_modality"] = out_ref
    item["output_secondary_modality"] = out_sec
    item["output_primary_boxes"] = out_primary["boxes"]
    item["output_secondary_boxes"] = out_secondary["boxes"] if out_secondary else []
    item["output_primary_size"] = out_primary["size"]
    item["output_secondary_size"] = out_secondary["size"] if out_secondary else None
    item["output_primary_bytes"] = out_primary["bytes"]
    item["output_secondary_bytes"] = out_secondary["bytes"] if out_secondary else None
    item["output_secondary_filename"] = item.get("x_filename") if out_ref == sec_mod else (item.get("rgb_row") or {}).get("filename", "")
    item["output_secondary_folder"] = _secondary_folder(out_sec)


def _primary_for(conn, user_id: int, project_id: str, parent, items: list[dict]) -> str:
    """输出数据集的**主模态**：回原修订跟父数据集，另建跟随所选记录用的模型。

    单 IR 数据集必须继续保持 ir，不能因为内部列名还叫 rgb_rel 就写成 rgb。
    """
    if parent is not None:
        return dataset_service.reference_modality(parent)
    for item in items:
        model_id = str(item.get("model_id") or "")
        if not model_id:
            continue
        row = conn.execute(
            "SELECT dataset_id FROM models WHERE id = ? AND project_id = ? AND owner_id = ?",
            (model_id, str(project_id), int(user_id)),
        ).fetchone()
        if row is not None and row["dataset_id"]:
            dataset = conn.execute(
                "SELECT modalities, reference_modality FROM datasets WHERE id = ?",
                (str(row["dataset_id"]),),
            ).fetchone()
            if dataset is not None:
                return dataset_service.reference_modality(dataset)
    return "rgb"


def _stem(filename: str, split: str) -> str:
    base = Path(str(filename or "image")).stem
    return storage.safe_name(base, fallback="image") or "image"


_RESERVED_STEMS = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def _stem_problem(raw: str) -> str | None:
    """显式 stem 必须是**单个安全文件名**；返回问题描述，None 表示可用。

    只靠 project_root 守卫不够：它只保证“在项目工作区内”，而
    ``../../../../x`` 正好落在工作区根上却不在本数据集目录里。这里先按名字拒绝。
    """
    text = str(raw or "")
    if "\x00" in text:
        return "文件名不能包含空字符"
    normalized = text.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        return "不能使用绝对路径或盘符"
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if len(parts) != 1 or parts[0] == ".." or ".." in normalized.split("/"):
        return "只能是一个文件名，不能包含目录或 .."
    name = parts[0]
    stem = Path(name).stem.strip(" .")
    if not stem:
        return "文件名不能为空或只有扩展名"
    if stem.lower() in _RESERVED_STEMS:
        return f"{stem} 是系统保留名，换一个"
    cleaned = storage.safe_name(stem, fallback="")
    if not cleaned or cleaned != stem:
        return "文件名含有不适合落盘的字符"
    return None


def apply(
    conn,
    user_id: int,
    project_id: str,
    *,
    mode: str,
    dataset_id: str | None,
    selections: list[dict],
    name: str,
    strategy: str = "even",
    ratios: dict[str, int] | None = None,
    seed: int = 0,
    request_key: str,
    session_id: str | None = None,
    reference_modality: str | None = None,
) -> dict:
    key = str(request_key or "").strip()
    if not key:
        raise ApiError(400, "REQUEST_KEY_REQUIRED", "缺少 request_key（用于幂等）")
    existing = conn.execute(
        "SELECT * FROM datasets WHERE project_id = ? AND owner_id = ? AND request_key = ?",
        (str(project_id), int(user_id), key),
    ).fetchone()
    if existing is not None:
        from backend.services import datasets as dataset_service

        return dataset_service.public(existing)

    plan = _plan(
        conn,
        user_id,
        project_id,
        mode=mode,
        dataset_id=dataset_id,
        selections=selections,
        strategy=strategy,
        ratios=ratios,
        seed=seed,
        reference_modality=reference_modality,
    )
    if plan["conflicts"]:
        raise ApiError(
            409,
            "REFLOW_CONFLICTS",
            "有冲突没有解决，本次没有写入任何文件",
            details={"conflicts": plan["conflicts"]},
        )
    if not plan["items"]:
        raise ApiError(400, "NO_SELECTION", "没有可回流的记录")

    root = storage.project_root(conn, user_id, project_id)
    new_id = security.new_id("dst")
    base = storage.project_dir(root, storage.DATASETS, new_id)
    try:
        result = _build(
            conn,
            user_id,
            project_id,
            root=root,
            base=base,
            db_id=new_id,
            plan=plan,
            name=_unique_name(conn, project_id, name or _default_name(plan)),
            strategy=strategy,
            ratios=ratios or DEFAULT_RATIOS,
            seed=int(seed or 0),
            request_key=key,
        )
    except Exception:
        try:
            storage.remove_tree(root, base)
        except ApiError:
            pass
        raise
    _mark_feedback(conn, project_id, user_id, plan, new_id)
    _notice(conn, project_id, user_id, session_id, new_id, plan)
    return result


def _default_name(plan: dict) -> str:
    return "回原修订" if plan["mode"] == "revision" else "另建数据集"


def _unique_name(conn, project_id: str, name: str) -> str:
    base = str(name).strip()[:60] or "数据集"
    candidate = base
    index = 2
    while conn.execute(
        "SELECT 1 FROM datasets WHERE project_id = ? AND name = ?", (str(project_id), candidate)
    ).fetchone():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def _build(
    conn,
    user_id: int,
    project_id: str,
    *,
    root: Path,
    base: Path,
    db_id: str,
    plan: dict,
    name: str,
    strategy: str,
    ratios: dict[str, int],
    seed: int,
    request_key: str,
) -> dict:
    classes = plan["classes"]
    names = [str(item) for item in classes]
    if not names:
        raise ApiError(409, "NO_CLASSES", "没有类别表，无法生成数据集")
    rows: list[dict] = []
    used_stems: set[tuple[str, str]] = set()

    # 1) 保留旧样本（修订模式）：原划分与原相对位置；被替换的那张不重复保留
    replaced = {
        str(item["target_sample"]["id"])
        for item in plan["items"]
        if item.get("target_sample") is not None
    }
    for sample in plan["kept"]:
        if str(sample["id"]) in replaced:
            continue
        rows.append(_copy_existing_sample(root, db_id, sample))
        used_stems.add((str(sample["split"]), str(sample["stem"])))

    # 2) 覆盖 / 追加
    for item in plan["items"]:
        if item["action"] == "exclude":
            continue
        split = str(item.get("split") or "train")
        stem = str(item.get("stem") or "image")
        while (split, stem) in used_stems:
            stem = f"{stem}-2"
        used_stems.add((split, stem))
        target = item.get("target_sample")
        if target is not None:
            stem = str(target["stem"])
            if item["action"] == "resplit":
                # 重新划分：图片、配图、label 原样复制到新 split，不改人工结论。
                row = _copy_existing_sample(root, db_id, target, split=split)
                row["split"] = split
                rows.append(row)
                continue
            # 同图替换：把原文件复制到新版本的**相同相对位置**，只改 label
            row = _copy_existing_sample(root, db_id, target, split=split)
            primary_size = item["output_primary_size"]
            label_dir = storage.project_dir(root, storage.DATASETS, db_id, "labels", split)
            label_path = label_service.write_label(
                root, label_dir, stem, item["output_primary_boxes"], primary_size[0], primary_size[1]
            )
            labels_ir_rel = _write_secondary_labels(
                root, db_id, split, stem, item.get("output_secondary_boxes") or [],
                item.get("output_secondary_size") or [0, 0],
            )
            row.update(
                {
                    "boxes": item["output_primary_boxes"],
                    "class_ids": [int(box["class_id"]) for box in item["output_primary_boxes"]],
                    "label_rel": storage.relative(root, label_path),
                    "labels_ir_rel": labels_ir_rel,
                    "width": int(primary_size[0]),
                    "height": int(primary_size[1]),
                    "origin_record_id": item["record_id"],
                    "origin_sample_key": str(
                        target.get("origin_sample_key") or f"{target['split']}/{target['stem']}"
                    ),
                    "review_version_id": item["version_id"],
                    "sha256": str(target["sha256"] or item["sha256"]),
                    "pixel_sha256": str(target["pixel_sha256"] or item["pixel_sha256"]),
                }
            )
            rows.append(row)
            continue
        row = _write_new_sample(root, db_id, split, stem, item)
        rows.append(row)

    # 3) 写出 data.yaml / manifest / zip
    _write_yaml(root, base, names, rows, plan)
    manifest = _manifest(plan, rows, db_id, name)
    manifest_path = storage.write_bytes(
        root, base / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    )
    export_rel = _export_zip(root, base, db_id)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["split"]] = counts.get(row["split"], 0) + 1
    now = security.now_iso()
    try:
        conn.execute(
            """
            INSERT INTO datasets (id, project_id, owner_id, name, modalities, secondary_modality,
                reference_modality, classes, root_rel, source, sample_count, split_counts, status,
                notes, created_at, parent_dataset_id, kind, seed, split_strategy, request_key,
                manifest_rel, export_rel)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'local', ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                db_id,
                str(project_id),
                int(user_id),
                name,
                json.dumps(_modalities(rows, plan.get("reference_modality") or plan.get("primary_modality") or "rgb")),
                _secondary(rows),
                plan.get("reference_modality") or plan.get("primary_modality") or "rgb",
                json.dumps(names, ensure_ascii=False),
                storage.relative(root, base),
                len(rows),
                json.dumps(counts, ensure_ascii=False),
                "",
                now,
                plan.get("parent_dataset_id"),
                "revision" if plan["mode"] == "revision" else "new",
                int(seed),
                str(strategy),
                request_key,
                storage.relative(root, manifest_path),
                export_rel,
            ),
        )
        conn.executemany(
            "INSERT INTO dataset_samples (id, dataset_id, split, stem, rgb_rel, x_rel, label_rel,"
            " labels_ir_rel, width, height, boxes, class_ids, created_at, origin_record_id,"
            " origin_sample_key, review_version_id, sha256, pixel_sha256)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    security.new_id("smp"),
                    db_id,
                    row["split"],
                    row["stem"],
                    row["rgb_rel"],
                    row["x_rel"],
                    row["label_rel"],
                    row.get("labels_ir_rel"),
                    row["width"],
                    row["height"],
                    json.dumps(row["boxes"], ensure_ascii=False),
                    json.dumps(row["class_ids"], ensure_ascii=False),
                    now,
                    row.get("origin_record_id"),
                    row.get("origin_sample_key"),
                    row.get("review_version_id"),
                    row.get("sha256"),
                    row.get("pixel_sha256"),
                )
                for row in rows
            ],
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        existing = conn.execute(
            "SELECT * FROM datasets WHERE project_id = ? AND request_key = ?",
            (str(project_id), request_key),
        ).fetchone()
        if existing is None:
            raise
        # 并发同 key：另一个线程已经落库。把本次新建但没入库的目录清掉，
        # 不能留下“一份数据集、两套文件”。
        storage.remove_tree(root, base)
        from backend.services import datasets as dataset_service

        return dataset_service.public(existing)

    from backend.services import datasets as dataset_service

    return dataset_service.public(
        conn.execute("SELECT * FROM datasets WHERE id = ?", (db_id,)).fetchone()
    )


def _copy_existing_sample(root: Path, dataset_id: str, sample, *, split: str | None = None) -> dict:
    split = str(split or sample["split"])
    stem = str(sample["stem"])
    rgb_src = storage.resolve_rel(root, str(sample["rgb_rel"]), must_exist=True)
    rgb_rel = _copy_into_split(root, dataset_id, rgb_src, split, f"{stem}{rgb_src.suffix}", "images")
    x_rel = None
    if sample["x_rel"]:
        x_src = storage.resolve_rel(root, str(sample["x_rel"]), must_exist=True)
        x_rel = _copy_into_split(
            root, dataset_id, x_src, split, f"{stem}{x_src.suffix}", _x_dir_for(x_src)
        )
    label_rel = None
    if sample["label_rel"]:
        label_src = storage.resolve_rel(root, str(sample["label_rel"]), must_exist=False)
        if label_src.is_file():
            label_rel = _copy_into_split(root, dataset_id, label_src, split, f"{stem}.txt", "labels")
    # 辅模态标注随主标签一起复制，保证回归修订时两套标注都不丢。
    labels_ir_rel = None
    labels_ir_src_rel = sample["labels_ir_rel"] if "labels_ir_rel" in sample.keys() else None
    if labels_ir_src_rel:
        labels_ir_src = storage.resolve_rel(root, str(labels_ir_src_rel), must_exist=False)
        if labels_ir_src.is_file():
            labels_ir_rel = _copy_into_split(
                root, dataset_id, labels_ir_src, split, f"{stem}.txt", "labels_ir"
            )
    return {
        "split": split,
        "stem": stem,
        "rgb_rel": rgb_rel,
        "x_rel": x_rel,
        "label_rel": label_rel,
        "labels_ir_rel": labels_ir_rel,
        "width": int(sample["width"] or 0),
        "height": int(sample["height"] or 0),
        "boxes": _loads(sample["boxes"], []),
        "class_ids": _loads(sample["class_ids"], []),
        "origin_record_id": sample["origin_record_id"],
        "origin_sample_key": sample["origin_sample_key"] or f"{split}/{stem}",
        "review_version_id": sample["review_version_id"],
        "sha256": sample["sha256"],
        "pixel_sha256": sample["pixel_sha256"],
    }


def _x_dir_for(path: Path) -> str:
    return _folder_of(path)


def _dataset_file(root: Path, dataset_id: str, folder: str, split: str, filename: str) -> Path:
    """算出本数据集内的落点，并确认它真的在这个数据集目录里。

    名字层面的校验（``_stem_problem``）是第一道；这里是兜底：即使调用方绕过
    preview 直接构造 item，也不能把文件写到工作区根或别的数据集里。
    """
    base = storage.project_dir(root, storage.DATASETS, dataset_id).resolve()
    target = storage.project_dir(root, storage.DATASETS, dataset_id, folder, split) / filename
    resolved = target.resolve()
    if resolved == base or not resolved.is_relative_to(base):
        raise ApiError(
            400,
            "BAD_STEM",
            "生成文件的落点不在本次新建的数据集目录内，已拒绝写入",
            details={"name": str(filename)},
        )
    return target


def _copy_into_split(
    root: Path, dataset_id: str, source: Path, split: str, filename: str, folder: str
) -> str:
    target = _dataset_file(root, dataset_id, folder, split, filename)
    storage.copy_into(root, source, target)
    return storage.relative(root, target)


def _secondary_label_dir(root: Path, dataset_id: str, split: str) -> Path:
    return storage.project_dir(root, storage.DATASETS, dataset_id, "labels_ir", split)


def _write_secondary_labels(
    root: Path, dataset_id: str, split: str, stem: str, boxes: list[dict], size: list[int]
) -> str | None:
    """辅模态标注单独写 labels_ir/，不与参考模态的 labels/ 混在一起。

    没有框时返回 None（不是写空文件）：空文件会被下游当成“确认无缺陷”。
    """
    if not boxes:
        return None
    width, height = int(size[0] or 0), int(size[1] or 0)
    if width <= 0 or height <= 0:
        raise ApiError(
            409,
            "IMAGE_SIZE_UNKNOWN",
            "辅模态图片尺寸未知，不能按原图像素坐标写标注；请重新打开复核页保存一次。",
        )
    label_dir = _secondary_label_dir(root, dataset_id, split)
    label_path = label_service.write_label(root, label_dir, stem, boxes, width, height)
    return storage.relative(root, label_path)


def _write_new_sample(root: Path, dataset_id: str, split: str, stem: str, item: dict) -> dict:
    suffix = Path(str(item["rgb_row"]["filename"])).suffix or ".jpg"
    rgb_path = _dataset_file(root, dataset_id, "images", split, f"{stem}{suffix}")
    storage.write_bytes(root, rgb_path, item["output_primary_bytes"])
    x_rel = None
    secondary_bytes = item.get("output_secondary_bytes")
    if secondary_bytes:
        secondary_filename = str(item.get("output_secondary_filename") or "image.jpg")
        x_suffix = Path(secondary_filename).suffix or ".jpg"
        x_folder = str(item.get("output_secondary_folder") or "images_ir")
        x_path = _dataset_file(root, dataset_id, x_folder, split, f"{stem}{x_suffix}")
        storage.write_bytes(root, x_path, secondary_bytes)
        x_rel = storage.relative(root, x_path)
    primary_size = item["output_primary_size"]
    label_dir = storage.project_dir(root, storage.DATASETS, dataset_id, "labels", split)
    label_path = label_service.write_label(
        root, label_dir, stem, item["output_primary_boxes"], primary_size[0], primary_size[1]
    )
    labels_ir_rel = _write_secondary_labels(
        root, dataset_id, split, stem, item.get("output_secondary_boxes") or [],
        item.get("output_secondary_size") or [0, 0],
    )
    return {
        "split": split,
        "stem": stem,
        "rgb_rel": storage.relative(root, rgb_path),
        "x_rel": x_rel,
        "label_rel": storage.relative(root, label_path),
        "labels_ir_rel": labels_ir_rel,
        "width": int(primary_size[0]),
        "height": int(primary_size[1]),
        "boxes": item["output_primary_boxes"],
        "class_ids": [int(box["class_id"]) for box in item["output_primary_boxes"]],
        "origin_record_id": item["record_id"],
        "origin_sample_key": None,
        "review_version_id": item["version_id"],
        "sha256": item["sha256"],
        "pixel_sha256": item["pixel_sha256"],
    }


def _modalities(rows: list[dict], primary: str = "rgb") -> list[str]:
    found = {_modality_of(row.get("x_rel")) for row in rows if row.get("x_rel")}
    return [primary] + sorted(found)


def _secondary(rows: list[dict]) -> str | None:
    found = sorted({_modality_of(row.get("x_rel")) for row in rows if row.get("x_rel")})
    return found[0] if found else None


def _write_yaml(root: Path, base: Path, names: list[str], rows: list[dict], plan: dict) -> None:
    payload: dict[str, Any] = {"path": ".", "nc": len(names), "names": list(names)}
    splits = sorted({row["split"] for row in rows})
    for split in splits:
        payload[split] = f"images/{split}"
    # modalities 的键是模态名、值是**真实目录名**；导入侧就是按这张表找 RGB 与配图。
    # 旧实现取 Path(x_rel).parent.name，目录布局是 datasets/<id>/images_ir/<split>/<file>，
    # 取到的是划分名 train，于是 YAML 里多出 train: train，IR 反而丢了。
    modalities = {str(plan.get("primary_modality") or "rgb"): "images"}
    for row in rows:
        if row.get("x_rel"):
            folder = _folder_of(row["x_rel"])
            modalities[_modality_of(folder)] = folder
    payload["modalities"] = modalities
    payload["modality_used"] = list(modalities)
    storage.write_bytes(root, base / "data.yaml", yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8"))


def _manifest(plan: dict, rows: list[dict], dataset_id: str, name: str) -> dict:
    return {
        "dataset_id": dataset_id,
        "name": name,
        "mode": plan["mode"],
        "parent_dataset_id": plan.get("parent_dataset_id"),
        "reference_modality": plan.get("reference_modality") or plan.get("primary_modality"),
        "secondary_modality": plan.get("secondary_modality"),
        "classes": plan["classes"],
        "created_at": security.now_iso(),
        "samples": [
            {
                "split": row["split"],
                "stem": row["stem"],
                "rgb_rel": row["rgb_rel"],
                "x_rel": row.get("x_rel"),
                "label_rel": row.get("label_rel"),
                "labels_ir_rel": row.get("labels_ir_rel"),
                "reference_modality": plan.get("reference_modality") or plan.get("primary_modality"),
                "secondary_modality": plan.get("secondary_modality"),
                "reference_size": [row.get("width"), row.get("height")],
                "sha256": row.get("sha256"),
                "pixel_sha256": row.get("pixel_sha256"),
                "source": {
                    "record_id": row.get("origin_record_id"),
                    "review_version_id": row.get("review_version_id"),
                    "sample_key": row.get("origin_sample_key"),
                },
            }
            for row in rows
        ],
        "note": "manifest 记录每个样本的来源记录与复核版本，便于追溯；不是训练结果。",
    }


def _export_zip(root: Path, base: Path, dataset_id: str) -> str:
    export_dir = storage.project_dir(root, storage.DATASETS, dataset_id, "export")
    target = export_dir / "dataset.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path == target:
                continue
            if path.parts[-2:-1] == ("export",):
                continue
            archive.write(path, path.relative_to(base).as_posix())
    storage.write_bytes(root, target, buffer.getvalue())
    return storage.relative(root, target)


def _mark_feedback(conn, project_id: str, user_id: int, plan: dict, dataset_id: str) -> None:
    for item in plan["items"]:
        if item["action"] == "exclude":
            continue
        conn.execute(
            "UPDATE record_feedback SET status = 'reflowed', dataset_id = ?, reflowed_at = ? "
            "WHERE project_id = ? AND owner_id = ? AND review_version_id = ? AND status = 'pending'",
            (str(dataset_id), security.now_iso(), str(project_id), int(user_id), str(item["version_id"])),
        )
    conn.commit()


def _notice(conn, project_id: str, user_id: int, session_id: str | None, dataset_id: str, plan: dict) -> None:
    from backend.services import jobs as job_service

    try:
        job, created = job_service.create(
            conn,
            project_id=project_id,
            owner_id=user_id,
            kind="reflow_notice",
            request_key=f"reflow-notice:{dataset_id}",
            payload={"dataset_id": dataset_id, "mode": plan["mode"]},
            session_id=session_id,
        )
        if not created:
            return
        from backend.services import datasets as dataset_service

        public = dataset_service.public(
            conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
        )
        job_service.update(
            conn,
            str(job["id"]),
            status="done",
            result=json.dumps({"dataset_id": dataset_id, "samples": public["sample_count"]}, ensure_ascii=False),
            finished_at=security.now_iso(),
        )
        job_service.emit(
            conn,
            job_service.get(conn, str(job["id"])),
            "reflow.done",
            {
                "dataset_id": dataset_id,
                "name": public["name"],
                "samples": public["sample_count"],
                "summary": f"回流完成：{public['name']}（{public['sample_count']} 个样本）",
            },
        )
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("回流通知写入失败 dataset=%s", dataset_id)


# --------------------------------------------------------------------------- 草稿 / 定稿 / 重新划分


def _draft_public(row) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "name": data["name"],
        "mode": data["mode"],
        "parent_dataset_id": data.get("parent_dataset_id"),
        "strategy": data["strategy"],
        "ratios": _loads(data.get("ratios"), {}),
        "seed": int(data.get("seed") or 0),
        "selections": _loads(data.get("selections"), []),
        "plan": _loads(data.get("plan"), {}),
        "status": data["status"],
        "error": (
            {"code": data.get("error_code"), "message": data.get("error_message")}
            if data.get("error_code")
            else None
        ),
        "dataset_id": data.get("dataset_id"),
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "finalized_at": data.get("finalized_at"),
        "deleted_at": data.get("deleted_at"),
    }


def _owned_draft(conn, user_id: int, project_id: str, draft_id: str):
    row = conn.execute(
        "SELECT * FROM reflow_drafts WHERE id = ? AND project_id = ? AND owner_id = ?",
        (str(draft_id), str(project_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise ApiError(404, "DRAFT_NOT_FOUND", "回流草稿不存在或不属于当前项目")
    return row


def list_drafts(conn, user_id: int, project_id: str, *, include_deleted: bool = False) -> dict:
    clause = "" if include_deleted else "AND status != 'deleted'"
    rows = conn.execute(
        f"SELECT * FROM reflow_drafts WHERE project_id = ? AND owner_id = ? {clause} "
        "ORDER BY created_at DESC LIMIT 100",
        (str(project_id), int(user_id)),
    ).fetchall()
    return {"drafts": [_draft_public(row) for row in rows]}


def read_draft(conn, user_id: int, project_id: str, draft_id: str) -> dict:
    """按真实草稿 ID 读取（含回收站里的），供页面/Agent 看当前选择与预览。"""
    row = _owned_draft(conn, user_id, project_id, draft_id)
    data = _draft_public(row)
    data["note"] = (
        "草稿只是计划，没有生成数据集文件；改过参数后旧预览会失效，需要重新保存预览再定稿。"
    )
    return data


def save_draft(
    conn,
    user_id: int,
    project_id: str,
    *,
    name: str,
    mode: str,
    dataset_id: str | None,
    selections: list[dict],
    strategy: str,
    ratios: dict | None,
    seed: int,
    session_id: str | None = None,
    draft_id: str | None = None,
    reference_modality: str | None = None,
) -> dict:
    """保存/更新草稿：只写计划，不生成 images/labels/ZIP。

    ``reference_modality`` 与预览保持同一参数并写进计划；定稿会复用它，
    避免“预览看的是 IR 参考、定稿又悄悄换回 RGB”。
    """
    label = str(name or "").strip()[:60] or _default_name({"mode": mode})
    plan = preview(
        conn,
        user_id,
        project_id,
        mode=mode,
        dataset_id=dataset_id,
        selections=selections,
        strategy=strategy,
        ratios=ratios,
        seed=seed,
        reference_modality=reference_modality,
    )
    status = "editing" if plan["counts"]["conflicts"] else "ready"
    now = security.now_iso()
    row = _owned_draft(conn, user_id, project_id, draft_id) if draft_id else None
    if row is not None and row["status"] == "finalized":
        raise ApiError(409, "DRAFT_FINALIZED", "这个草稿已经定稿，不能再改；请另存一份新草稿")
    payload = (
        label,
        mode,
        dataset_id if mode == "revision" else None,
        strategy,
        json.dumps(ratios or DEFAULT_RATIOS, ensure_ascii=False),
        int(seed or 0),
        json.dumps(selections, ensure_ascii=False),
        json.dumps(plan, ensure_ascii=False),
        status,
        None,
        None,
        now,
    )
    try:
        if row is None:
            draft_id = security.new_id("rfd")
            conn.execute(
                "INSERT INTO reflow_drafts (id, project_id, owner_id, session_id, name, mode,"
                " parent_dataset_id, strategy, ratios, seed, selections, plan, status,"
                " error_code, error_message, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (draft_id, str(project_id), int(user_id), session_id, *payload, now),
            )
        else:
            conn.execute(
                "UPDATE reflow_drafts SET name = ?, mode = ?, parent_dataset_id = ?, strategy = ?,"
                " ratios = ?, seed = ?, selections = ?, plan = ?, status = ?, error_code = ?,"
                " error_message = ?, updated_at = ? WHERE id = ?",
                (*payload, str(draft_id)),
            )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ApiError(409, "DRAFT_NAME_EXISTS", "已经有同名草稿，换一个名称或编辑原草稿") from None
    return _draft_public(_owned_draft(conn, user_id, project_id, str(draft_id)))


def delete_draft(conn, user_id: int, project_id: str, draft_id: str) -> dict:
    row = _owned_draft(conn, user_id, project_id, draft_id)
    if row["status"] == "deleted":
        return {"draft_id": str(draft_id), "status": "unchanged"}
    conn.execute(
        "UPDATE reflow_drafts SET status = 'deleted', deleted_at = ?, updated_at = ?"
        " WHERE id = ? AND project_id = ? AND owner_id = ?",
        (security.now_iso(), security.now_iso(), str(draft_id), str(project_id), int(user_id)),
    )
    conn.commit()
    return {"draft_id": str(draft_id), "status": "deleted", "note": "草稿已移入回收站；没有生成过数据集文件。"}


def restore_draft(conn, user_id: int, project_id: str, draft_id: str) -> dict:
    row = _owned_draft(conn, user_id, project_id, draft_id)
    if row["status"] != "deleted":
        return {"draft_id": str(draft_id), "status": "unchanged"}
    plan = _loads(row["plan"], {})
    status = "editing" if (plan.get("counts") or {}).get("conflicts") else "ready"
    conn.execute(
        "UPDATE reflow_drafts SET status = ?, deleted_at = NULL, updated_at = ? WHERE id = ?",
        (status, security.now_iso(), str(draft_id)),
    )
    conn.commit()
    return {"draft_id": str(draft_id), "status": status}


def finalize_draft(conn, user_id: int, project_id: str, draft_id: str) -> dict:
    """只有 ready 草稿能定稿；定稿后才真正生成一套数据集文件。"""
    row = _owned_draft(conn, user_id, project_id, draft_id)
    if row["status"] == "finalized":
        from backend.services import datasets as dataset_service

        return {
            "dataset": dataset_service.public(
                dataset_service.get_owned(conn, user_id, project_id, str(row["dataset_id"]))
            ),
            "created": False,
        }
    if row["status"] == "deleted":
        raise ApiError(409, "DRAFT_DELETED", "草稿在回收站里，请先恢复")
    if row["status"] != "ready":
        raise ApiError(409, "DRAFT_NOT_READY", "草稿里还有冲突或上次定稿失败，请修改后重新预览")
    plan = _loads(row["plan"], {})
    selections = _loads(row["selections"], [])
    reference_modality = plan.get("reference_modality") or None
    # 服务端自己核验：草稿存的是“那一刻的真实状态”。父数据集样本、复核结论或身份
    # 核验结果变过，就要求重新预览，而不是拿旧计划写文件。UI 的按钮状态只是辅助。
    fresh = preview(
        conn,
        user_id,
        project_id,
        mode=str(row["mode"]),
        dataset_id=row["parent_dataset_id"],
        selections=selections,
        strategy=str(row["strategy"]),
        ratios=_loads(row["ratios"], DEFAULT_RATIOS),
        seed=int(row["seed"] or 0),
        reference_modality=reference_modality,
    )
    stored_signature = str(plan.get("signature") or "")
    if stored_signature and not fresh["conflicts"] and fresh["signature"] != stored_signature:
        raise ApiError(
            409,
            "PLAN_STALE",
            "预览之后数据已经变了（父数据集样本、复核结论或文件核验结果有更新），"
            "这份预览不再代表现在的实际情况；请重新保存预览再定稿。",
            details={
                "stored": stored_signature,
                "current": fresh["signature"],
                "counts": {"stored": plan.get("counts"), "current": fresh["counts"]},
            },
        )
    key = f"reflow-draft:{row['id']}"
    try:
        dataset = apply(
            conn,
            user_id,
            project_id,
            mode=str(row["mode"]),
            dataset_id=row["parent_dataset_id"],
            selections=selections,
            name=str(row["name"]),
            strategy=str(row["strategy"]),
            ratios=_loads(row["ratios"], DEFAULT_RATIOS),
            seed=int(row["seed"] or 0),
            request_key=key,
            session_id=row["session_id"],
            reference_modality=reference_modality,
        )
    except ApiError as exc:
        conn.execute(
            "UPDATE reflow_drafts SET status = 'failed', error_code = ?, error_message = ?,"
            " updated_at = ? WHERE id = ?",
            (exc.code, exc.msg[:500], security.now_iso(), str(row["id"])),
        )
        conn.commit()
        raise
    conn.execute(
        "UPDATE reflow_drafts SET status = 'finalized', dataset_id = ?, finalized_at = ?,"
        " error_code = NULL, error_message = NULL, updated_at = ? WHERE id = ?",
        (str(dataset["id"]), security.now_iso(), security.now_iso(), str(row["id"])),
    )
    conn.commit()
    return {"dataset": dataset, "created": True, "note": "已定稿：数据集目录、清单与 ZIP 已生成。"}


def resplit(
    conn,
    user_id: int,
    project_id: str,
    dataset_id: str,
    *,
    strategy: str = "even",
    ratios: dict | None = None,
    seed: int = 0,
    request_key: str = "",
    session_id: str | None = None,
) -> dict:
    """对已有数据集重新划分：复制原样本与标注到新版本，不改动原目录。"""
    key = str(request_key or "").strip()
    if not key:
        raise ApiError(400, "REQUEST_KEY_REQUIRED", "缺少 request_key（用于幂等）")
    existing = conn.execute(
        "SELECT * FROM datasets WHERE project_id = ? AND owner_id = ? AND request_key = ?",
        (str(project_id), int(user_id), key),
    ).fetchone()
    if existing is not None:
        from backend.services import datasets as dataset_service

        return dataset_service.public(existing)
    parent = _dataset(conn, user_id, project_id, dataset_id)
    prepared = identity_service.prepare_samples(conn, user_id, project_id, str(parent["id"]))
    samples = prepared["samples"]
    if not samples:
        raise ApiError(409, "NO_SAMPLES", "这个数据集没有可重新划分的样本")
    if prepared["problems"]:
        # 重新划分也按身份分组；文件读不到或内容变过时分组依据不可信，先停下。
        raise ApiError(
            409,
            "DATASET_IDENTITY_UNVERIFIED",
            "数据集里有样本的真实文件与记录不一致，先把这些样本重新核验或恢复原文件，再重新划分。",
            details={"problems": prepared["problems"][:5], "count": len(prepared["problems"])},
        )
    items = []
    for sample in samples:
        identity_key = str(sample.get("pixel_sha256") or sample.get("sha256") or sample["id"])
        items.append(
            {
                "record_id": str(sample.get("origin_record_id") or sample["id"]),
                "action": "resplit",
                "identity_key": identity_key,
                "split": None,
                "stem": str(sample["stem"]),
                "boxes": _loads(sample["boxes"], []),
                "version_id": sample.get("review_version_id"),
                "image_width": int(sample["width"] or 0),
                "image_height": int(sample["height"] or 0),
                "sha256": sample.get("sha256"),
                "pixel_sha256": sample.get("pixel_sha256"),
                "target_sample": sample,
            }
        )
    _split_for(str(strategy or "even").lower(), ratios or DEFAULT_RATIOS, int(seed or 0), items)
    plan = {
        "mode": "revision",
        "dataset_id": str(parent["id"]),
        "parent_dataset_id": str(parent["id"]),
        "items": items,
        "kept": [],
        "conflicts": [],
        "classes": [str(item) for item in _loads(parent["classes"], [])],
    }
    root = storage.project_root(conn, user_id, project_id)
    new_id = security.new_id("dst")
    base = storage.project_dir(root, storage.DATASETS, new_id)
    try:
        result = _build(
            conn,
            user_id,
            project_id,
            root=root,
            base=base,
            db_id=new_id,
            plan=plan,
            name=_unique_name(conn, project_id, f"{parent['name']}-重新划分"),
            strategy=str(strategy or "even"),
            ratios=ratios or DEFAULT_RATIOS,
            seed=int(seed or 0),
            request_key=key,
        )
    except Exception:
        try:
            storage.remove_tree(root, base)
        except ApiError:
            pass
        raise
    _notice(conn, project_id, user_id, session_id, new_id, plan)
    return result


__all__ = [
    "DEFAULT_RATIOS",
    "SPLITS",
    "apply",
    "delete_draft",
    "finalize_draft",
    "list_drafts",
    "preview",
    "resplit",
    "restore_draft",
    "save_draft",
]

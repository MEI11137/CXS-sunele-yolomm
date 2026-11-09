"""数据集导入：目录 / ZIP，RGB 或 RGB+X，严格校验后登记样本清单。

支持的合法布局（都是平台自己会导出的）：

* RGB 主图 ``images/`` + 第二模态 ``images_<modality>/``（ir / edge / depth / x）；
* IR 参考主图 ``images/`` + RGB 辅助图 ``images_ir/``（reflow 导出布局；此时
  参考标注在 ``labels/``、辅助标注在 ``labels_ir/``，两套各自对应自己的图片坐标）。

校验清单（每一项都真的读过内容，不是看文件名）：

* data.yaml 的类别表（``names`` / ``nc``）；
* train/val/test 划分目录必须真实存在且非空；
* 每张图都要能被 PIL 真实解码（顺带拿到真实宽高）；
* 标注逐行校验：5 列、class 索引在 ``[0, nc)``、bbox 归一化到 [0,1] 且宽高为正；
* 辅图按**同名 stem** 配对，缺辅图时：``edge`` 允许派生并记录来源，
  其它模态一律拒绝（绝不用 RGB 冒充红外，也不补零图）；
* 辅助标注 ``labels_ir/`` 存在时同样逐行校验，并登记到 samples，参与内容指纹；
* ZIP 解压防路径穿越、防符号链接逃逸、防解压炸弹（条目数与总字节上限）。

外来 ``manifest.json`` 只是数据不是权威：owner / record ID / 跨项目路径一律不信，
只按真实文件与 data.yaml 扫描；类别名不猜，yaml 里没有、调用方也没给就报
``NEED_CLASSES``。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from PIL import Image

from backend import security
from backend.errors import ApiError
from backend.services import assets as asset_service
from backend.services import storage

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
SPLITS = ("train", "val", "test")
MAX_ZIP_ENTRIES = 20000
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_SAMPLES = 20000
SUPPORTED_SECONDARY = {"ir", "edge", "depth", "x", "rgb"}
# IR 参考数据集只允许 RGB 作为辅助模态：这是 reflow 导出/再导入的既有合法布局。
IR_REFERENCE_AUXILIARIES = {"rgb"}
# 主模态：原图就是它。ir 单模态是真实数据集（热红外），不是“缺了 RGB 的错误数据”。
PRIMARY_MODALITIES = {"rgb", "ir"}


class DatasetError(ApiError):
    pass


def public(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    modes = _loads(data["modalities"], [])
    reference = reference_modality(data)
    # 主模态/参考模态以显式 reference_modality 为准：legacy 行 modalities 顺序可能
    # 是 [rgb, ir] 但 reference_modality 已明确为 ir，不能按 0 号位误判成 RGB。
    mode = str(modes[0]).lower() if modes else "rgb"
    primary = reference if reference in PRIMARY_MODALITIES else mode
    return {
        "id": data["id"],
        "project_id": data["project_id"],
        "name": data["name"],
        "modalities": modes,
        # 主模态 = 原图本身是什么。单 IR 数据集这里就是 "ir"，界面据此显示“IR 原图”，
        # 不再把红外图叫成 RGB，也没有第二张凑数的配图。
        "primary_modality": primary,
        # 参考模态：导出/训练真正当作“主模态像素坐标”的那一侧（reflow 显式选择）。
        "reference_modality": reference,
        "secondary_modality": data["secondary_modality"],
        "classes": _loads(data["classes"], []),
        "root_rel": data["root_rel"],
        "source": data["source"],
        "sample_count": int(data["sample_count"]),
        "split_counts": _loads(data["split_counts"], {}),
        "status": data["status"],
        "deleted_at": data.get("deleted_at"),
        "notes": data["notes"],
        "created_at": data["created_at"],
        "parent_dataset_id": data.get("parent_dataset_id"),
        "kind": data.get("kind") or "import",
        "seed": data.get("seed"),
        "split_strategy": data.get("split_strategy") or "source",
        "manifest_rel": data.get("manifest_rel"),
        "export_rel": data.get("export_rel"),
        "export_url": (
            f"/api/projects/{data['project_id']}/datasets/{data['id']}/export"
            if data.get("export_rel")
            else None
        ),
    }


def _loads(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (ValueError, TypeError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def reference_modality(row: sqlite3.Row | dict) -> str:
    """数据集的参考模态：显式 ``reference_modality`` 优先，其次 ``modalities[0]``。

    导出/训练/发布/复核都以参考模态像素为准；legacy 行的 modalities 顺序可能与
    显式参考列不一致（例如 reference=ir 但 modalities=[rgb,ir]），此时一律信列。
    """
    data = dict(row)
    declared = str(data.get("reference_modality") or "").strip().lower()
    if declared in {"rgb", "ir", "edge", "depth"}:
        return declared
    modes = _loads(data.get("modalities"), [])
    first = str(modes[0]).lower() if modes else "rgb"
    return first if first in {"rgb", "ir", "edge", "depth"} else "rgb"


def get_owned(conn, user_id: int, project_id: str, dataset_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM datasets WHERE id = ? AND project_id = ? AND owner_id = ? AND deleted_at IS NULL",
        (str(dataset_id), str(project_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise ApiError(404, "DATASET_NOT_FOUND", "数据集不存在、已删除或不属于当前项目")
    return row


def list_for_project(
    conn, user_id: int, project_id: str, *, limit: int = 50, deleted: str = "exclude"
) -> dict:
    mode = str(deleted or "exclude").lower()
    clause = "deleted_at IS NULL" if mode != "only" else "deleted_at IS NOT NULL"
    rows = conn.execute(
        f"SELECT * FROM datasets WHERE project_id = ? AND owner_id = ? AND {clause} "
        "ORDER BY created_at DESC LIMIT ?",
        (str(project_id), int(user_id), max(1, min(int(limit), 200))),
    ).fetchall()
    return {"datasets": [public(row) for row in rows]}


def soft_delete(conn, user_id: int, project_id: str, dataset_id: str) -> dict:
    # 重复删除要给出“已在回收站”，不是 404：Agent 重试同一动作时不能把
    # “已经删过”误报成“这个数据集不存在”。找回记录时连同回收站一起查。
    row = conn.execute(
        "SELECT * FROM datasets WHERE id = ? AND project_id = ? AND owner_id = ?",
        (str(dataset_id), str(project_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise ApiError(404, "DATASET_NOT_FOUND", "数据集不存在或不属于当前项目")
    if row["deleted_at"]:
        return {"dataset_id": str(dataset_id), "status": "unchanged"}
    conn.execute(
        "UPDATE datasets SET deleted_at = ? WHERE id = ? AND project_id = ? AND owner_id = ?"
        " AND deleted_at IS NULL",
        (security.now_iso(), str(dataset_id), str(project_id), int(user_id)),
    )
    conn.commit()
    return {"dataset_id": str(dataset_id), "status": "deleted", "note": "只移入回收站；目录与文件保留。"}


def restore(conn, user_id: int, project_id: str, dataset_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM datasets WHERE id = ? AND project_id = ? AND owner_id = ?",
        (str(dataset_id), str(project_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise ApiError(404, "DATASET_NOT_FOUND", "数据集不存在或不属于当前项目")
    if row["deleted_at"] is not None:
        # 软删期间可能已重新导入过同一内容：恢复会与活跃版本撞唯一索引。
        # 提前查清并给出明确 409，原记录继续留在回收站，文件与历史不动。
        # 指纹口径有三类：库里存的是 v2、库里存的是旧 v1、或为空；恢复侧
        # 必须三种都能与活跃记录对上，不能只在指纹为空时重算。
        active = _restore_conflict(conn, user_id, project_id, dataset_id, row)
        if active is not None:
            raise ApiError(
                    409,
                    "DATASET_RESTORE_CONFLICT",
                    "回收站里这份与正在使用的数据集内容相同，不能同时恢复两份；"
                    "如需换回旧版本，请先删除当前使用的那份。",
                    details={
                        "conflict_with": {
                            "id": str(active["id"]),
                            "name": str(active["name"]),
                            "deleted_at": None,
                        },
                        "kept_in_trash": {"id": str(dataset_id), "name": str(row["name"])},
                    },
                )
    try:
        conn.execute(
            "UPDATE datasets SET deleted_at = NULL WHERE id = ? AND project_id = ? AND owner_id = ?",
            (str(dataset_id), str(project_id), int(user_id)),
        )
    except sqlite3.IntegrityError:
        # 并发兜底：检查与 UPDATE 之间有人恢复了同内容版本。
        conn.rollback()
        active = _restore_conflict(conn, user_id, project_id, dataset_id, row)
        raise ApiError(
            409,
            "DATASET_RESTORE_CONFLICT",
            "恢复时检测到另一份相同内容已在用（并发），本次恢复未写入；"
            f"当前可用：{active['name'] if active is not None else '见列表'}",
            details={"conflict_with": {"id": str(active["id"])} if active is not None else None},
        ) from None
    conn.commit()
    return {"dataset_id": str(dataset_id), "status": "restored"}


def samples(conn, user_id: int, project_id: str, dataset_id: str, *, split: str | None = None, limit: int = 200) -> dict:
    get_owned(conn, user_id, project_id, dataset_id)
    clauses = ["dataset_id = ?"]
    params: list[Any] = [str(dataset_id)]
    if split:
        clauses.append("split = ?")
        params.append(str(split))
    rows = conn.execute(
        f"SELECT * FROM dataset_samples WHERE {' AND '.join(clauses)} ORDER BY split, stem LIMIT ?",
        (*params, max(1, min(int(limit), 2000))),
    ).fetchall()
    return {
        "samples": [
            {
                "id": row["id"],
                "split": row["split"],
                "stem": row["stem"],
                "rgb_rel": row["rgb_rel"],
                "x_rel": row["x_rel"],
                "label_rel": row["label_rel"],
                "labels_ir_rel": row["labels_ir_rel"] if "labels_ir_rel" in row.keys() else None,
                "width": row["width"],
                "height": row["height"],
                "boxes": _loads(row["boxes"], []),
                "sha256": row["sha256"],
                "pixel_sha256": row["pixel_sha256"],
                "origin_sample_key": row["origin_sample_key"],
                "origin_record_id": row["origin_record_id"],
                "review_version_id": row["review_version_id"],
            }
            for row in rows
        ]
    }


# --------------------------------------------------------------------------- 导入



# --------------------------------------------------------------------------- 内容指纹


def _x_digest(root: Path, rel: str | None) -> str:
    """X 模态图片按**字节**取摘要；读不到就明确记 missing，不当作相同。"""
    if not rel:
        return ""
    try:
        return hashlib.sha256(storage.resolve_rel(root, rel).read_bytes()).hexdigest()
    except (ApiError, OSError):
        return "missing"


def _label_digest(root: Path, rel: str | None) -> str:
    """辅助标注按**字节**取摘要；读不到同样记 missing，不当作相同。"""
    if not rel:
        return ""
    try:
        return hashlib.sha256(storage.resolve_rel(root, rel).read_bytes()).hexdigest()
    except (ApiError, OSError):
        return "missing"


def _fingerprint_lines(modes: list[str], secondary: str | None, classes: list[str], rows: list[str]) -> str:
    """v2：头部用确定性 JSON 结构编码，类别名里的 ``|``/换行等不再可能碰撞。

    ``json.dumps`` + ``sort_keys`` + 紧凑分隔符对同数据是稳定的；``classes``
    保持原顺序（类别表顺序有语义，不做排序）。
    """
    head = json.dumps(
        {
            "v": 2,
            "modes": sorted(modes),
            "secondary": secondary or "",
            "classes": list(classes),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    # 排序后再哈希：压缩包里的文件顺序、时间戳、显示名称都不参与。
    payload = (head + "\n" + "\n".join(sorted(rows))).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _v1_fingerprint_lines(modes: list[str], secondary: str | None, classes: list[str], rows: list[str]) -> str:
    """v1 兼容算法：仅用于把旧库里的 v1 指纹对上（不能改历史，也不能丢复用）。"""
    head = [
        "v1",
        "modes=" + ",".join(sorted(modes)),
        "secondary=" + (secondary or ""),
        "classes=" + "|".join(classes),
    ]
    return hashlib.sha256("\n".join(head + sorted(rows)).encode("utf-8")).hexdigest()


def _scan_rows(scanned: dict, root: Path) -> list[str]:
    """样本行（不含类别/模态头部）：划分 + 内容哈希 + X 字节摘要 + 标注。

    存在辅助标注时，每行再追加 ``labels_ir`` 的字节摘要：只改了辅助标签的
    数据集不能被去重复用（旧数据集没有该列，行格式保持不变，指纹继续兼容）。
    """
    rows: list[str] = []
    with_aux_labels = any(item.get("labels_ir_rel") for item in scanned["samples"])
    for item in scanned["samples"]:
        fields = [
            str(item["split"]),
            str(item.get("sha256") or ""),
            str(item.get("pixel_sha256") or ""),
            _x_digest(root, item.get("x_rel")),
            json.dumps(item.get("boxes") or [], ensure_ascii=False),
            json.dumps(item.get("class_ids") or [], ensure_ascii=False),
        ]
        if with_aux_labels:
            fields.append(_label_digest(root, item.get("labels_ir_rel")))
        rows.append("\t".join(fields))
    return rows


def _content_fingerprint(scanned: dict, modes: list[str], secondary: str | None, root: Path) -> str:
    return _content_fingerprint_pair(scanned, modes, secondary, root)[0]


def _content_fingerprint_pair(
    scanned: dict, modes: list[str], secondary: str | None, root: Path
) -> tuple[str, str]:
    """返回 (v2, v1)。v1 只用于把库里旧规则算出的指纹对上，不再写入新记录。"""
    rows = _scan_rows(scanned, root)
    return (
        _fingerprint_lines(modes, secondary, scanned["classes"], rows),
        _v1_fingerprint_lines(modes, secondary, scanned["classes"], rows),
    )


def _stored_fingerprint_pair(conn, row, root: Path) -> tuple[str | None, str | None]:
    """按已落库样本现场重算 (v2, v1)；X 图或辅助标注读不到就放弃比较，不猜。"""
    samples = conn.execute(
        "SELECT * FROM dataset_samples WHERE dataset_id = ?", (str(row["id"]),)
    ).fetchall()
    if not samples:
        return None, None
    has_aux_column = "labels_ir_rel" in samples[0].keys()
    with_aux_labels = has_aux_column and any(sample["labels_ir_rel"] for sample in samples)
    rows: list[str] = []
    for sample in samples:
        x_sha = _x_digest(root, sample["x_rel"])
        if x_sha == "missing":
            return None, None
        fields = [
            str(sample["split"]),
            str(sample["sha256"] or ""),
            str(sample["pixel_sha256"] or ""),
            x_sha,
            json.dumps(json.loads(sample["boxes"] or "[]"), ensure_ascii=False),
            json.dumps(json.loads(sample["class_ids"] or "[]"), ensure_ascii=False),
        ]
        if with_aux_labels:
            label_sha = _label_digest(root, sample["labels_ir_rel"])
            if label_sha == "missing":
                return None, None
            fields.append(label_sha)
        rows.append("\t".join(fields))
    modes = json.loads(row["modalities"] or "[]")
    secondary = row["secondary_modality"]
    classes = json.loads(row["classes"] or "[]")
    return (
        _fingerprint_lines(modes, secondary, classes, rows),
        _v1_fingerprint_lines(modes, secondary, classes, rows),
    )


def _legacy_fingerprint(conn, row, root: Path) -> str | None:
    """旧记录（无指纹）用已落库的样本重算（v2 口径）。"""
    return _stored_fingerprint_pair(conn, row, root)[0]


def _restore_conflict(conn, user_id: int, project_id: str, dataset_id: str, row):
    """恢复前的冲突检查：回收站记录与活跃记录按内容对上就返回那个活跃行。

    口径覆盖三类：存 v2、存旧 v1、为空。存了旧 v1 的记录直接比对会漏掉
    活跃侧的 v2（反过来也一样），所以同时用样本现场重算 v2/v1 两条路查。
    v1 算法 ``'|'.join`` 会碰撞，仅凭 v1 命中还须类别表逐项相等才算冲突。
    """
    root = storage.project_root(conn, user_id, project_id)
    stored = str(row["content_fingerprint"] or "")
    recomputed_v2, recomputed_v1 = _stored_fingerprint_pair(conn, row, root)
    v1_values = {value for value in (stored, recomputed_v1) if value and value != recomputed_v2}
    # 直接口径（v2 或库里存的值）：算法本身无碰撞，命中即冲突。
    for value in {stored, recomputed_v2}:
        if not value:
            continue
        active = conn.execute(
            "SELECT * FROM datasets WHERE project_id = ? AND owner_id = ?"
            " AND content_fingerprint = ? AND deleted_at IS NULL AND id != ?",
            (str(project_id), int(user_id), value, str(dataset_id)),
        ).fetchone()
        if active is not None:
            return active
    # v1 口径：类别表逐项相等才认（同指纹不同类别是 v1 碰撞，不算冲突）。
    own_classes = json.loads(row["classes"] or "[]")
    for value in v1_values:
        if not value:
            continue
        actives = conn.execute(
            "SELECT * FROM datasets WHERE project_id = ? AND owner_id = ?"
            " AND content_fingerprint = ? AND deleted_at IS NULL AND id != ?",
            (str(project_id), int(user_id), value, str(dataset_id)),
        ).fetchall()
        for active in actives:
            if json.loads(active["classes"] or "[]") == own_classes:
                return active
    return None


def _find_duplicate(
    conn,
    user_id: int,
    project_id: str,
    fingerprint: str,
    scanned: dict,
    v1_fingerprint: str | None = None,
):
    """同项目/同所有者里已有相同内容就返回它；回收站不参与，绝不复活。

    ``fingerprint`` 是新规则（v2）的值。库里可能还有两类旧记录：
    - ``content_fingerprint IS NULL``：现场重算（v2）比对后顺手补齐；
    - 存的是 v1 规则的指纹：用导入侧的 v1 值对上，再核对类别表逐项相等
      （v1 的 ``'|'.join`` 会碰撞，不能只信指纹本身），对上后升级为 v2。
    """
    row = conn.execute(
        "SELECT * FROM datasets WHERE project_id = ? AND owner_id = ?"
        " AND content_fingerprint = ? AND deleted_at IS NULL",
        (str(project_id), int(user_id), fingerprint),
    ).fetchone()
    if row is not None:
        return row
    root = storage.project_root(conn, user_id, project_id)
    legacy = conn.execute(
        "SELECT * FROM datasets WHERE project_id = ? AND owner_id = ?"
        " AND content_fingerprint IS NULL AND deleted_at IS NULL AND sample_count = ?",
        (str(project_id), int(user_id), len(scanned["samples"])),
    ).fetchall()
    for item in legacy:
        if json.loads(item["split_counts"] or "{}") != scanned["split_counts"]:
            continue
        digest = _legacy_fingerprint(conn, item, root)
        if digest is None or digest != fingerprint:
            continue
        # 顺手补齐：下次就不用再重算旧记录。
        conn.execute(
            "UPDATE datasets SET content_fingerprint = ? WHERE id = ?", (digest, str(item["id"]))
        )
        conn.commit()
        return conn.execute("SELECT * FROM datasets WHERE id = ?", (str(item["id"]),)).fetchone()
    v1_fingerprint = v1_fingerprint or ""
    if v1_fingerprint:
        old_rule = conn.execute(
            "SELECT * FROM datasets WHERE project_id = ? AND owner_id = ?"
            " AND content_fingerprint = ? AND deleted_at IS NULL",
            (str(project_id), int(user_id), v1_fingerprint),
        ).fetchall()
        for item in old_rule:
            if json.loads(item["classes"] or "[]") != list(scanned["classes"]):
                # v1 头部用 '|' 拼接，['a|b','c'] 与 ['a','b|c'] 会撞出同一个指纹；
                # 类别表逐项不等就不是同一份数据，绝不能复用。
                continue
            conn.execute(
                "UPDATE datasets SET content_fingerprint = ? WHERE id = ?",
                (fingerprint, str(item["id"])),
            )
            conn.commit()
            return conn.execute("SELECT * FROM datasets WHERE id = ?", (str(item["id"]),)).fetchone()
    return None


def _resolve_modes(
    modes: list[str],
    declared_reference: str | None,
    secondary_modality: str | None,
) -> tuple[list[str], str, str | None]:
    """校验并归一化主/辅模态；返回 (落库 modalities, primary, secondary)。"""
    modes = [str(item).strip().lower() for item in (modes or []) if str(item).strip()]
    deduped: list[str] = []
    for item in modes:
        if item not in deduped:
            deduped.append(item)
    modes = deduped
    if declared_reference is not None:
        if declared_reference not in modes:
            raise ApiError(400, "VALIDATION", "参考模态必须在 modalities 中声明")
        # 显式参考模态就是主模态：legacy modalities 顺序（如 [rgb, ir]）不改变像素口径。
        modes = [declared_reference] + [item for item in modes if item != declared_reference]
    primary = declared_reference or (modes[0] if modes else "rgb")
    if primary not in PRIMARY_MODALITIES:
        raise ApiError(
            400,
            "VALIDATION",
            f"主模态只能是 {'/'.join(sorted(PRIMARY_MODALITIES))}；当前是 {primary}",
        )
    secondary = str(secondary_modality or "").strip().lower() or None
    if secondary is not None and secondary not in SUPPORTED_SECONDARY:
        raise ApiError(400, "VALIDATION", f"第二模态只支持 {'/'.join(sorted(SUPPORTED_SECONDARY))}")
    if secondary is None and len(modes) > 1:
        secondary = modes[1]
    if secondary == primary:
        raise ApiError(400, "VALIDATION", "第二模态不能和主模态相同")
    if primary == "ir":
        # IR 参考 + RGB 辅助是 reflow 导出的既有合法布局；其它组合仍只走 RGB 主模态。
        if secondary is not None and secondary not in IR_REFERENCE_AUXILIARIES:
            raise ApiError(
                400,
                "VALIDATION",
                "IR 参考数据集只支持 RGB 作为辅助模态；其它模态请把 RGB 放在 modalities 第一位",
            )
    if secondary is not None and secondary not in modes:
        modes.append(secondary)
    # 落库的 modalities 只保留“主模态 + 辅助模态”两项，不额外扩未知多模态。
    return [primary] + ([secondary] if secondary else []), primary, secondary


def _infer_modalities(raw: Path) -> list[str]:
    """未显式给 modalities 时，按压缩包内 data.yaml 的 modalities 声明推断顺序。

    只读取声明键，不做像素级探测；data.yaml 缺失/无 modalities 时交给调用方回退 rgb。
    """
    yaml_path = _find_yaml(raw)
    if yaml_path is None:
        return []
    try:
        loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(loaded, dict):
        return []
    declared = loaded.get("modalities")
    if not isinstance(declared, dict):
        return []
    known = set(PRIMARY_MODALITIES) | set(SUPPORTED_SECONDARY)
    return [str(key).strip().lower() for key in declared if str(key).strip().lower() in known]


def _preview_yaml(loaded: dict) -> tuple[list[str], list[str]]:
    """从 data.yaml 里取类别与模态声明；不猜类别、不改写内容。"""
    names = loaded.get("names")
    classes: list[str] = []
    if isinstance(names, dict):
        classes = [str(names[key]).strip() for key in sorted(names, key=lambda item: int(item))]
    elif isinstance(names, list):
        classes = [str(item).strip() for item in names]
    modalities = loaded.get("modalities")
    declared = [str(key).strip().lower() for key in modalities] if isinstance(modalities, dict) else []
    known = set(PRIMARY_MODALITIES) | set(SUPPORTED_SECONDARY)
    return [item for item in classes if item], [item for item in declared if item in known]


def _preview_split_paths(loaded: dict) -> dict[str, list[str]]:
    """data.yaml 的 train/val/test 可能是字符串或字符串数组。"""
    found: dict[str, list[str]] = {}
    for split in SPLITS:
        raw = loaded.get(split)
        values: list[str] = []
        if isinstance(raw, str) and raw.strip():
            values = [raw.strip()]
        elif isinstance(raw, list):
            values = [str(item).strip() for item in raw if str(item).strip()]
        found[split] = values
    return found


def _preview_count_zip(zf: zipfile.ZipFile, split_paths: dict[str, list[str]]) -> dict[str, int]:
    names = [name.replace("\\", "/").lstrip("./") for name in zf.namelist()]
    counts: dict[str, int] = {}
    for split, paths in split_paths.items():
        total = 0
        for path in paths:
            stem = path.strip("./").rstrip("/")
            for name in names:
                if not name.lower().endswith(tuple(IMAGE_SUFFIXES)):
                    continue
                # data.yaml 可能位于压缩包内一层子目录，按“路径片段”匹配更稳。
                if name == stem or name.startswith(stem + "/") or ("/" + stem + "/") in ("/" + name):
                    total += 1
        counts[split] = total
    return counts


def _preview_count_dir(base: Path, split_paths: dict[str, list[str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for split, paths in split_paths.items():
        total = 0
        for path in paths:
            candidate = (base / path).resolve()
            try:
                candidate.relative_to(base.resolve())
            except ValueError:
                continue
            if candidate.is_file() and candidate.suffix.lower() in IMAGE_SUFFIXES:
                total += 1
            elif candidate.is_dir():
                total += sum(
                    1 for item in candidate.rglob("*") if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES
                )
        counts[split] = total
    return counts


def preview_source(
    conn,
    user_id: int,
    project_id: str,
    *,
    asset_id: str | None = None,
    rel_path: str | None = None,
) -> dict:
    """只读预览 ZIP/目录：返回建议名称、类别、模态与各划分图片数；不创建数据集。"""
    root = storage.project_root(conn, user_id, project_id)
    source = "zip" if asset_id else "local"
    if asset_id:
        asset = asset_service.get_owned(conn, user_id, project_id, asset_id)
        if str(asset["kind"]) != "archive":
            raise ApiError(409, "NOT_AN_ARCHIVE", "只能预览 ZIP 压缩包")
        path = asset_service.path_of(conn, user_id, project_id, asset_id)
        suggested = str(asset["filename"] or Path(path).stem)
        if suggested.lower().endswith(".zip"):
            suggested = suggested[:-4]
        with zipfile.ZipFile(path) as zf:
            yaml_name = _find_yaml_in_zip(zf)
            loaded = _load_zip_yaml(zf, yaml_name) if yaml_name else {}
            classes, declared = _preview_yaml(loaded)
            split_paths = _preview_split_paths(loaded)
            split_counts = _preview_count_zip(zf, split_paths) if split_paths else {}
    elif rel_path:
        source_dir = storage.resolve_rel(root, rel_path)
        if not source_dir.is_dir():
            raise ApiError(409, "NOT_A_DIRECTORY", "目录预览需要一个真实存在的目录")
        suggested = source_dir.name
        yaml_path = _find_yaml(source_dir)
        loaded = {}
        if yaml_path is not None:
            try:
                value = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                loaded = value if isinstance(value, dict) else {}
            except (OSError, yaml.YAMLError):
                loaded = {}
        classes, declared = _preview_yaml(loaded)
        split_paths = _preview_split_paths(loaded)
        split_counts = _preview_count_dir(source_dir, split_paths) if split_paths else {}
    else:
        raise ApiError(400, "SOURCE_REQUIRED", "需要提供 ZIP 资产或项目内目录")

    if not declared:
        # data.yaml 没声明模态时按目录名给一个保守提示，不写库也不改用户文件。
        if asset_id:
            declared = ["rgb", "ir"] if any("images_ir/" in name for name in zipfile.ZipFile(path).namelist()) else ["rgb"]
        else:
            declared = ["rgb", "ir"] if (source_dir / "images_ir").is_dir() else ["rgb"]
    try:
        modes, primary, secondary = _resolve_modes(declared, None, None)
    except ApiError:
        modes, primary, secondary = ["rgb"], "rgb", None
    has_yaml = bool(loaded) if isinstance(loaded, dict) else False
    total = sum(split_counts.values()) if split_counts else 0
    return {
        "source": source,
        "suggested_name": suggested[:80],
        "has_yaml": has_yaml,
        "modalities": modes,
        "primary_modality": primary,
        "secondary_modality": secondary,
        "classes": classes,
        "class_count": len(classes),
        "split_counts": split_counts,
        "sample_count": total,
        "warnings": [] if has_yaml else ["没有识别到 data.yaml，类别/划分/模态需要人工补充"],
    }


def _find_yaml_in_zip(zf: zipfile.ZipFile) -> str | None:
    for name in zf.namelist():
        base = PurePosixPath(name).name.lower()
        if base in {"data.yaml", "data.yml", "dataset.yaml", "dataset.yml"} and name.count("/") <= 1:
            return name
    return None


def _load_zip_yaml(zf: zipfile.ZipFile, name: str) -> dict:
    try:
        value = yaml.safe_load(zf.read(name).decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    return value if isinstance(value, dict) else {}


def import_dataset(
    conn,
    user_id: int,
    project_id: str,
    *,
    name: str,
    modalities: list[str] | None,
    secondary_modality: str | None = None,
    reference_modality: str | None = None,
    classes: list[str] | None = None,
    asset_id: str | None = None,
    rel_path: str | None = None,
) -> dict:
    label = str(name or "").strip()
    if not label:
        raise ApiError(400, "VALIDATION", "数据集名称不能为空")
    declared_reference = str(reference_modality or "").strip().lower() or None
    if declared_reference is not None and declared_reference not in PRIMARY_MODALITIES:
        raise ApiError(
            400,
            "VALIDATION",
            f"参考模态只能是 {'/'.join(sorted(PRIMARY_MODALITIES))}；当前是 {declared_reference}",
        )
    requested_modes = [str(item) for item in (modalities or []) if str(item).strip()]
    requested_secondary = str(secondary_modality or "").strip().lower() or None

    root = storage.project_root(conn, user_id, project_id)
    dataset_id = security.new_id("dst")
    # 目录与 raw/ 都由 storage 统一创建并校验真实落点：dataset 目录若被换成
    # 指向项目外的联接，这里会在写入任何字节之前就拒绝。
    base = storage.project_dir(root, storage.DATASETS, dataset_id)
    raw = storage.project_dir(root, storage.DATASETS, dataset_id, "raw")
    try:
        if asset_id:
            asset = asset_service.get_owned(conn, user_id, project_id, asset_id)
            if str(asset["kind"]) != "archive":
                raise ApiError(409, "NOT_AN_ARCHIVE", "只能从 ZIP 压缩包导入数据集")
            source_path = asset_service.path_of(conn, user_id, project_id, asset_id)
            _extract_zip(root, source_path, raw)
            source = "upload"
            source_asset_id = str(asset_id)
        elif rel_path:
            source_dir = storage.resolve_rel(root, rel_path)
            if not source_dir.is_dir():
                raise ApiError(409, "NOT_A_DIRECTORY", "目录导入需要一个真实存在的目录")
            _copy_tree(root, source_dir, raw)
            source = "local"
            source_asset_id = None
        else:
            raise ApiError(400, "SOURCE_REQUIRED", "需要提供 ZIP 资产或项目内目录")

        if not requested_modes:
            # 前端“自动识别”路径：按 data.yaml 声明推断主/辅模态，避免手填成正模态。
            requested_modes = _infer_modalities(raw) or ["rgb"]
        modes, primary, secondary = _resolve_modes(
            requested_modes, declared_reference, requested_secondary
        )

        scanned = _scan(raw, primary=primary, secondary=secondary, classes=classes, project_root=root)
        scanned["reference"] = primary
        fingerprint, v1_fingerprint = _content_fingerprint_pair(scanned, modes, secondary, root)
        duplicate = _find_duplicate(
            conn, user_id, project_id, fingerprint, scanned, v1_fingerprint=v1_fingerprint
        )
        if duplicate is not None:
            # 本次复制出来的副本只是临时产物：只清理自己刚建的目录。
            storage.remove_tree(root, base)
            return {**public(duplicate), "reused": True}
        _write_yaml(root, base, scanned)
        _insert_dataset(
            conn,
            dataset_id=dataset_id,
            user_id=user_id,
            project_id=project_id,
            name=label,
            modalities=modes,
            secondary=secondary,
            reference=primary,
            scanned=scanned,
            root_rel=storage.relative(root, base),
            source=source,
            source_asset_id=source_asset_id,
            content_fingerprint=fingerprint,
        )
    except sqlite3.IntegrityError:
        # 并发：两请求同时通过查重，唯一索引只放一个进来；输的那方返回已存在的那份。
        try:
            storage.remove_tree(root, base)
        except ApiError:
            pass
        existing = conn.execute(
            "SELECT * FROM datasets WHERE project_id = ? AND owner_id = ?"
            " AND content_fingerprint = ? AND deleted_at IS NULL",
            (str(project_id), int(user_id), fingerprint),
        ).fetchone()
        if existing is None:
            raise
        return {**public(existing), "reused": True}
    except Exception:
        # 清理同样要过守卫：目标真实路径可能已经不在项目内，绝不递归删外面。
        try:
            storage.remove_tree(root, base)
        except ApiError:
            pass
        raise
    return public(get_owned(conn, user_id, project_id, dataset_id))


def _insert_dataset(
    conn,
    *,
    dataset_id: str,
    user_id: int,
    project_id: str,
    name: str,
    modalities: list[str],
    secondary: str | None,
    reference: str | None = None,
    scanned: dict,
    root_rel: str,
    source: str,
    source_asset_id: str | None,
    content_fingerprint: str | None = None,
) -> None:
    now = security.now_iso()
    # reference_modality 是“像素坐标以哪一侧为准”，与 modalities 顺序解耦；
    # 训练/导出/复核都读这一列，不能只依赖 modalities[0]。
    inferred_reference = reference or (modalities[0] if modalities else "rgb")
    conn.execute(
        """
        INSERT INTO datasets
            (id, project_id, owner_id, name, modalities, secondary_modality, reference_modality,
             classes, root_rel, source, source_asset_id, sample_count, split_counts, status, notes,
             created_at, content_fingerprint)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?)
        """,
        (
            dataset_id,
            str(project_id),
            int(user_id),
            name,
            json.dumps(modalities, ensure_ascii=False),
            secondary,
            inferred_reference,
            json.dumps(scanned["classes"], ensure_ascii=False),
            root_rel,
            source,
            source_asset_id,
            len(scanned["samples"]),
            json.dumps(scanned["split_counts"], ensure_ascii=False),
            scanned["notes"],
            now,
            content_fingerprint,
        ),
    )
    conn.executemany(
        """
        INSERT INTO dataset_samples
            (id, dataset_id, split, stem, rgb_rel, x_rel, label_rel, labels_ir_rel,
             width, height, boxes, class_ids, created_at, sha256, pixel_sha256, origin_sample_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                security.new_id("smp"),
                dataset_id,
                item["split"],
                item["stem"],
                item["rgb_rel"],
                item["x_rel"],
                item["label_rel"],
                item.get("labels_ir_rel"),
                item["width"],
                item["height"],
                json.dumps(item["boxes"], ensure_ascii=False),
                json.dumps(item["class_ids"], ensure_ascii=False),
                now,
                item.get("sha256"),
                item.get("pixel_sha256"),
                item.get("origin_sample_key"),
            )
            for item in scanned["samples"]
        ],
    )
    conn.commit()


# --------------------------------------------------------------------------- 解包


def _extract_zip(root: Path, source: Path, target: Path) -> None:
    total = 0
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile:
        raise DatasetError(409, "BAD_ZIP", "压缩包已损坏，无法解压") from None
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            raise DatasetError(413, "ZIP_TOO_MANY_ENTRIES", f"压缩包条目超过 {MAX_ZIP_ENTRIES}")
        for entry in entries:
            name = entry.filename.replace("\\", "/")
            if entry.is_dir():
                continue
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts or ":" in pure.parts[0]:
                raise DatasetError(400, "ZIP_PATH_TRAVERSAL", f"压缩包内含非法路径：{name}")
            if entry.external_attr >> 16 & 0o170000 == 0o120000:
                raise DatasetError(400, "ZIP_SYMLINK", f"压缩包内含符号链接：{name}")
            total += int(entry.file_size)
            if total > MAX_TOTAL_BYTES:
                raise DatasetError(413, "ZIP_TOO_LARGE", "解压后体积超过上限")
            # 落点先过真实路径校验，再解压；不是解完再判断。
            destination = storage.guard(root, target / pure.as_posix())
            with archive.open(entry) as reader:
                storage.write_stream(root, destination, reader)


def _copy_tree(root: Path, source: Path, target: Path) -> None:
    total = 0
    count = 0
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise DatasetError(400, "SYMLINK_REFUSED", f"目录内含符号链接：{path.name}")
        if not path.is_file():
            continue
        count += 1
        total += path.stat().st_size
        if count > MAX_ZIP_ENTRIES or total > MAX_TOTAL_BYTES:
            raise DatasetError(413, "DATASET_TOO_LARGE", "目录内文件过多或体积超过上限")
        storage.copy_into(root, path, target / path.relative_to(source))


# --------------------------------------------------------------------------- 校验


def _find_yaml(raw: Path) -> Path | None:
    for candidate in (raw / "data.yaml", raw / "data.yml"):
        if candidate.is_file():
            return candidate
    for candidate in sorted(raw.glob("*/data.y*ml")):
        if candidate.is_file():
            return candidate
    return None


def _scan(
    raw: Path,
    *,
    primary: str = "rgb",
    secondary: str | None,
    classes: list[str] | None,
    project_root: Path,
) -> dict:
    yaml_path = _find_yaml(raw)
    base = yaml_path.parent if yaml_path is not None else raw
    config: dict = {}
    if yaml_path is not None:
        try:
            loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise DatasetError(409, "BAD_YAML", f"data.yaml 无法解析：{type(exc).__name__}") from exc
        if not isinstance(loaded, dict):
            raise DatasetError(409, "BAD_YAML", "data.yaml 必须是键值结构")
        config = loaded

    names = _class_names(config, classes)
    modalities = config.get("modalities") if isinstance(config.get("modalities"), dict) else {}
    # 辅图目录：以 data.yaml 的 modalities 声明为准；reflow 导出 IR 参考时写
    # {"ir": "images", "rgb": "images_ir"}，所以 RGB 辅助图在 images_ir/。
    if secondary:
        if modalities.get(secondary):
            x_rel = str(modalities[secondary])
        elif secondary == "edge":
            x_rel = "images_edge"
        elif secondary == "rgb":
            x_rel = "images_ir" if primary != "rgb" else "images_rgb"
        else:
            x_rel = f"images_{secondary}"
    else:
        x_rel = None

    split_dirs = _split_dirs(base, config)
    if not split_dirs:
        raise DatasetError(409, "NO_SPLITS", "没有找到 train/val/test 中的任何图片目录")

    samples: list[dict] = []
    split_counts: dict[str, int] = {}
    notes: list[str] = []
    background = 0
    derived_edge = 0
    for split, directory in split_dirs.items():
        images = [p for p in sorted(directory.iterdir()) if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES]
        if not images:
            raise DatasetError(409, "EMPTY_SPLIT", f"{split} 划分里没有图片")
        split_counts[split] = len(images)
        for image_path in images:
            stem = image_path.stem
            width, height = _image_size(image_path)
            label_path = base / "labels" / split / f"{stem}.txt"
            boxes, class_ids = _read_label(label_path, len(names))
            labels_ir_rel: str | None = None
            if secondary:
                aux_label = base / "labels_ir" / split / f"{stem}.txt"
                if aux_label.is_file():
                    # 辅助标注对应辅图自己的像素坐标，逐行同样校验；它不参与主标注框。
                    _read_label(aux_label, len(names))
                    labels_ir_rel = storage.relative(project_root, aux_label)
            # 身份按真实字节 + 解码像素登记；同名/大小/时间都不参与判定。
            from backend.services import identity as identity_service

            identity = identity_service.identity_of_bytes(image_path.read_bytes())
            if not boxes:
                background += 1
            x_path: Path | None = None
            if secondary:
                candidate = base / str(x_rel) / split / image_path.name
                if candidate.is_file():
                    # 配图要真读一遍：文件名存在但内容坏掉的 IR/其它 X 不是有效配对。
                    # 尺寸单独解码，不把主图尺寸当配图元数据（两者可以合法不同）。
                    _image_size(candidate)
                    x_path = candidate
                elif secondary == "edge":
                    x_path = _derive_edge(image_path, project_root, base, split, stem)
                    derived_edge += 1
                else:
                    raise DatasetError(
                        409,
                        "MISSING_MODALITY",
                        f"{split}/{image_path.name} 缺少 {secondary} 配图；"
                        + (
                            "IR 参考数据集必须成对提供 RGB 辅助图"
                            if secondary == "rgb"
                            else "非 edge 模态不允许用 RGB 或零图顶替"
                        ),
                    )
            samples.append(
                {
                    "split": split,
                    "stem": stem,
                    "rgb_rel": storage.relative(project_root, image_path),
                    "x_rel": storage.relative(project_root, x_path) if x_path else None,
                    "label_rel": storage.relative(project_root, label_path) if label_path.is_file() else None,
                    "labels_ir_rel": labels_ir_rel,
                    "width": width,
                    "height": height,
                    "boxes": boxes,
                    "class_ids": class_ids,
                    "sha256": identity["sha256"],
                    "pixel_sha256": identity["pixel_sha256"],
                    "origin_sample_key": f"{split}/{image_path.name}",
                }
            )
            if len(samples) > MAX_SAMPLES:
                raise DatasetError(413, "TOO_MANY_SAMPLES", f"样本数超过 {MAX_SAMPLES}")

    if not samples:
        raise DatasetError(409, "NO_SAMPLES", "没有可用样本")
    if background == len(samples):
        notes.append("所有图片都没有标注框（按背景图处理），请确认是否导错目录")
    if derived_edge:
        notes.append(f"{derived_edge} 张 X 图为 edge 派生（来源已记录，不是真实传感器数据）")
    auxiliary_labels = sum(1 for item in samples if item.get("labels_ir_rel"))
    if auxiliary_labels:
        notes.append(f"{auxiliary_labels} 张辅助标注已登记（labels_ir/，对应辅图坐标）")
    notes.append(f"配对: {primary}={len(samples)}" + (f", x={sum(1 for s in samples if s['x_rel'])}" if secondary else ""))
    return {
        "classes": names,
        "samples": samples,
        "split_counts": split_counts,
        "notes": "；".join(notes),
        "primary": primary,
        "secondary": secondary,
    }


def _class_names(config: dict, override: list[str] | None) -> list[str]:
    if override:
        return [str(item) for item in override]
    names = config.get("names")
    if isinstance(names, dict):
        ordered = [str(names[key]) for key in sorted(names, key=lambda k: int(k))]
        if ordered:
            return ordered
    if isinstance(names, list) and names:
        return [str(item) for item in names]
    raise DatasetError(
        409,
        "NEED_CLASSES",
        "这个数据集没有类别表（data.yaml 的 names）；请显式提供类别名称，系统不会猜类别",
    )


def _split_dirs(base: Path, config: dict) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for split in SPLITS:
        value = config.get(split)
        candidates: list[str] = []
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, list):
            candidates = [str(item) for item in value]
        elif isinstance(value, dict):
            candidates = [str(item) for item in value.values()]
        if not candidates:
            candidates = [f"images/{split}"]
        for rel in candidates:
            target = (base / rel).resolve()
            if target.is_dir() and storage.inside(base.resolve(), target):
                found[split] = target
                break
    return found


def _image_size(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return int(image.size[0]), int(image.size[1])
    except Exception:
        raise DatasetError(409, "IMAGE_UNREADABLE", f"图片无法解码：{path.name}") from None


def _read_label(path: Path, class_count: int) -> tuple[list[list[float]], list[int]]:
    if not path.is_file():
        return [], []
    boxes: list[list[float]] = []
    class_ids: list[int] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text:
            continue
        parts = text.split()
        if len(parts) != 5:
            raise DatasetError(409, "BAD_LABEL", f"{path.name} 第 {number} 行不是 5 列 YOLO 标注")
        try:
            class_id = int(float(parts[0]))
            x, y, w, h = (float(value) for value in parts[1:])
        except ValueError:
            raise DatasetError(409, "BAD_LABEL", f"{path.name} 第 {number} 行含非数字") from None
        if not 0 <= class_id < class_count:
            raise DatasetError(
                409, "CLASS_OUT_OF_RANGE", f"{path.name} 第 {number} 行类别 {class_id} 超出 0..{class_count - 1}"
            )
        if not all(0.0 <= value <= 1.0 for value in (x, y, w, h)) or w <= 0 or h <= 0:
            raise DatasetError(409, "BAD_BBOX", f"{path.name} 第 {number} 行 bbox 不是合法归一化坐标")
        if x - w / 2 < -1e-6 or x + w / 2 > 1 + 1e-6 or y - h / 2 < -1e-6 or y + h / 2 > 1 + 1e-6:
            raise DatasetError(409, "BAD_BBOX", f"{path.name} 第 {number} 行 bbox 越出图片边界")
        boxes.append([round(x, 6), round(y, 6), round(w, 6), round(h, 6)])
        class_ids.append(class_id)
    return boxes, class_ids


def _derive_edge(image_path: Path, root: Path, base: Path, split: str, stem: str) -> Path:
    import cv2
    import numpy as np

    data = image_path.read_bytes()
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise DatasetError(409, "IMAGE_UNREADABLE", f"图片无法解码：{image_path.name}")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edge = cv2.cvtColor(cv2.Canny(gray, 80, 160), cv2.COLOR_GRAY2BGR)
    ok, encoded = cv2.imencode(".png", edge)
    if not ok:
        raise DatasetError(500, "EDGE_FAILED", "edge 图编码失败")
    return storage.write_bytes(root, base / "images_edge" / split / f"{stem}.png", encoded.tobytes())


def _raw_relative(raw_rel: Path, sample_rel: str | None) -> PurePosixPath:
    """把样本的“项目内相对路径”换算成“raw/ 内相对路径”，并确认它真的在 raw/ 下。"""
    text = str(sample_rel or "").replace("\\", "/")
    parts = PurePosixPath(text).parts
    if not text or parts[: len(raw_rel.parts)] != raw_rel.parts:
        raise DatasetError(500, "SCAN_PATH", f"样本路径不在导入目录内：{sample_rel}")
    relative = parts[len(raw_rel.parts) :]
    if not relative:
        raise DatasetError(500, "SCAN_PATH", f"样本路径缺少相对目录：{sample_rel}")
    return PurePosixPath(*relative)


def _write_yaml(root: Path, base: Path, scanned: dict) -> None:
    """写导入后的 data.yaml：目录一律从**真实样本路径**反推，不猜固定目录名。

    reflow 导出的“IR 参考 + RGB 辅助”布局是 images/ 与 images_ir/；按样本实际
    所在目录写，才能原样再导入（辅助目录不会被误写成 images_rgb）。
    """
    payload: dict[str, Any] = {
        "path": "raw",
        "nc": len(scanned["classes"]),
        "names": list(scanned["classes"]),
    }
    raw_rel = Path(storage.relative(root, base / "raw"))
    primary = scanned.get("reference") or scanned.get("primary") or "rgb"
    secondary = scanned["secondary"]
    split_dirs: dict[str, str] = {}
    primary_root: str | None = None
    secondary_root: str | None = None
    for item in scanned["samples"]:
        split = str(item["split"])
        primary_rel = _raw_relative(raw_rel, item["rgb_rel"])
        directory = primary_rel.parent.as_posix()
        known = split_dirs.get(split)
        if known is not None and known != directory:
            raise DatasetError(500, "SPLIT_LAYOUT", f"{split} 划分内的样本目录不一致")
        split_dirs[split] = directory
        primary_root = primary_root or primary_rel.parts[0]
        if secondary:
            if not item.get("x_rel"):
                raise DatasetError(500, "SPLIT_LAYOUT", f"{split}/{item['stem']} 缺少辅助图路径")
            secondary_rel = _raw_relative(raw_rel, item["x_rel"])
            secondary_root = secondary_root or secondary_rel.parts[0]
    for split in SPLITS:
        if split in split_dirs:
            payload[split] = split_dirs[split]
    payload["modalities"] = {primary: primary_root or "images"}
    if secondary:
        payload["modalities"][secondary] = secondary_root or f"images_{secondary}"
    payload["modality_used"] = [primary] + ([secondary] if secondary else [])
    storage.write_bytes(
        root,
        base / "data.yaml",
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8"),
    )


__all__ = [
    "import_dataset",
    "get_owned",
    "list_for_project",
    "public",
    "restore",
    "samples",
    "soft_delete",
]

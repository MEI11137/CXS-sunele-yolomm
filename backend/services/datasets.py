"""数据集导入：目录 / ZIP，RGB 或 RGB+X，严格校验后登记样本清单。

校验清单（每一项都真的读过内容，不是看文件名）：

* data.yaml 的类别表（``names`` / ``nc``）；
* train/val/test 划分目录必须真实存在且非空；
* 每张图都要能被 PIL 真实解码（顺带拿到真实宽高）；
* 标注逐行校验：5 列、class 索引在 ``[0, nc)``、bbox 归一化到 [0,1] 且宽高为正；
* RGB 与 X 按**同名 stem** 配对，缺 X 时：``edge`` 允许派生并记录来源，
  其它模态一律拒绝（绝不用 RGB 冒充红外，也不补零图）；
* ZIP 解压防路径穿越、防符号链接逃逸、防解压炸弹（条目数与总字节上限）。

类别名不猜：yaml 里没有、调用方也没给，就直接报 ``NEED_CLASSES``。
"""

from __future__ import annotations

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
SUPPORTED_SECONDARY = {"ir", "edge", "depth", "x"}


class DatasetError(ApiError):
    pass


def public(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "project_id": data["project_id"],
        "name": data["name"],
        "modalities": _loads(data["modalities"], []),
        "secondary_modality": data["secondary_modality"],
        "classes": _loads(data["classes"], []),
        "root_rel": data["root_rel"],
        "source": data["source"],
        "sample_count": int(data["sample_count"]),
        "split_counts": _loads(data["split_counts"], {}),
        "status": data["status"],
        "notes": data["notes"],
        "created_at": data["created_at"],
    }


def _loads(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (ValueError, TypeError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def get_owned(conn, user_id: int, project_id: str, dataset_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM datasets WHERE id = ? AND project_id = ? AND owner_id = ?",
        (str(dataset_id), str(project_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise ApiError(404, "DATASET_NOT_FOUND", "数据集不存在或不属于当前项目")
    return row


def list_for_project(conn, user_id: int, project_id: str, *, limit: int = 50) -> dict:
    rows = conn.execute(
        "SELECT * FROM datasets WHERE project_id = ? AND owner_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (str(project_id), int(user_id), max(1, min(int(limit), 200))),
    ).fetchall()
    return {"datasets": [public(row) for row in rows]}


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
                "width": row["width"],
                "height": row["height"],
                "boxes": _loads(row["boxes"], []),
            }
            for row in rows
        ]
    }


# --------------------------------------------------------------------------- 导入


def import_dataset(
    conn,
    user_id: int,
    project_id: str,
    *,
    name: str,
    modalities: list[str],
    secondary_modality: str | None = None,
    classes: list[str] | None = None,
    asset_id: str | None = None,
    rel_path: str | None = None,
) -> dict:
    label = str(name or "").strip()
    if not label:
        raise ApiError(400, "VALIDATION", "数据集名称不能为空")
    modes = [str(item).strip().lower() for item in (modalities or ["rgb"]) if str(item).strip()]
    if "rgb" not in modes:
        raise ApiError(400, "VALIDATION", "数据集必须包含 rgb 模态")
    secondary = str(secondary_modality or "").strip().lower() or None
    if secondary is not None and secondary not in SUPPORTED_SECONDARY:
        raise ApiError(400, "VALIDATION", f"第二模态只支持 {'/'.join(sorted(SUPPORTED_SECONDARY))}")
    if secondary and secondary not in modes:
        modes.append(secondary)
    if secondary is None and len(modes) > 1:
        secondary = modes[1]

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

        scanned = _scan(raw, secondary=secondary, classes=classes, project_root=root)
        _write_yaml(root, base, scanned)
        _insert_dataset(
            conn,
            dataset_id=dataset_id,
            user_id=user_id,
            project_id=project_id,
            name=label,
            modalities=modes,
            secondary=secondary,
            scanned=scanned,
            root_rel=storage.relative(root, base),
            source=source,
            source_asset_id=source_asset_id,
        )
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
    scanned: dict,
    root_rel: str,
    source: str,
    source_asset_id: str | None,
) -> None:
    now = security.now_iso()
    conn.execute(
        """
        INSERT INTO datasets
            (id, project_id, owner_id, name, modalities, secondary_modality, classes,
             root_rel, source, source_asset_id, sample_count, split_counts, status, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)
        """,
        (
            dataset_id,
            str(project_id),
            int(user_id),
            name,
            json.dumps(modalities, ensure_ascii=False),
            secondary,
            json.dumps(scanned["classes"], ensure_ascii=False),
            root_rel,
            source,
            source_asset_id,
            len(scanned["samples"]),
            json.dumps(scanned["split_counts"], ensure_ascii=False),
            scanned["notes"],
            now,
        ),
    )
    conn.executemany(
        """
        INSERT INTO dataset_samples
            (id, dataset_id, split, stem, rgb_rel, x_rel, label_rel, width, height, boxes, class_ids, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                item["width"],
                item["height"],
                json.dumps(item["boxes"], ensure_ascii=False),
                json.dumps(item["class_ids"], ensure_ascii=False),
                now,
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


def _scan(raw: Path, *, secondary: str | None, classes: list[str] | None, project_root: Path) -> dict:
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
    rgb_rel = str(modalities.get("rgb") or "images")
    x_rel = str(modalities.get(secondary) or f"images_{secondary}") if secondary else None

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
            if not boxes:
                background += 1
            x_path: Path | None = None
            if secondary:
                candidate = base / str(x_rel) / split / image_path.name
                if candidate.is_file():
                    x_path = candidate
                elif secondary == "edge":
                    x_path = _derive_edge(image_path, project_root, base, split, stem)
                    derived_edge += 1
                else:
                    raise DatasetError(
                        409,
                        "MISSING_MODALITY",
                        f"{split}/{image_path.name} 缺少 {secondary} 配图；"
                        "非 edge 模态不允许用 RGB 或零图顶替",
                    )
            samples.append(
                {
                    "split": split,
                    "stem": stem,
                    "rgb_rel": storage.relative(project_root, image_path),
                    "x_rel": storage.relative(project_root, x_path) if x_path else None,
                    "label_rel": storage.relative(project_root, label_path) if label_path.is_file() else None,
                    "width": width,
                    "height": height,
                    "boxes": boxes,
                    "class_ids": class_ids,
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
    notes.append(f"配对: rgb={len(samples)}, x={sum(1 for s in samples if s['x_rel'])}")
    return {
        "classes": names,
        "samples": samples,
        "split_counts": split_counts,
        "notes": "；".join(notes),
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


def _write_yaml(root: Path, base: Path, scanned: dict) -> None:
    payload: dict[str, Any] = {
        "path": "raw",
        "nc": len(scanned["classes"]),
        "names": list(scanned["classes"]),
    }
    for split in SPLITS:
        if split in scanned["split_counts"]:
            payload[split] = f"images/{split}"
    payload["modalities"] = {"rgb": "images"}
    if scanned["secondary"]:
        payload["modalities"][scanned["secondary"]] = (
            "images_edge" if scanned["secondary"] == "edge" else f"images_{scanned['secondary']}"
        )
    payload["modality_used"] = ["rgb"] + ([scanned["secondary"]] if scanned["secondary"] else [])
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
    "samples",
]

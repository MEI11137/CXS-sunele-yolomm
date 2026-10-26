"""Authorized directory registry and read-only dataset structure inspection (C-07).

A directory must be registered for one project by an explicit user action. The
registered value is the *real* path (``Path.resolve``); every later access
re-resolves the filesystem entry so a renamed symlink cannot widen the scope.
The process being able to read a path is not treated as the website user having
permission: the caller passes the project snapshot obtained from the workspace
policy service and this module only ever answers for the path that was
registered for that project.

Inspection is read-only. It reports what is actually in the tree — splits,
modalitity pairs, class ids, duplicate content and train/val leakage — and never
rewrites, moves or renames anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
LABEL_EXTENSIONS = {".txt"}
SPLITS = {"train": "train", "val": "val", "valid": "val", "validation": "val",
          "test": "test"}
MODALITY_DIRS = {
    "images": "rgb", "images_rgb": "rgb", "rgb": "rgb",
    "images_x": "x", "x": "x",
    "images_edge": "edge", "edge": "edge",
    "images_infrared": "infrared", "infrared": "infrared",
}


class DirectoryError(ValueError):
    code = "DIRECTORY_REFUSED"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DirectoryStore:
    """Project-owned registry of explicitly authorized directories."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        with sqlite3.connect(self.database_path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS authorized_directories (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    label TEXT,
                    root TEXT NOT NULL,
                    real_root TEXT NOT NULL,
                    created_by INTEGER,
                    created_at TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                )"""
            )

    def register(self, project_id: str, root: str, label: str | None,
                 created_by: int | None) -> dict:
        candidate = Path(root).expanduser()
        if not candidate.is_absolute():
            raise DirectoryError("目录必须是绝对路径")
        try:
            real = candidate.resolve(strict=True)
        except OSError as exc:
            raise DirectoryError(f"目录不存在或无法访问: {exc}") from exc
        if not real.is_dir():
            raise DirectoryError("路径不是目录")
        directory_id = str(uuid.uuid4())
        with sqlite3.connect(self.database_path) as conn:
            conn.execute(
                "INSERT INTO authorized_directories "
                "(id, project_id, label, root, real_root, created_by, created_at, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (directory_id, project_id, label, str(candidate), str(real),
                 created_by, _now()),
            )
        return self.get(directory_id)

    def get(self, directory_id: str) -> dict:
        with sqlite3.connect(self.database_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM authorized_directories WHERE id=?", (directory_id,)
            ).fetchone()
        if row is None:
            raise FileNotFoundError("目录未登记")
        record = dict(row)
        if not record["enabled"]:
            raise DirectoryError("该目录已停用")
        return record

    def list(self, project_id: str) -> list[dict]:
        with sqlite3.connect(self.database_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM authorized_directories WHERE project_id=? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def disable(self, directory_id: str) -> None:
        with sqlite3.connect(self.database_path) as conn:
            conn.execute(
                "UPDATE authorized_directories SET enabled=0 WHERE id=?", (directory_id,),
            )


# ----------------------------------------------------------------- inspection


def _contained(root: Path, path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root)
    except OSError:
        return False


def _split_of(parts: tuple[str, ...]) -> str:
    for part in parts[:-1]:
        normalized = SPLITS.get(part.lower())
        if normalized:
            return normalized
    return "unassigned"


def _modality_of(parts: tuple[str, ...]) -> str:
    for part in parts[:-1]:
        normalized = MODALITY_DIRS.get(part.lower())
        if normalized:
            return normalized
    return "rgb"


def _read_yaml_names(root: Path) -> list[str] | None:
    for name in ("data.yaml", "data.yml"):
        candidate = root / name
        if not candidate.is_file():
            continue
        try:
            import yaml

            payload = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        except Exception:
            return None
        names = payload.get("names")
        if isinstance(names, dict):
            return [str(names[key]) for key in sorted(names, key=lambda item: int(item))]
        if isinstance(names, list):
            return [str(item) for item in names]
        return None
    return None


def scan_dataset_tree(root: str | Path, max_samples: int = 50000) -> dict:
    """Read one tree and report samples, classes, numeric problems and duplicates.

    Only paths that resolve inside the registered root are read; a symlink that
    points elsewhere is reported and skipped instead of followed.
    """
    base = Path(root).resolve(strict=True)
    issues: list[dict] = []
    samples: dict[str, dict] = {}
    images: list[tuple[Path, str, str]] = []
    labels: dict[str, Path] = {}
    truncated = False

    for current, dirnames, filenames in os.walk(base, followlinks=False):
        current_path = Path(current)
        # os.walk lists symlinked directories in dirnames; drop and report them.
        for directory in list(dirnames):
            candidate = current_path / directory
            if candidate.is_symlink():
                dirnames.remove(directory)
                issues.append({
                    "code": "SYMLINK_SKIPPED", "severity": "warning",
                    "message": "符号链接目录未跟随", "path": str(candidate.relative_to(base)),
                })
            elif not _contained(base, candidate):
                dirnames.remove(directory)
                issues.append({
                    "code": "PATH_OUTSIDE_ROOT", "severity": "blocking",
                    "message": "目录跳转到授权范围之外", "path": str(candidate),
                })
        for filename in filenames:
            path = current_path / filename
            if path.is_symlink():
                issues.append({
                    "code": "SYMLINK_SKIPPED", "severity": "warning",
                    "message": "符号链接文件未读取", "path": str(path.relative_to(base)),
                })
                continue
            if not _contained(base, path):
                issues.append({
                    "code": "PATH_OUTSIDE_ROOT", "severity": "blocking",
                    "message": "文件跳转到授权范围之外", "path": str(path),
                })
                continue
            relative = path.relative_to(base)
            parts = relative.parts
            suffix = path.suffix.lower()
            if suffix in IMAGE_EXTENSIONS:
                images.append((path, _split_of(parts), _modality_of(parts)))
            elif suffix in LABEL_EXTENSIONS:
                labels[relative.with_suffix("").as_posix()] = path

    class_names = _read_yaml_names(base)
    derived = class_names is None
    if derived:
        class_names = []
        issues.append({
            "code": "CLASS_NAMES_MISSING", "severity": "blocking",
            "message": "目录中没有 data.yaml 的 names，需显式提供类别列表",
            "path": None,
        })

    max_class_id = -1
    for path, split, modality in images:
        relative = path.relative_to(base)
        key = relative.with_suffix("").as_posix()
        # images/train/a and images_edge/train/a both become train/a so the
        # modalities of one frame meet on a single sample key.
        stem_key = _strip_modality(key)
        if stem_key in samples:
            samples[stem_key]["modalities"][modality] = str(path)
        else:
            samples[stem_key] = {
                "key": stem_key, "split": split, "modalities": {modality: str(path)},
                "label": None, "class_ids": [], "problems": [],
            }

    for key, sample in samples.items():
        # Images and labels meet on the modality-stripped relative key:
        # images_edge/train/a.png and labels/train/a.txt share "train/a".
        label_path = labels.get(f"labels/{key}")
        if label_path is not None:
            sample["label"] = str(label_path)
        if sample["label"] is None:
            sample["problems"].append({"code": "LABEL_MISSING",
                                       "message": "缺少标签文件"})
            continue
        class_ids, parsed_issues = _parse_label(Path(sample["label"]))
        sample["class_ids"] = class_ids
        max_class_id = max([max_class_id, *class_ids])
        for item in parsed_issues:
            item["path"] = sample["label"]
            issues.append(item)

    if derived and max_class_id >= 0:
        class_names = [f"class_{index}" for index in range(max_class_id + 1)]

    for name in labels:
        if not any(sample["label"] == str(labels[name]) for sample in samples.values()):
            issues.append({
                "code": "ORPHAN_LABEL", "severity": "warning",
                "message": "标签文件没有对应图片", "path": str(labels[name]),
            })

    duplicates, leakage = _duplicates_and_leakage(base, samples)
    ordered = list(samples.values())[:max_samples]
    truncated = len(samples) > max_samples
    if truncated:
        issues.append({
            "code": "SCAN_TRUNCATED", "severity": "warning",
            "message": f"目录样本超过 {max_samples}，仅返回前 {max_samples} 条明细",
            "path": None,
        })
    splits: dict[str, int] = {}
    for sample in samples.values():
        splits[sample["split"]] = splits.get(sample["split"], 0) + 1
    return {
        "root": str(base),
        "samples": ordered,
        "sample_count": len(samples),
        "class_names": class_names,
        "class_names_derived": derived,
        "issues": issues,
        "duplicates": duplicates,
        "leakage": leakage,
        "splits": splits,
    }


def modality_key(path: str) -> str:
    for part in Path(path).parts:
        if part.lower() in MODALITY_DIRS:
            return part
    return "images"


def _strip_modality(key: str) -> str:
    parts = Path(key).parts
    if parts and parts[0].lower() in MODALITY_DIRS:
        return Path(*parts[1:]).as_posix()
    return key


def _parse_label(path: Path) -> tuple[list[int], list[dict]]:
    class_ids: list[int] = []
    issues: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [], [{"code": "LABEL_ENCODING", "severity": "blocking",
                     "message": "标签不是 UTF-8 文本", "line": None}]
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 5:
            issues.append({"code": "LABEL_FIELD_COUNT", "severity": "blocking",
                           "message": f"标签字段数为 {len(fields)}，应为 5", "line": number})
            continue
        try:
            class_id = int(float(fields[0]))
            coords = [float(value) for value in fields[1:]]
        except ValueError:
            issues.append({"code": "LABEL_NOT_NUMERIC", "severity": "blocking",
                           "message": "标签包含非数字字段", "line": number})
            continue
        if class_id < 0:
            issues.append({"code": "CLASS_ID_NEGATIVE", "severity": "blocking",
                           "message": "类别编号为负数", "line": number})
        if not all(0.0 <= value <= 1.0 for value in coords):
            issues.append({"code": "COORDINATE_OUT_OF_RANGE", "severity": "blocking",
                           "message": "归一化坐标越界", "line": number})
        if coords[2] <= 0 or coords[3] <= 0:
            issues.append({"code": "BOX_NOT_POSITIVE", "severity": "blocking",
                           "message": "宽或高不为正", "line": number})
        class_ids.append(class_id)
    return class_ids, issues


def _duplicates_and_leakage(base: Path, samples: dict[str, dict]) -> tuple[list, dict]:
    digests: dict[str, list[dict]] = {}
    for sample in samples.values():
        rgb = sample["modalities"].get("rgb") or next(iter(sample["modalities"].values()), None)
        if not rgb:
            continue
        try:
            digest = hashlib.sha256(Path(rgb).read_bytes()).hexdigest()
        except OSError:
            continue
        digests.setdefault(digest, []).append({
            "key": sample["key"], "split": sample["split"],
            "label": sample["label"],
        })
    duplicates = []
    leaking = []
    for digest, entries in digests.items():
        if len(entries) > 1:
            duplicates.append({"sha256": digest, "count": len(entries),
                               "samples": entries[:20]})
            splits = {entry["split"] for entry in entries}
            if len(splits) > 1:
                leaking.append({"sha256": digest, "splits": sorted(splits),
                                "keys": [entry["key"] for entry in entries][:20]})
    return duplicates, {"cross_split": leaking, "count": len(leaking)}


def inspect_directory(root: str | Path, max_samples: int = 50000) -> dict:
    report = scan_dataset_tree(root, max_samples=max_samples)
    pairing = []
    for sample in report["samples"]:
        if "rgb" not in sample["modalities"]:
            sample["problems"].append({"code": "RGB_MISSING", "message": "缺少 RGB 图片"})
        if len(sample["modalities"]) > 1:
            pairing.append(sample["key"])
    report["pairs"] = len(pairing)
    return report


def search_directory(root: str | Path, query: str, limit: int = 100) -> dict:
    base = Path(root).resolve(strict=True)
    needle = query.lower()
    matches: list[dict] = []
    truncated = False
    for current, dirnames, filenames in os.walk(base, followlinks=False):
        current_path = Path(current)
        dirnames[:] = [name for name in dirnames
                       if not (current_path / name).is_symlink()]
        for filename in filenames:
            path = current_path / filename
            if path.is_symlink() or not _contained(base, path):
                continue
            relative = path.relative_to(base).as_posix()
            if needle in relative.lower():
                if len(matches) >= limit:
                    truncated = True
                    break
                matches.append({"path": relative, "size": path.stat().st_size})
        if truncated:
            break
    return {"matches": matches, "truncated": truncated}

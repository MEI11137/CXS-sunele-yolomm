"""Preview-and-confirm import for YOLO dataset archives.

``services.dataset_contract.validate_dataset_archive`` stays the single
authority for whether an archive may be registered. This module adds two
things on top of it:

* :func:`inspect_dataset_archive` collects *every* structural, pairing and
  label problem with a stable code so a user (or the agent) can show a fix
  list before anything is written;
* :func:`import_dataset_archive` re-runs the authoritative check, stores the
  validated archive and registers the dataset row with its real class
  mapping, modality and runtime contract.

Nothing here trusts a caller-declared class list, project or owner: the
caller must already have verified resource permission and passes the values
it resolved from its own resource service.
"""

from __future__ import annotations

import json
import math
import sqlite3
import stat
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

# Shared internals of the dataset contract module: reusing them keeps one
# definition of archive layout, member normalisation and class-name rules.
from services.dataset_contract import (
    IMAGE_EXTENSIONS,
    SPLITS,
    DatasetValidationError,
    _class_names,
    _clean_class_names,
    _directory_name,
    _find_data_yaml,
    _infer_archive_root,
    _normalized_member,
    _sample_keys,
    inject_generated_data_yaml,
    validate_dataset_archive,
)


# ``DATASET_STATUS['APPROVED']`` in utils.models; kept as a constant so this
# service does not depend on the file client or Flask configuration.
STATUS_APPROVED = 1
MAX_ISSUES = 200


class DatasetImportError(ValueError):
    """An archive cannot be registered; ``issues`` carries the full list."""

    def __init__(self, message: str, *, code: str = "DATASET_INVALID",
                 issues: list["DatasetIssue"] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.issues = list(issues or [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class DatasetIssue:
    code: str
    severity: str
    message: str
    path: str | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
            "line": self.line,
        }


class _IssueLog:
    """Collects issues in a bounded list while counting every finding."""

    def __init__(self, limit: int = MAX_ISSUES) -> None:
        self.limit = limit
        self.items: list[DatasetIssue] = []
        self.total = 0
        self.blocking_total = 0
        self.warning_total = 0

    def add(self, code: str, message: str, *, severity: str = "blocking",
            path: str | None = None, line: int | None = None) -> None:
        if severity not in {"blocking", "warning"}:
            raise ValueError("Unknown issue severity")
        self.total += 1
        if severity == "blocking":
            self.blocking_total += 1
        else:
            self.warning_total += 1
        if len(self.items) < self.limit:
            self.items.append(DatasetIssue(code, severity, message, path, line))

    @property
    def truncated(self) -> bool:
        return self.total > len(self.items)


@dataclass
class DatasetImportPreview:
    """Everything a user needs to decide whether to register the archive."""

    dataset_mode: str
    primary_modality: str
    secondary_modality: str | None
    class_names: list[str]
    split_counts: dict[str, int]
    generated_data_yaml: bool
    archive_root: str
    runtime_mapping: dict[str, Any]
    issues: list[DatasetIssue] = field(default_factory=list)
    issue_total: int = 0
    blocking_total: int = 0
    warning_total: int = 0
    truncated: bool = False
    data_yaml_member: str | None = None

    @property
    def class_count(self) -> int:
        return len(self.class_names)

    @property
    def total_samples(self) -> int:
        return sum(self.split_counts.values())

    @property
    def ready_to_register(self) -> bool:
        return self.blocking_total == 0 and bool(self.class_names)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_mode": self.dataset_mode,
            "primary_modality": self.primary_modality,
            "secondary_modality": self.secondary_modality,
            "class_names": list(self.class_names),
            "class_count": self.class_count,
            "split_counts": dict(self.split_counts),
            "total_samples": self.total_samples,
            "generated_data_yaml": self.generated_data_yaml,
            "archive_root": self.archive_root,
            "runtime_mapping": dict(self.runtime_mapping),
            "data_yaml_member": self.data_yaml_member,
            "issues": [issue.to_dict() for issue in self.issues],
            "issue_counts": {
                "total": self.issue_total,
                "blocking": self.blocking_total,
                "warning": self.warning_total,
                "listed": len(self.issues),
                "truncated": self.truncated,
            },
            "ready_to_register": self.ready_to_register,
        }


def _finish(issues: _IssueLog, **values: Any) -> DatasetImportPreview:
    base: dict[str, Any] = {
        "dataset_mode": "",
        "primary_modality": "rgb",
        "secondary_modality": None,
        "class_names": [],
        "split_counts": {},
        "generated_data_yaml": False,
        "archive_root": "",
        "runtime_mapping": {},
        "data_yaml_member": None,
    }
    base.update(values)
    return DatasetImportPreview(
        **base,
        issues=issues.items,
        issue_total=issues.total,
        blocking_total=issues.blocking_total,
        warning_total=issues.warning_total,
        truncated=issues.truncated,
    )


def _unsafe_members(archive: zipfile.ZipFile, issues: _IssueLog) -> list[str]:
    """Return normalised member names after recording unsafe entries."""
    members: list[str] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = _normalized_member(info.filename)
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(unix_mode):
            issues.add("UNSAFE_ENTRY", f"ZIP 包含符号链接: {name}", path=name)
            continue
        parts = PurePosixPath(name).parts
        if name.startswith("/") or ".." in parts or ":" in name:
            issues.add("UNSAFE_ENTRY", f"ZIP 包含不安全路径: {name}", path=name)
            continue
        members.append(name)
    return members


def _duplicate_stems(members: set[str], directory: str, split: str) -> list[str]:
    """Samples sharing a stem but differing in extension collapse to one key."""
    prefix = f"{directory}/{split}/"
    seen: dict[str, int] = {}
    for member in members:
        if not member.startswith(prefix):
            continue
        relative = PurePosixPath(member[len(prefix):])
        if relative.suffix.lower() in IMAGE_EXTENSIONS:
            key = str(relative.with_suffix(""))
            seen[key] = seen.get(key, 0) + 1
    return sorted(key for key, count in seen.items() if count > 1)


def _scan_labels(archive: zipfile.ZipFile, archive_root: str,
                 class_names: list[str], issues: _IssueLog) -> None:
    prefix = f"{archive_root}/" if archive_root else ""
    for info in archive.infolist():
        if info.is_dir():
            continue
        normalized = _normalized_member(info.filename)
        if not normalized.startswith(prefix):
            continue
        relative = normalized[len(prefix):]
        path = PurePosixPath(relative)
        if (len(path.parts) < 3 or path.parts[0] != "labels"
                or path.parts[1] not in SPLITS or path.suffix.lower() != ".txt"):
            continue
        try:
            content = archive.read(info).decode("utf-8-sig")
        except UnicodeDecodeError:
            issues.add("LABEL_ENCODING", "YOLO 标注不是 UTF-8 文本", path=relative)
            continue
        for line_number, line in enumerate(content.splitlines(), start=1):
            values = line.split()
            if not values:
                continue
            if len(values) != 5:
                issues.add("LABEL_FIELD_COUNT", "YOLO 标注必须包含 5 个字段",
                           path=relative, line=line_number)
                continue
            try:
                class_id = int(values[0])
                x_center, y_center, width, height = map(float, values[1:])
            except ValueError:
                issues.add("LABEL_NOT_NUMERIC", "YOLO 标注包含无效数字",
                           path=relative, line=line_number)
                continue
            if class_id < 0 or class_id >= len(class_names):
                issues.add(
                    "LABEL_CLASS_RANGE",
                    f"类别编号 {class_id} 超出 0..{len(class_names) - 1} 的实际类别范围",
                    path=relative, line=line_number,
                )
                continue
            coordinates = (x_center, y_center, width, height)
            if (
                not all(math.isfinite(value) for value in coordinates)
                or not (0 <= x_center <= 1 and 0 <= y_center <= 1)
                or not (0 < width <= 1 and 0 < height <= 1)
                or x_center - width / 2 < 0
                or x_center + width / 2 > 1
                or y_center - height / 2 < 0
                or y_center + height / 2 > 1
            ):
                issues.add(
                    "LABEL_COORDINATE",
                    "归一化坐标越界或框体超出图片范围",
                    path=relative, line=line_number,
                )


def inspect_dataset_archive(
    zip_path: str | Path,
    dataset_mode: str,
    *,
    secondary_modality: str | None = None,
    class_names: list[str] | None = None,
) -> DatasetImportPreview:
    """Report every problem in an archive without registering anything."""
    issues = _IssueLog()
    requested = str(dataset_mode or "").strip().lower()
    if requested not in {"single", "multimodal"}:
        issues.add("INVALID_DATASET_MODE", "dataset_mode 必须是 single 或 multimodal")
        return _finish(issues)

    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        issues.add("BAD_ZIP", "文件不是有效的 ZIP 归档")
        return _finish(issues, dataset_mode=requested)

    with archive:
        members = _unsafe_members(archive, issues)
        if issues.blocking_total:
            return _finish(issues, dataset_mode=requested)

        try:
            yaml_member = _find_data_yaml(members)
        except DatasetValidationError as exc:
            issues.add("DUPLICATE_DATA_YAML", str(exc))
            return _finish(issues, dataset_mode=requested)

        generated = yaml_member is None
        if generated:
            if requested != "multimodal":
                issues.add("MISSING_DATA_YAML", "ZIP 缺少 data.yaml")
                return _finish(issues, dataset_mode=requested)
            selected = str(secondary_modality or "").strip().lower()
            names = _clean_class_names(class_names)
            if not selected:
                issues.add("MISSING_X_MODALITY",
                           "ZIP 缺少 data.yaml，需指定本次配对使用的 X 模态")
            elif selected == "rgb":
                issues.add("INVALID_X_MODALITY", "X 模态不能与 RGB 相同")
            if not names:
                issues.add("MISSING_CLASS_NAMES",
                           "ZIP 缺少 data.yaml，需按实际顺序填写类别名称")
            if issues.blocking_total:
                return _finish(issues, dataset_mode=requested)
            try:
                archive_root = _infer_archive_root(members)
            except DatasetValidationError as exc:
                issues.add("ARCHIVE_ROOT_UNRESOLVED", str(exc))
                return _finish(issues, dataset_mode=requested)
            yaml_member = f"{archive_root + '/' if archive_root else ''}data.yaml"
            data: dict[str, Any] = {
                "path": ".",
                "train": "./images/train",
                "val": "./images/val",
                "modalities": {"rgb": "./images", selected: f"./images_{selected}"},
                "modality_used": ["rgb", selected],
                "nc": len(names),
                "names": names,
            }
        else:
            archive_root = str(PurePosixPath(yaml_member).parent)
            if archive_root == ".":
                archive_root = ""
            try:
                data = yaml.safe_load(archive.read(yaml_member)) or {}
            except (yaml.YAMLError, OSError) as exc:
                issues.add("INVALID_DATA_YAML", f"data.yaml 无法解析: {exc}", path=yaml_member)
                return _finish(issues, dataset_mode=requested)
            if not isinstance(data, dict):
                issues.add("INVALID_DATA_YAML", "data.yaml 顶层必须是映射", path=yaml_member)
                return _finish(issues, dataset_mode=requested)

        prefix = f"{archive_root}/" if archive_root else ""
        relative_members = {
            member[len(prefix):] for member in members if member.startswith(prefix)
        }

        modalities_config = data.get("modalities") or data.get("modality")
        yaml_multimodal = bool(modalities_config or data.get("modality_used"))
        if requested == "single" and yaml_multimodal:
            issues.add("MODE_MISMATCH", "选择的是单模态，但 data.yaml 声明了多模态",
                       path=yaml_member)
        if requested == "multimodal" and not yaml_multimodal:
            issues.add("MODE_MISMATCH",
                       "选择的是多模态，但 data.yaml 未声明 modalities 与 modality_used",
                       path=yaml_member)

        try:
            names = _class_names(data)
        except DatasetValidationError as exc:
            issues.add("INVALID_CLASS_NAMES", str(exc), path=yaml_member)
            names = []
        if names:
            cleaned = [str(name).strip() for name in names]
            empty = [str(index) for index, name in enumerate(cleaned) if not name]
            if empty:
                issues.add("INVALID_CLASS_NAMES",
                           f"类别名称不能为空（编号 {', '.join(empty[:10])}）",
                           path=yaml_member)
            repeated = sorted({name for name in cleaned if cleaned.count(name) > 1})
            if repeated:
                issues.add(
                    "DUPLICATE_CLASS_NAMES",
                    f"类别名称重复，无法确定标签与类别的一一映射: {', '.join(repeated[:10])}",
                    path=yaml_member,
                )

        secondary_directory: str | None = None
        rgb_directory = "images"
        if requested == "multimodal" and not issues.blocking_total:
            used = data.get("modality_used")
            if not isinstance(modalities_config, dict) or not isinstance(used, list) or len(used) != 2:
                issues.add("MODALITY_CONFIG", "多模态 data.yaml 要求 modality_used 按 [rgb, X] 配置",
                           path=yaml_member)
            else:
                primary = str(used[0]).strip().lower()
                candidate = str(used[1]).strip().lower()
                if primary != "rgb" or primary not in modalities_config or candidate not in modalities_config:
                    issues.add("MODALITY_CONFIG",
                               "modality_used 必须以 rgb 为主模态，且两个模态都存在于 modalities",
                               path=yaml_member)
                elif candidate == "rgb":
                    issues.add("MODALITY_CONFIG", "X 模态不能与 RGB 相同", path=yaml_member)
                else:
                    secondary_modality = candidate
                    rgb_directory = _directory_name(modalities_config[primary], "images")
                    secondary_directory = _directory_name(
                        modalities_config[candidate], f"images_{candidate}"
                    )

        if issues.blocking_total:
            return _finish(issues, dataset_mode=requested, class_names=list(names))

        split_counts: dict[str, int] = {}
        for split in SPLITS:
            rgb_keys = _sample_keys(relative_members, rgb_directory, split, IMAGE_EXTENSIONS)
            label_keys = _sample_keys(relative_members, "labels", split, {".txt"})
            split_counts[split] = len(rgb_keys)
            if split in {"train", "val"} and not rgb_keys:
                issues.add("MISSING_SPLIT", f"缺少 {rgb_directory}/{split} 图片")
            for key in sorted(rgb_keys - label_keys):
                issues.add("MISSING_LABEL", "图片缺少对应的 YOLO 标注",
                           path=f"labels/{split}/{key}.txt")
            for key in sorted(label_keys - rgb_keys):
                issues.add("ORPHAN_LABEL", "标注文件没有对应图片",
                           severity="warning", path=f"labels/{split}/{key}.txt")
            for key in _duplicate_stems(relative_members, rgb_directory, split):
                issues.add(
                    "DUPLICATE_SAMPLE",
                    "同名不同扩展名的图片会被当作同一样本，只保留其中一个",
                    severity="warning", path=f"{rgb_directory}/{split}/{key}",
                )
            if secondary_directory is not None:
                x_keys = _sample_keys(relative_members, secondary_directory, split, IMAGE_EXTENSIONS)
                for key in sorted(rgb_keys - x_keys):
                    issues.add(
                        "MISSING_SECONDARY",
                        f"RGB 样本缺少 {secondary_modality} 配对图片",
                        path=f"{secondary_directory}/{split}/{key}",
                    )
                for key in sorted(x_keys - rgb_keys):
                    issues.add(
                        "ORPHAN_SECONDARY",
                        f"{secondary_modality} 图片没有对应 RGB 样本",
                        path=f"{secondary_directory}/{split}/{key}",
                    )
                for key in _duplicate_stems(relative_members, secondary_directory, split):
                    issues.add(
                        "DUPLICATE_SAMPLE",
                        "同名不同扩展名的图片会被当作同一样本，只保留其中一个",
                        severity="warning", path=f"{secondary_directory}/{split}/{key}",
                    )

        if names:
            # Pairing failures already block registration; label problems are
            # still reported so the user can fix everything in one pass.
            _scan_labels(archive, archive_root, names, issues)

    runtime_mapping = {
        "rgb_directory": rgb_directory,
        "secondary_directory": secondary_directory,
        "splits": split_counts,
    }
    return _finish(
        issues,
        dataset_mode=requested,
        primary_modality="rgb",
        secondary_modality=secondary_modality,
        class_names=list(names),
        split_counts=split_counts,
        generated_data_yaml=generated,
        archive_root=archive_root,
        runtime_mapping=runtime_mapping,
        data_yaml_member=yaml_member,
    )


def _normalized_name(name: str | None) -> str:
    return str(name or "").strip()


def import_dataset_archive(
    *,
    zip_path: str | Path,
    name: str,
    description: str = "",
    dataset_mode: str,
    secondary_modality: str | None = None,
    class_names: list[str] | None = None,
    database,
    storage_client,
    project_id: str | None = None,
    owner_id: int | None = None,
    on_registered=None,
) -> dict[str, Any]:
    """Validate, store and register one archive as a dataset row.

    ``zip_path`` must be a caller-owned temporary copy: a generated data.yaml
    is written into it before storage. The authoritative contract check runs
    again here, so a stale preview can never register an invalid archive.

    ``on_registered(conn, dataset_id)`` runs inside the registration
    transaction, so a caller that must bind a source upload to the new dataset
    cannot leave a registered dataset without its provenance.
    """
    dataset_name = _normalized_name(name)
    if not dataset_name:
        raise DatasetImportError("数据集名称不能为空", code="NAME_REQUIRED")

    preview = inspect_dataset_archive(
        zip_path, dataset_mode,
        secondary_modality=secondary_modality, class_names=class_names,
    )
    if not preview.ready_to_register:
        listed = [issue for issue in preview.issues if issue.severity == "blocking"]
        detail = "；".join(
            f"{issue.code}{'@' + issue.path if issue.path else ''}" for issue in listed[:10]
        )
        message = "数据集格式验证失败" + (f": {detail}" if detail else "")
        if preview.issue_total > len(listed):
            message += f"（共 {preview.blocking_total} 项阻断问题，仅列出前 {len(listed)} 项）"
        raise DatasetImportError(message, issues=preview.issues)

    try:
        validation = validate_dataset_archive(
            str(zip_path), dataset_mode,
            secondary_modality=secondary_modality, class_names=class_names,
        )
        inject_generated_data_yaml(str(zip_path), validation)
    except DatasetValidationError as exc:
        raise DatasetImportError(
            f"数据集格式验证失败: {exc}", issues=preview.issues,
        ) from exc

    conn = database.get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM datasets WHERE name = ?", (dataset_name,)
        ).fetchone()
    finally:
        conn.close()
    if existing:
        raise DatasetImportError("数据集名称已存在", code="NAME_CONFLICT")

    file_size = Path(zip_path).stat().st_size
    try:
        stored = storage_client.upload("datasets", str(zip_path))
    except Exception as exc:
        raise DatasetImportError(f"数据集文件保存失败: {exc}", code="STORAGE_FAILED") from exc

    dataset_id = str(uuid.uuid4())
    upload_time = datetime.now().isoformat()
    structure_info = validation.to_dict()
    try:
        conn = database.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM datasets WHERE name = ?", (dataset_name,)).fetchone():
                raise DatasetImportError("数据集名称已存在", code="NAME_CONFLICT")
            cursor = conn.execute(
                """
                INSERT INTO datasets (id, name, description, zip_bucket, zip_object_key,
                                      structure_info, upload_time, file_size, status,
                                      dataset_mode, primary_modality, secondary_modality,
                                      runtime_mapping)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dataset_id, dataset_name, _normalized_name(description),
                    stored["bucket"], stored["objectKey"],
                    json.dumps(structure_info), upload_time, file_size,
                    STATUS_APPROVED, validation.dataset_mode, validation.primary_modality,
                    validation.secondary_modality,
                    json.dumps(validation.runtime_mapping, ensure_ascii=False),
                ),
            )
            if cursor.rowcount <= 0:
                raise RuntimeError("数据集记录未写入")
            if on_registered is not None:
                on_registered(conn, dataset_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except DatasetImportError:
        _discard(storage_client, stored)
        raise
    except Exception as exc:
        _discard(storage_client, stored)
        raise DatasetImportError(f"保存数据集信息失败: {exc}", code="REGISTER_FAILED") from exc

    return {
        "dataset_id": dataset_id,
        "name": dataset_name,
        "description": _normalized_name(description),
        "dataset_mode": validation.dataset_mode,
        "primary_modality": validation.primary_modality,
        "secondary_modality": validation.secondary_modality,
        "class_names": list(validation.names),
        "class_count": len(validation.names),
        "split_counts": dict(validation.split_counts),
        "total_samples": sum(validation.split_counts.values()),
        "generated_data_yaml": validation.generated_data_yaml,
        "runtime_mapping": dict(validation.runtime_mapping),
        "status": STATUS_APPROVED,
        "file_size": file_size,
        "upload_time": upload_time,
        "project_id": project_id,
        "owner_id": owner_id,
        "storage": {"bucket": stored["bucket"], "object_key": stored["objectKey"]},
        "structure_info": structure_info,
        "warnings": [issue.to_dict() for issue in preview.issues
                     if issue.severity == "warning"],
    }


def _discard(storage_client, stored: dict[str, str]) -> None:
    try:
        storage_client.delete(stored["bucket"], stored["objectKey"])
    except Exception:
        pass

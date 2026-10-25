"""Validation and runtime mapping for single-modal and RGB+X datasets."""

from __future__ import annotations

import stat
import math
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
SPLITS = ("train", "val", "test")


class DatasetValidationError(ValueError):
    """A dataset archive does not satisfy the selected contract."""


@dataclass
class DatasetValidationResult:
    dataset_mode: str
    primary_modality: str
    secondary_modality: str | None
    secondary_directory: str | None
    names: list[str]
    split_counts: dict[str, int]
    missing_pairs: list[str]
    data_yaml_member: str
    archive_root: str
    runtime_mapping: dict[str, Any]
    generated_data_yaml: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(
            {
                "train_images": self.split_counts.get("train", 0),
                "train_labels": self.split_counts.get("train", 0),
                "valid_images": self.split_counts.get("val", 0),
                "valid_labels": self.split_counts.get("val", 0),
                "test_images": self.split_counts.get("test", 0),
                "test_labels": self.split_counts.get("test", 0),
            }
        )
        return payload


def _normalized_member(name: str) -> str:
    normalized = name.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _find_data_yaml(members: list[str]) -> str | None:
    candidates = [name for name in members if PurePosixPath(name).name.lower() == "data.yaml"]
    if len(candidates) > 1:
        raise DatasetValidationError("ZIP中只能包含一个data.yaml")
    return candidates[0] if candidates else None


def _infer_archive_root(members: list[str]) -> str:
    candidates: set[str] = set()
    for member in members:
        parts = PurePosixPath(member).parts
        for index in range(len(parts) - 2):
            if parts[index].lower() == "images" and parts[index + 1].lower() in SPLITS:
                candidates.add("/".join(parts[:index]))
    if len(candidates) != 1:
        raise DatasetValidationError("ZIP缺少data.yaml，且无法唯一定位images/train目录")
    return candidates.pop()


def _clean_class_names(class_names: list[str] | None) -> list[str]:
    return [str(name).strip() for name in (class_names or []) if str(name).strip()]


def _directory_name(value: Any, fallback: str) -> str:
    text = str(value or fallback).replace("\\", "/").rstrip("/")
    return PurePosixPath(text).name or fallback


def _class_names(data: dict) -> list[str]:
    names = data.get("names")
    if isinstance(names, list):
        return [str(name) for name in names]
    if isinstance(names, dict):
        def key_order(item):
            try:
                return int(item[0])
            except (TypeError, ValueError):
                return str(item[0])

        return [str(value) for _, value in sorted(names.items(), key=key_order)]
    raise DatasetValidationError("data.yaml缺少有效的names类别配置")


def _sample_keys(members: set[str], directory: str, split: str, extensions: set[str]) -> set[str]:
    prefix = f"{directory}/{split}/"
    keys = set()
    for member in members:
        if not member.startswith(prefix):
            continue
        relative = PurePosixPath(member[len(prefix):])
        if relative.suffix.lower() in extensions:
            keys.add(str(relative.with_suffix("")))
    return keys


def _validate_yolo_labels(zip_path: str, archive_root: str, class_names: list[str]) -> None:
    prefix = f"{archive_root}/" if archive_root else ""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                normalized = _normalized_member(info.filename)
                if not normalized.startswith(prefix):
                    continue
                relative = normalized[len(prefix):]
                path = PurePosixPath(relative)
                if len(path.parts) < 3 or path.parts[0] != "labels" or path.suffix.lower() != ".txt":
                    continue
                try:
                    content = archive.read(info).decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise DatasetValidationError(f"YOLO标签不是UTF-8文本: {relative}") from exc
                for line_number, line in enumerate(content.splitlines(), start=1):
                    values = line.split()
                    if not values:
                        continue
                    if len(values) != 5:
                        raise DatasetValidationError(
                            f"YOLO标签字段数量错误: {relative}:{line_number}"
                        )
                    try:
                        class_id = int(values[0])
                        x_center, y_center, width, height = map(float, values[1:])
                    except ValueError as exc:
                        raise DatasetValidationError(
                            f"YOLO标签包含无效数字: {relative}:{line_number}"
                        ) from exc
                    coordinates = (x_center, y_center, width, height)
                    if (
                        class_id < 0
                        or class_id >= len(class_names)
                        or not all(math.isfinite(value) for value in coordinates)
                        or not (0 <= x_center <= 1 and 0 <= y_center <= 1)
                        or not (0 < width <= 1 and 0 < height <= 1)
                        or x_center - width / 2 < 0
                        or x_center + width / 2 > 1
                        or y_center - height / 2 < 0
                        or y_center + height / 2 > 1
                    ):
                        raise DatasetValidationError(
                            f"YOLO标签类别或归一化坐标超出范围: {relative}:{line_number}"
                        )
    except zipfile.BadZipFile as exc:
        raise DatasetValidationError("文件不是有效的ZIP归档") from exc


def validate_dataset_archive(
    zip_path: str,
    dataset_mode: str,
    *,
    secondary_modality: str | None = None,
    class_names: list[str] | None = None,
) -> DatasetValidationResult:
    requested_mode = str(dataset_mode or "").strip().lower()
    if requested_mode not in {"single", "multimodal"}:
        raise DatasetValidationError("dataset_mode必须是single或multimodal")

    try:
        with zipfile.ZipFile(zip_path) as archive:
            raw_members = [_normalized_member(info.filename) for info in archive.infolist() if not info.is_dir()]
            data_yaml_member = _find_data_yaml(raw_members)
            generated_data_yaml = data_yaml_member is None
            if generated_data_yaml:
                if requested_mode != "multimodal":
                    raise DatasetValidationError("ZIP缺少data.yaml")
                archive_root = _infer_archive_root(raw_members)
                selected = str(secondary_modality or "").strip().lower()
                names = _clean_class_names(class_names)
                if not selected:
                    raise DatasetValidationError("ZIP缺少data.yaml，请选择本次使用的X模态")
                if not names:
                    raise DatasetValidationError("ZIP缺少data.yaml，请填写类别名称")
                data_yaml_member = f"{archive_root + '/' if archive_root else ''}data.yaml"
                data = {
                    "path": ".",
                    "train": "./images/train",
                    "val": "./images/val",
                    "modalities": {
                        "rgb": "./images",
                        selected: f"./images_{selected}",
                    },
                    "modality_used": ["rgb", selected],
                    "nc": len(names),
                    "names": names,
                }
            else:
                try:
                    data = yaml.safe_load(archive.read(data_yaml_member)) or {}
                except yaml.YAMLError as exc:
                    raise DatasetValidationError(f"data.yaml无法解析: {exc}") from exc
    except zipfile.BadZipFile as exc:
        raise DatasetValidationError("文件不是有效的ZIP归档") from exc

    if not generated_data_yaml:
        archive_root = str(PurePosixPath(data_yaml_member).parent)
        if archive_root == ".":
            archive_root = ""
    prefix = f"{archive_root}/" if archive_root else ""
    members = {
        member[len(prefix):]
        for member in raw_members
        if member.startswith(prefix)
    }

    modalities_config = data.get("modalities") or data.get("modality")
    yaml_is_multimodal = bool(modalities_config or data.get("modality_used"))
    if requested_mode == "single" and yaml_is_multimodal:
        raise DatasetValidationError("选择的是单模态，但data.yaml声明了多模态")
    if requested_mode == "multimodal" and not yaml_is_multimodal:
        raise DatasetValidationError("选择的是多模态，但data.yaml未声明modalities和modality_used")

    names = _class_names(data)
    secondary_modality = None
    secondary_directory = None
    if requested_mode == "multimodal":
        modalities = modalities_config
        used = data.get("modality_used")
        if not isinstance(modalities, dict) or not isinstance(used, list) or len(used) != 2:
            raise DatasetValidationError("多模态data.yaml要求modality_used按[rgb, X]配置")
        primary = str(used[0]).lower()
        secondary_modality = str(used[1]).lower()
        if primary != "rgb" or primary not in modalities or secondary_modality not in modalities:
            raise DatasetValidationError("modality_used必须以rgb为主模态，且两个模态都存在于modalities")
        rgb_directory = _directory_name(modalities[primary], "images")
        secondary_directory = _directory_name(
            modalities[secondary_modality], f"images_{secondary_modality}"
        )
    else:
        rgb_directory = "images"

    missing: list[str] = []
    split_counts: dict[str, int] = {}
    for split in SPLITS:
        rgb_keys = _sample_keys(members, rgb_directory, split, IMAGE_EXTENSIONS)
        label_keys = _sample_keys(members, "labels", split, {".txt"})
        if split in {"train", "val"} and not rgb_keys:
            missing.append(f"{rgb_directory}/{split}/*")
        for key in sorted(rgb_keys - label_keys):
            missing.append(f"labels/{split}/{key}.txt")
        if requested_mode == "multimodal":
            x_keys = _sample_keys(members, secondary_directory, split, IMAGE_EXTENSIONS)
            for key in sorted(rgb_keys - x_keys):
                missing.append(f"{secondary_directory}/{split}/{key}")
            for key in sorted(x_keys - rgb_keys):
                missing.append(f"{rgb_directory}/{split}/{key}")
        split_counts[split] = len(rgb_keys)

    if missing:
        preview = "、".join(missing[:20])
        if len(missing) > 20:
            preview += f" 等{len(missing)}项"
        raise DatasetValidationError(f"数据集文件或配对缺失: {preview}")

    _validate_yolo_labels(zip_path, archive_root, names)

    runtime_mapping = {
        "rgb_directory": rgb_directory,
        "secondary_directory": secondary_directory,
        "splits": split_counts,
    }
    return DatasetValidationResult(
        dataset_mode=requested_mode,
        primary_modality="rgb",
        secondary_modality=secondary_modality,
        secondary_directory=secondary_directory,
        names=names,
        split_counts=split_counts,
        missing_pairs=[],
        data_yaml_member=data_yaml_member,
        archive_root=archive_root,
        runtime_mapping=runtime_mapping,
        generated_data_yaml=generated_data_yaml,
    )


def inject_generated_data_yaml(zip_path: str, result: DatasetValidationResult) -> None:
    """Add the validated generated YAML to a temporary upload archive."""
    if not result.generated_data_yaml:
        return
    data: dict[str, Any] = {
        "path": ".",
        "train": f"./{result.runtime_mapping['rgb_directory']}/train",
        "val": f"./{result.runtime_mapping['rgb_directory']}/val",
        "modalities": {
            "rgb": f"./{result.runtime_mapping['rgb_directory']}",
            result.secondary_modality: f"./{result.secondary_directory}",
        },
        "modality_used": ["rgb", result.secondary_modality],
        "nc": len(result.names),
        "names": result.names,
    }
    if result.split_counts.get("test"):
        data["test"] = f"./{result.runtime_mapping['rgb_directory']}/test"
    content = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    with zipfile.ZipFile(zip_path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        if result.data_yaml_member in archive.namelist():
            raise DatasetValidationError("ZIP中的data.yaml已存在，无法生成配置")
        archive.writestr(result.data_yaml_member, content)


def safe_extract_dataset(zip_path: str, destination: str) -> str:
    destination_path = Path(destination).resolve()
    destination_path.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = archive.infolist()
            normalized = [_normalized_member(info.filename) for info in members if not info.is_dir()]
            data_yaml_member = _find_data_yaml(normalized)
            if data_yaml_member is None:
                raise DatasetValidationError("ZIP缺少data.yaml")
            for info in members:
                name = _normalized_member(info.filename)
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(unix_mode):
                    raise DatasetValidationError(f"ZIP包含不安全的符号链接: {name}")
                target = (destination_path / name).resolve()
                try:
                    target.relative_to(destination_path)
                except ValueError as exc:
                    raise DatasetValidationError(f"ZIP包含不安全路径: {name}") from exc
            archive.extractall(destination_path)
    except zipfile.BadZipFile as exc:
        raise DatasetValidationError("文件不是有效的ZIP归档") from exc

    archive_root = str(PurePosixPath(data_yaml_member).parent)
    return str((destination_path / ("" if archive_root == "." else archive_root)).resolve())


def build_runtime_yaml(
    extract_root: str,
    result: DatasetValidationResult,
    output_path: str,
) -> str:
    root = Path(extract_root).resolve()
    source_yaml = root / "data.yaml"
    data = yaml.safe_load(source_yaml.read_text(encoding="utf-8")) or {}
    data["path"] = str(root)
    data["train"] = f"./{result.runtime_mapping['rgb_directory']}/train"
    data["val"] = f"./{result.runtime_mapping['rgb_directory']}/val"
    if result.split_counts.get("test"):
        data["test"] = f"./{result.runtime_mapping['rgb_directory']}/test"
    else:
        data.pop("test", None)
    if result.dataset_mode == "multimodal":
        data.pop("modality", None)
        data["modalities"] = {
            "rgb": str((root / result.runtime_mapping["rgb_directory"]).resolve()),
            result.secondary_modality: str((root / result.secondary_directory).resolve()),
        }
        data["modality_used"] = ["rgb", result.secondary_modality]

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return str(destination)

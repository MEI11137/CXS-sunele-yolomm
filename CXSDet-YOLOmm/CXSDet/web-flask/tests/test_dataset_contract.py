import zipfile
from pathlib import Path

import pytest
import yaml


def _write_zip(path: Path, files: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def _single_files():
    return {
        "data.yaml": yaml.safe_dump(
            {
                "path": ".",
                "train": "./images/train",
                "val": "./images/val",
                "test": "./images/test",
                "nc": 1,
                "names": ["crack"],
            },
            allow_unicode=True,
        ),
        "images/train/a.jpg": b"train",
        "labels/train/a.txt": "0 0.5 0.5 0.2 0.2",
        "images/val/b.png": b"val",
        "labels/val/b.txt": "0 0.5 0.5 0.2 0.2",
        "images/test/c.jpg": b"test",
        "labels/test/c.txt": "0 0.5 0.5 0.2 0.2",
    }


def _multimodal_files(x_name="edge"):
    x_dir = f"images_{x_name}"
    return {
        "data.yaml": yaml.safe_dump(
            {
                "path": r"C:\invalid\on\server\dataset",
                "train": "./images/train",
                "val": "./images/val",
                "names": {0: "crack"},
                "modalities": {
                    "rgb": r"C:\source\images",
                    x_name: rf"C:\source\{x_dir}",
                },
                "modality_used": ["rgb", x_name],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        "images/train/a.jpg": b"rgb-train",
        f"{x_dir}/train/a.png": b"x-train",
        "labels/train/a.txt": "0 0.5 0.5 0.2 0.2",
        "images/val/sub/b.jpg": b"rgb-val",
        f"{x_dir}/val/sub/b.jpg": b"x-val",
        "labels/val/sub/b.txt": "0 0.5 0.5 0.2 0.2",
    }


def test_single_modal_archive_uses_images_split_layout(tmp_path):
    from services.dataset_contract import validate_dataset_archive

    result = validate_dataset_archive(
        str(_write_zip(tmp_path / "single.zip", _single_files())), "single"
    )

    assert result.dataset_mode == "single"
    assert result.primary_modality == "rgb"
    assert result.secondary_modality is None
    assert result.split_counts == {"train": 1, "val": 1, "test": 1}
    assert result.names == ["crack"]
    serialized = result.to_dict()
    assert serialized["train_images"] == 1
    assert serialized["valid_images"] == 1
    assert serialized["test_images"] == 1


def test_multimodal_archive_accepts_dynamic_x_and_relative_pairing(tmp_path):
    from services.dataset_contract import validate_dataset_archive

    result = validate_dataset_archive(
        str(_write_zip(tmp_path / "dual.zip", _multimodal_files("ir"))),
        "multimodal",
    )

    assert result.dataset_mode == "multimodal"
    assert result.secondary_modality == "ir"
    assert result.secondary_directory == "images_ir"
    assert result.split_counts == {"train": 1, "val": 1, "test": 0}
    assert result.missing_pairs == []


def test_multimodal_archive_accepts_legacy_modality_key(tmp_path):
    """The imported YOLOMM fork writes ``modality`` instead of ``modalities``."""
    from services.dataset_contract import validate_dataset_archive

    files = _multimodal_files("ir")
    data = yaml.safe_load(files["data.yaml"])
    data["modality"] = data.pop("modalities")
    files["data.yaml"] = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)

    result = validate_dataset_archive(
        str(_write_zip(tmp_path / "legacy-dual.zip", files)),
        "multimodal",
    )

    assert result.secondary_modality == "ir"
    assert result.secondary_directory == "images_ir"


def test_multimodal_archive_without_yaml_can_generate_selected_rgb_x_config(tmp_path):
    """A ZIP with RGB plus multiple X folders can be normalized from upload fields."""
    from services.dataset_contract import inject_generated_data_yaml, validate_dataset_archive

    files = _multimodal_files("edge")
    files.pop("data.yaml")
    files.update(
        {
            "images_depth/train/a.png": b"depth-train",
            "images_depth/val/sub/b.jpg": b"depth-val",
        }
    )
    archive = _write_zip(tmp_path / "no-yaml.zip", files)

    result = validate_dataset_archive(
        str(archive),
        "multimodal",
        secondary_modality="edge",
        class_names=["hotspot"],
    )

    assert result.generated_data_yaml is True
    assert result.secondary_modality == "edge"
    assert result.names == ["hotspot"]

    inject_generated_data_yaml(str(archive), result)
    normalized = validate_dataset_archive(str(archive), "multimodal")
    assert normalized.generated_data_yaml is False
    assert normalized.secondary_directory == "images_edge"
    assert normalized.names == ["hotspot"]


def test_multimodal_archive_reports_missing_x_pair(tmp_path):
    from services.dataset_contract import DatasetValidationError, validate_dataset_archive

    files = _multimodal_files("depth")
    files.pop("images_depth/val/sub/b.jpg")
    archive = _write_zip(tmp_path / "missing.zip", files)

    with pytest.raises(DatasetValidationError, match=r"images_depth/val/sub/b"):
        validate_dataset_archive(str(archive), "multimodal")


def test_requested_mode_must_match_yaml(tmp_path):
    from services.dataset_contract import DatasetValidationError, validate_dataset_archive

    archive = _write_zip(tmp_path / "mismatch.zip", _multimodal_files())
    with pytest.raises(DatasetValidationError, match="选择的是单模态"):
        validate_dataset_archive(str(archive), "single")


def test_archive_rejects_yolo_label_outside_class_mapping(tmp_path):
    from services.dataset_contract import DatasetValidationError, validate_dataset_archive

    files = _single_files()
    files["labels/train/a.txt"] = "1 0.5 0.5 0.2 0.2"
    archive = _write_zip(tmp_path / "bad-class.zip", files)

    with pytest.raises(DatasetValidationError):
        validate_dataset_archive(str(archive), "single")


def test_archive_accepts_empty_label_as_background_sample(tmp_path):
    from services.dataset_contract import validate_dataset_archive

    files = _single_files()
    files["labels/train/a.txt"] = ""
    archive = _write_zip(tmp_path / "background.zip", files)

    result = validate_dataset_archive(str(archive), "single")

    assert result.split_counts["train"] == 1


def test_safe_extract_rejects_path_traversal(tmp_path):
    from services.dataset_contract import DatasetValidationError, safe_extract_dataset

    archive = _write_zip(
        tmp_path / "evil.zip",
        {"data.yaml": "names: [x]", "../outside.txt": "escape"},
    )
    destination = tmp_path / "extract"

    with pytest.raises(DatasetValidationError, match="不安全"):
        safe_extract_dataset(str(archive), str(destination))
    assert not (tmp_path / "outside.txt").exists()


def test_runtime_yaml_rewrites_only_extracted_copy(tmp_path):
    from services.dataset_contract import (
        build_runtime_yaml,
        safe_extract_dataset,
        validate_dataset_archive,
    )

    archive = _write_zip(tmp_path / "dual.zip", _multimodal_files("edge"))
    result = validate_dataset_archive(str(archive), "multimodal")
    extract_root = safe_extract_dataset(str(archive), str(tmp_path / "extract"))
    runtime_path = build_runtime_yaml(
        extract_root, result, str(tmp_path / "runtime" / "data.runtime.yaml")
    )
    runtime = yaml.safe_load(Path(runtime_path).read_text(encoding="utf-8"))

    assert runtime["path"] == str(Path(extract_root).resolve())
    assert runtime["modalities"]["rgb"] == str(Path(extract_root, "images").resolve())
    assert runtime["modalities"]["edge"] == str(Path(extract_root, "images_edge").resolve())
    with zipfile.ZipFile(archive) as original:
        original_yaml = yaml.safe_load(original.read("data.yaml"))
    assert original_yaml["path"] == r"C:\invalid\on\server\dataset"

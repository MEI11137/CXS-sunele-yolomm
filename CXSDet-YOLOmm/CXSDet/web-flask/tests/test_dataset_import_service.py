"""C-03: dataset archive preview and registration."""

import io
import json
import sqlite3
import zipfile

import pytest
import yaml

from services.dataset_contract import DatasetValidationError, validate_dataset_archive
from services.dataset_import_service import (
    DatasetImportError,
    import_dataset_archive,
    inspect_dataset_archive,
)


def _zip(path, members: dict[str, bytes | str]):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return str(path)


def _single_archive(path, *, names=("crack", "hotspot"), extra=None):
    members = {
        "data.yaml": yaml.safe_dump({
            "path": ".", "train": "./images/train", "val": "./images/val",
            "names": list(names),
        }),
        "images/train/a.jpg": b"rgb",
        "labels/train/a.txt": "0 0.5 0.5 0.2 0.2\n1 0.4 0.4 0.1 0.1",
        "images/val/b.jpg": b"rgb",
        "labels/val/b.txt": "1 0.5 0.5 0.2 0.2",
    }
    members.update(extra or {})
    return _zip(path, members)


def _multimodal_archive(path, *, with_yaml=True, secondary="edge", val_pair=True):
    members = {}
    if with_yaml:
        members["data.yaml"] = yaml.safe_dump({
            "path": ".", "train": "./images/train", "val": "./images/val",
            "modalities": {"rgb": "./images", secondary: f"./images_{secondary}"},
            "modality_used": ["rgb", secondary],
            "names": ["crack"],
        })
    members.update({
        "images/train/a.jpg": b"rgb",
        f"images_{secondary}/train/a.png": b"x",
        "labels/train/a.txt": "0 0.5 0.5 0.2 0.2",
        "images/val/b.jpg": b"rgb",
        "labels/val/b.txt": "0 0.5 0.5 0.2 0.2",
    })
    if val_pair:
        members[f"images_{secondary}/val/b.png"] = b"x"
    return _zip(path, members)


class _Storage:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.deleted: list[tuple[str, str]] = []

    def upload(self, bucket, file_path):
        key = f"{len(self.objects)}.zip"
        with open(file_path, "rb") as source:
            self.objects[(bucket, key)] = source.read()
        return {"bucket": bucket, "objectKey": key}

    def delete(self, bucket, object_key):
        self.deleted.append((bucket, object_key))
        self.objects.pop((bucket, object_key), None)

    def get(self, bucket, object_key):
        return self.objects[(bucket, object_key)]


def _database(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE datasets (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
            zip_bucket TEXT NOT NULL, zip_object_key TEXT NOT NULL,
            structure_info TEXT, upload_time TEXT NOT NULL, file_size INTEGER,
            status INTEGER DEFAULT 0, dataset_mode TEXT NOT NULL DEFAULT 'single',
            primary_modality TEXT NOT NULL DEFAULT 'rgb', secondary_modality TEXT,
            runtime_mapping TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    return path


class _Handle:
    def __init__(self, path):
        self.path = str(path)

    def get_connection(self):
        return sqlite3.connect(self.path)


def test_valid_single_archive_preview_reports_real_classes_and_counts(tmp_path):
    path = _single_archive(tmp_path / "single.zip")
    preview = inspect_dataset_archive(path, "single")

    assert preview.ready_to_register is True
    assert preview.class_names == ["crack", "hotspot"]
    assert preview.split_counts == {"train": 1, "val": 1, "test": 0}
    assert preview.total_samples == 2
    assert preview.issues == []
    assert preview.runtime_mapping["rgb_directory"] == "images"


def test_missing_secondary_pair_blocks_multimodal_import(tmp_path):
    path = _multimodal_archive(tmp_path / "multi.zip", val_pair=False)
    preview = inspect_dataset_archive(path, "multimodal")

    assert preview.ready_to_register is False
    assert preview.secondary_modality == "edge"
    codes = {(issue.code, issue.path) for issue in preview.issues}
    assert ("MISSING_SECONDARY", "images_edge/val/b") in codes


def test_label_problems_are_listed_together(tmp_path):
    path = _single_archive(
        tmp_path / "bad-labels.zip",
        extra={
            "labels/train/a.txt": "7 0.5 0.5 0.2 0.2\n0 1.4 0.5 0.2 0.2\n0 0.5 0.5 0.2\n0.5 x 0.2 0.2 0.2",
        },
    )
    preview = inspect_dataset_archive(path, "single")
    codes = [issue.code for issue in preview.issues]

    assert preview.ready_to_register is False
    assert "LABEL_CLASS_RANGE" in codes
    assert "LABEL_COORDINATE" in codes
    assert "LABEL_FIELD_COUNT" in codes
    assert "LABEL_NOT_NUMERIC" in codes
    assert preview.blocking_total >= 4


def test_duplicate_class_names_block_registration(tmp_path):
    path = _single_archive(
        tmp_path / "dup-classes.zip",
        extra={"data.yaml": yaml.safe_dump({
            "path": ".", "train": "./images/train", "val": "./images/val",
            "names": ["crack", "crack"],
        })},
    )
    preview = inspect_dataset_archive(path, "single")

    assert preview.ready_to_register is False
    assert any(issue.code == "DUPLICATE_CLASS_NAMES" for issue in preview.issues)


def test_archive_path_traversal_is_reported_not_extracted(tmp_path):
    path = _zip(tmp_path / "escape.zip", {
        "data.yaml": yaml.safe_dump({"names": ["crack"]}),
        "../evil.jpg": b"x",
        "images/train/a.jpg": b"x",
        "labels/train/a.txt": "0 0.5 0.5 0.2 0.2",
    })
    preview = inspect_dataset_archive(path, "single")

    assert preview.ready_to_register is False
    assert any(issue.code == "UNSAFE_ENTRY" for issue in preview.issues)


def test_missing_data_yaml_requires_modality_and_classes(tmp_path):
    path = _multimodal_archive(tmp_path / "no-yaml.zip", with_yaml=False)

    blocked = inspect_dataset_archive(path, "multimodal")
    assert blocked.ready_to_register is False
    codes = {issue.code for issue in blocked.issues}
    assert {"MISSING_X_MODALITY", "MISSING_CLASS_NAMES"} <= codes

    ready = inspect_dataset_archive(
        path, "multimodal", secondary_modality="edge", class_names=["crack"],
    )
    assert ready.ready_to_register is True
    assert ready.generated_data_yaml is True
    assert ready.secondary_modality == "edge"


def test_orphan_label_is_a_warning_not_a_blocker(tmp_path):
    path = _single_archive(
        tmp_path / "orphan.zip",
        extra={"labels/val/ghost.txt": "0 0.5 0.5 0.2 0.2"},
    )
    preview = inspect_dataset_archive(path, "single")

    assert preview.ready_to_register is True
    assert [(issue.code, issue.severity) for issue in preview.issues] == [
        ("ORPHAN_LABEL", "warning"),
    ]


def test_not_a_zip_is_reported_as_one_stable_issue(tmp_path):
    path = tmp_path / "broken.zip"
    path.write_bytes(b"not a zip")
    preview = inspect_dataset_archive(str(path), "single")

    assert preview.ready_to_register is False
    assert [issue.code for issue in preview.issues] == ["BAD_ZIP"]
    assert preview.to_dict()["issue_counts"]["blocking"] == 1


@pytest.mark.parametrize(
    "builder, mode, kwargs",
    [
        (lambda p: _single_archive(p), "single", {}),
        (lambda p: _single_archive(p, names=("a",)), "single", {}),
        (lambda p: _multimodal_archive(p), "multimodal", {}),
        (lambda p: _multimodal_archive(p, val_pair=False), "multimodal", {}),
        (lambda p: _multimodal_archive(p, with_yaml=False), "multimodal",
         {"secondary_modality": "edge", "class_names": ["crack"]}),
        (lambda p: _single_archive(p, extra={"images/train/c.jpg": b"x"}), "single", {}),
    ],
)
def test_preview_agrees_with_the_authoritative_contract(tmp_path, builder, mode, kwargs):
    """The fix list must never claim an archive is importable when it is not."""
    path = builder(tmp_path / "archive.zip")
    preview = inspect_dataset_archive(path, mode, **kwargs)
    try:
        validate_dataset_archive(
            path, mode,
            secondary_modality=kwargs.get("secondary_modality"),
            class_names=kwargs.get("class_names"),
        )
        contract_ok = True
    except DatasetValidationError:
        contract_ok = False

    assert preview.ready_to_register is contract_ok


def test_import_registers_dataset_and_stores_validated_archive(tmp_path):
    path = _multimodal_archive(tmp_path / "generated.zip", with_yaml=False)
    storage = _Storage()
    database = _Handle(_database(tmp_path / "app.sqlite3"))

    record = import_dataset_archive(
        zip_path=path,
        name="rgb-edge-v1",
        description="demo",
        dataset_mode="multimodal",
        secondary_modality="edge",
        class_names=["crack"],
        database=database,
        storage_client=storage,
        project_id="project-1",
        owner_id=7,
    )

    assert record["class_names"] == ["crack"]
    assert record["total_samples"] == 2
    assert record["project_id"] == "project-1"
    bucket, key = record["storage"]["bucket"], record["storage"]["object_key"]
    assert bucket == "datasets"
    stored = storage.get(bucket, key)
    with zipfile.ZipFile(io.BytesIO(stored)) as archive:
        assert "data.yaml" in archive.namelist()
    with sqlite3.connect(database.path) as conn:
        row = conn.execute(
            "SELECT status, dataset_mode, secondary_modality, structure_info "
            "FROM datasets WHERE id=?", (record["dataset_id"],),
        ).fetchone()
    assert row[0] == 1
    assert (row[1], row[2]) == ("multimodal", "edge")
    assert json.loads(row[3])["names"] == ["crack"]


def test_import_rejects_invalid_archive_without_storing(tmp_path):
    path = _multimodal_archive(tmp_path / "broken.zip", val_pair=False)
    storage = _Storage()
    database = _Handle(_database(tmp_path / "app.sqlite3"))

    with pytest.raises(DatasetImportError) as excinfo:
        import_dataset_archive(
            zip_path=path, name="broken", dataset_mode="multimodal",
            database=database, storage_client=storage,
        )

    assert excinfo.value.code == "DATASET_INVALID"
    assert any(issue.code == "MISSING_SECONDARY" for issue in excinfo.value.issues)
    assert storage.objects == {}
    with sqlite3.connect(database.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 0


def test_import_rejects_duplicate_name_before_storing(tmp_path):
    path = _single_archive(tmp_path / "single.zip")
    storage = _Storage()
    database = _Handle(_database(tmp_path / "app.sqlite3"))
    args = dict(zip_path=path, dataset_mode="single", database=database,
                storage_client=storage)

    import_dataset_archive(name="twice", **args)
    with pytest.raises(DatasetImportError) as excinfo:
        import_dataset_archive(name="twice", **args)

    assert excinfo.value.code == "NAME_CONFLICT"
    assert len(storage.objects) == 1


def test_registration_hook_runs_inside_the_transaction(tmp_path):
    path = _single_archive(tmp_path / "single.zip")
    storage = _Storage()
    database = _Handle(_database(tmp_path / "app.sqlite3"))
    observed = []

    def failing_hook(conn, dataset_id):
        observed.append(conn.execute(
            "SELECT name FROM datasets WHERE id=?", (dataset_id,)
        ).fetchone()[0])
        raise RuntimeError("binding failed")

    with pytest.raises(DatasetImportError):
        import_dataset_archive(
            zip_path=path, name="atomic", dataset_mode="single",
            database=database, storage_client=storage, on_registered=failing_hook,
        )

    assert observed == ["atomic"]
    with sqlite3.connect(database.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 0
    assert storage.objects == {}

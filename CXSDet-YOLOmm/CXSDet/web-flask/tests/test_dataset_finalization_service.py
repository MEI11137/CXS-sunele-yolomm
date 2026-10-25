import json
import sqlite3
import uuid
import zipfile

import pytest


class FinalizationDatabase:
    def __init__(self):
        self.uri = f"file:finalizer-tests-{uuid.uuid4().hex}?mode=memory&cache=shared"
        self.anchor = sqlite3.connect(self.uri, uri=True)
        self.anchor.executescript(
            """
            CREATE TABLE datasets (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
                zip_bucket TEXT NOT NULL, zip_object_key TEXT NOT NULL,
                structure_info TEXT, upload_time TEXT NOT NULL, file_size INTEGER,
                status INTEGER NOT NULL, dataset_mode TEXT NOT NULL,
                primary_modality TEXT NOT NULL, secondary_modality TEXT,
                runtime_mapping TEXT, parent_dataset_id TEXT
            );
            CREATE TABLE dataset_drafts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
                creator_id INTEGER, source_type TEXT NOT NULL, source_dataset_id TEXT,
                video_detection_record_id TEXT, dataset_mode TEXT NOT NULL,
                primary_modality TEXT NOT NULL, secondary_modality TEXT,
                workflow_status TEXT NOT NULL, metadata_json TEXT NOT NULL,
                error_message TEXT, create_time TEXT NOT NULL, update_time TEXT NOT NULL,
                finalized_at TEXT
            );
            CREATE TABLE dataset_draft_samples (
                id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, sample_key TEXT NOT NULL,
                split TEXT NOT NULL, rgb_bucket TEXT, rgb_object_key TEXT,
                secondary_image_bucket TEXT, secondary_image_object_key TEXT,
                label_bucket TEXT, label_object_key TEXT, annotations_json TEXT NOT NULL,
                frame_index INTEGER, primary_timestamp REAL,
                secondary_frame_index INTEGER, secondary_timestamp REAL,
                pairing_error_ms REAL, pairing_state TEXT NOT NULL,
                create_time TEXT NOT NULL, update_time TEXT NOT NULL
            );
            CREATE UNIQUE INDEX uq_dataset_draft_name ON dataset_drafts(name);
            CREATE UNIQUE INDEX uq_dataset_draft_sample_key
                ON dataset_draft_samples(draft_id, sample_key);
            """
        )

    def get_connection(self):
        return sqlite3.connect(self.uri, uri=True)


class MemoryStorage:
    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.uploaded = []

    def get(self, bucket, object_key):
        return self.objects[(bucket, object_key)]

    def upload(self, bucket, path):
        object_key = f"archive-{len(self.objects)}.zip"
        self.objects[(bucket, object_key)] = open(path, "rb").read()
        self.uploaded.append((bucket, object_key))
        return {"bucket": bucket, "objectKey": object_key}

    def delete(self, bucket, object_key):
        self.deleted.append((bucket, object_key))
        self.objects.pop((bucket, object_key), None)


def _seed_draft(database, storage, *, mode="single", secondary=None):
    from services.dataset_draft_service import add_draft_sample, create_draft, mark_draft_ready

    create_draft(
        {
            "id": "draft-1",
            "name": "线路巡检",
            "description": "人工划分后的数据集",
            "creator_id": 1,
            "source_type": "video",
            "dataset_mode": mode,
            "primary_modality": "rgb",
            "secondary_modality": secondary,
            "metadata": {"class_names": ["裂纹"], "annotation_model_id": "model-1"},
        },
        database,
    )
    samples = [
        ("sample-a", "train", b"rgb-train", b"0 0.5 0.5 0.2 0.2\n"),
        ("sample-b", "val", b"rgb-val", b""),
    ]
    for sample_id, split, rgb, label in samples:
        storage.objects[("images", f"{sample_id}.jpg")] = rgb
        storage.objects[("labels", f"{sample_id}.txt")] = label
        secondary_key = f"{sample_id}.png" if secondary else None
        if secondary:
            storage.objects[(secondary, secondary_key)] = f"x-{split}".encode()
        add_draft_sample(
            "draft-1",
            {
                "id": sample_id,
                "sample_key": sample_id,
                "split": split,
                "rgb_bucket": "images",
                "rgb_object_key": f"{sample_id}.jpg",
                "secondary_image_bucket": secondary if secondary else None,
                "secondary_image_object_key": secondary_key,
                "label_bucket": "labels",
                "label_object_key": f"{sample_id}.txt",
                "annotations": [{"class_id": 0}] if label else [],
                "pairing_state": "paired" if secondary else "not_required",
                "frame_index": 0 if split == "train" else 2,
            },
            database,
        )
    mark_draft_ready("draft-1", database)


def _archive_members(storage, result):
    with zipfile.ZipFile(__import__("io").BytesIO(
        storage.get(result.zip_bucket, result.zip_object_key)
    )) as archive:
        return set(archive.namelist()), archive.read("data.yaml")


def test_single_modal_finalization_uses_only_assigned_samples(tmp_path):
    from services.dataset_finalization_service import finalize_draft

    database = FinalizationDatabase()
    storage = MemoryStorage()
    _seed_draft(database, storage)

    result = finalize_draft("draft-1", tmp_path, database, storage)

    members, _ = _archive_members(storage, result)
    assert "images/train/sample-a.jpg" in members
    assert "labels/train/sample-a.txt" in members
    assert "images/val/sample-b.jpg" in members
    assert "labels/val/sample-b.txt" in members
    assert "images/val/sample-a.jpg" not in members
    assert "images/test" not in members
    assert "data.yaml" in members
    assert "manifest.json" in members
    formal = database.get_connection().execute(
        "SELECT id, name, status FROM datasets"
    ).fetchone()
    assert formal == ("draft-1", "线路巡检", 1)
    draft = database.get_connection().execute(
        "SELECT workflow_status FROM dataset_drafts WHERE id='draft-1'"
    ).fetchone()
    assert draft == ("finalized",)


def test_multimodal_finalization_keeps_rgb_x_and_label_basenames_paired(tmp_path):
    from services.dataset_finalization_service import finalize_draft

    database = FinalizationDatabase()
    storage = MemoryStorage()
    _seed_draft(database, storage, mode="multimodal", secondary="thermal")

    result = finalize_draft("draft-1", tmp_path, database, storage)

    members, data_yaml = _archive_members(storage, result)
    assert "images_thermal/train/sample-a.png" in members
    assert "images_thermal/val/sample-b.png" in members
    assert "labels/val/sample-b.txt" in members
    data = __import__("yaml").safe_load(data_yaml)
    assert data["modality_used"] == ["rgb", "thermal"]


def test_finalization_deletes_uploaded_archive_if_database_insert_fails(tmp_path):
    from services.dataset_finalization_service import finalize_draft

    database = FinalizationDatabase()
    storage = MemoryStorage()
    _seed_draft(database, storage)
    with database.get_connection() as conn:
        conn.execute(
            "CREATE TRIGGER reject_dataset_insert BEFORE INSERT ON datasets "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        finalize_draft("draft-1", tmp_path, database, storage)

    assert storage.deleted == storage.uploaded
    assert storage.objects.get(storage.uploaded[0]) is None
    assert database.get_connection().execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 0
    assert database.get_connection().execute(
        "SELECT workflow_status FROM dataset_drafts WHERE id='draft-1'"
    ).fetchone()[0] == "draft"

import io
import json
import sqlite3

import pytest
from PIL import Image


def _png(color):
    stream = io.BytesIO()
    Image.new("RGB", (20, 10), color).save(stream, "PNG")
    return stream.getvalue()


def _database(tmp_path, status="confirmed"):
    path = tmp_path / "feedback.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE datasets (
            id TEXT PRIMARY KEY, name TEXT, dataset_mode TEXT,
            primary_modality TEXT, secondary_modality TEXT, runtime_mapping TEXT,
            description TEXT, zip_bucket TEXT, zip_object_key TEXT,
            structure_info TEXT, upload_time TEXT, file_size INTEGER,
            status INTEGER DEFAULT 1, parent_dataset_id TEXT
        );
        CREATE TABLE detection_records (
            id TEXT PRIMARY KEY, human_review_status TEXT,
            corrected_annotations TEXT, original_image_bucket TEXT,
            original_image_object_key TEXT, secondary_image_bucket TEXT,
            secondary_image_object_key TEXT, secondary_image_origin TEXT,
            feedback_dataset_id TEXT
        );
        CREATE TABLE feedback_samples (
            id TEXT PRIMARY KEY, record_id TEXT NOT NULL, dataset_id TEXT NOT NULL,
            annotation_hash TEXT NOT NULL, export_status TEXT DEFAULT 'pending',
            create_time TEXT NOT NULL
        );
        CREATE UNIQUE INDEX uq_feedback_record_annotation
            ON feedback_samples(record_id, annotation_hash);
        INSERT INTO datasets
            (id, name, dataset_mode, primary_modality, secondary_modality, runtime_mapping)
        VALUES ('d1', 'feedback', 'multimodal', 'rgb', 'edge', '{}');
        """
    )
    conn.execute(
        "INSERT INTO detection_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            "r1",
            status,
            json.dumps([{"class_name": "crack", "class_id": 1, "bbox": [0.1, 0.2, 0.7, 0.8]}]),
            "rgb",
            "sample.png",
            "edge",
            "sample.png",
            "generated",
        ),
    )
    conn.commit()
    conn.close()

    class Db:
        def get_connection(self):
            return sqlite3.connect(path)

    return Db(), path


class Storage:
    def __init__(self):
        self.uploaded = None

    def get(self, bucket, key):
        return _png((100, 100, 100) if bucket == "rgb" else (10, 10, 10))

    def upload(self, bucket, path):
        self.uploaded = (bucket, path)
        return {"bucket": bucket, "objectKey": "feedback.zip"}

    def delete(self, bucket, key):
        pass


def test_only_confirmed_records_can_be_added(tmp_path):
    from services.feedback_dataset_service import FeedbackValidationError, add_confirmed_record

    db, _ = _database(tmp_path, status="pending")
    with pytest.raises(FeedbackValidationError):
        add_confirmed_record("r1", "d1", db)


def test_feedback_sample_is_idempotent_by_record_and_annotation_hash(tmp_path):
    from services.feedback_dataset_service import add_confirmed_record

    db, path = _database(tmp_path)
    first = add_confirmed_record("r1", "d1", db)
    second = add_confirmed_record("r1", "d1", db)
    assert first.sample_id == second.sample_id
    assert second.created is False
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM feedback_samples").fetchone()[0] == 1
    conn.close()


def test_multimodal_export_writes_rgb_x_labels_and_origin_manifest(tmp_path):
    from services.feedback_dataset_service import add_confirmed_record, export_feedback_dataset

    db, _ = _database(tmp_path)
    add_confirmed_record("r1", "d1", db)
    result = export_feedback_dataset("d1", tmp_path / "export", db, Storage())

    assert result.sample_count == 1
    assert (tmp_path / "export/images/train/r1.png").exists()
    assert (tmp_path / "export/images_edge/train/r1.png").exists()
    label = (tmp_path / "export/labels/train/r1.txt").read_text(encoding="utf-8")
    assert label.strip() == "1 0.400000 0.500000 0.600000 0.600000"
    manifest = json.loads((tmp_path / "export/manifest.json").read_text(encoding="utf-8"))
    assert manifest["samples"][0]["secondary_origin"] == "generated"
    assert "images_edge" in (tmp_path / "export/data.yaml").read_text(encoding="utf-8")


def test_feedback_export_registers_a_new_immutable_dataset_version(tmp_path):
    from services.feedback_dataset_service import (
        add_confirmed_record,
        create_feedback_dataset_version,
    )

    db, path = _database(tmp_path)
    storage = Storage()
    add_confirmed_record("r1", "d1", db)
    result = create_feedback_dataset_version("d1", tmp_path / "version", db, storage)

    assert result.parent_dataset_id == "d1"
    assert result.dataset_id != "d1"
    assert storage.uploaded is not None
    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT parent_dataset_id, zip_object_key, dataset_mode, secondary_modality FROM datasets WHERE id=?",
        (result.dataset_id,),
    ).fetchone()
    conn.close()
    assert row == ("d1", "feedback.zip", "multimodal", "edge")

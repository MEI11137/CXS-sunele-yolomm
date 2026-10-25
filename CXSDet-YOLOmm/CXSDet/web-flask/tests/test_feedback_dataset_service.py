import io
import json
import sqlite3
import uuid

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
        self.objects = {}
        self.next_key = 0

    def get(self, bucket, key):
        return _png((100, 100, 100) if bucket == "rgb" else (10, 10, 10))

    def upload(self, bucket, path):
        self.next_key += 1
        object_key = f"generated-{self.next_key}.txt"
        self.objects[(bucket, object_key)] = open(path, "rb").read()
        self.uploaded = (bucket, object_key)
        return {"bucket": bucket, "objectKey": object_key}

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


def test_feedback_draft_reuses_confirmed_images_and_stores_unassigned_label(tmp_path):
    from services.feedback_dataset_service import add_confirmed_record, create_feedback_dataset_draft

    db, path = _database(tmp_path)
    storage = Storage()
    add_confirmed_record("r1", "d1", db)
    result = create_feedback_dataset_draft("d1", tmp_path / "draft", 7, db, storage)

    assert result.workflow_status == "draft"
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 1
    sample = conn.execute(
        "SELECT split, rgb_bucket, rgb_object_key, secondary_image_bucket, "
        "secondary_image_object_key, label_bucket, label_object_key, annotations_json "
        "FROM dataset_draft_samples WHERE draft_id=?",
        (result.id,),
    ).fetchone()
    conn.close()
    assert sample[:6] == (
        "unassigned", "rgb", "sample.png", "edge", "sample.png", "feedback-labels"
    )
    assert sample[6]
    assert json.loads(sample[7])[0]["class_id"] == 1
    assert storage.objects[("feedback-labels", sample[6])].decode().replace("\r\n", "\n") == (
        "1 0.400000 0.500000 0.600000 0.600000\n"
    )
    assert result.source_dataset_id == "d1"
    assert result.dataset_mode == "multimodal"
    assert result.secondary_modality == "edge"


def test_feedback_draft_preserves_missing_secondary_for_manual_repair(tmp_path):
    from services.feedback_dataset_service import (
        add_confirmed_record,
        create_feedback_dataset_draft,
    )

    db, path = _database(tmp_path)
    storage = Storage()
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE detection_records SET secondary_image_bucket=NULL, "
            "secondary_image_object_key=NULL WHERE id='r1'"
        )
    add_confirmed_record("r1", "d1", db)
    result = create_feedback_dataset_draft("d1", tmp_path / "draft", 7, db, storage)

    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT split, pairing_state, secondary_image_object_key FROM dataset_draft_samples WHERE draft_id=?",
        (result.id,),
    ).fetchone()
    conn.close()
    assert row == ("unassigned", "missing", None)


def test_feedback_draft_uses_only_confirmed_corrections(tmp_path):
    from services.feedback_dataset_service import FeedbackValidationError, create_feedback_dataset_draft

    db, _ = _database(tmp_path, status="pending")
    with pytest.raises(FeedbackValidationError):
        create_feedback_dataset_draft("d1", tmp_path / "draft", 7, db, Storage())

import json
import sqlite3
import zipfile

import cv2
import numpy as np


class SQLiteDatabase:
    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as conn:
            conn.executescript(
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
                CREATE UNIQUE INDEX uq_dataset_draft_sample_key ON dataset_draft_samples(draft_id, sample_key);
                CREATE INDEX ix_dataset_draft_sample_split ON dataset_draft_samples(draft_id, split);
                """
            )

    def get_connection(self):
        return sqlite3.connect(self.path)


class LocalStorage:
    def __init__(self):
        self.objects = {}
        self.next_id = 0

    def upload(self, bucket, path):
        self.next_id += 1
        key = f"object-{self.next_id}{'.zip' if bucket == 'datasets' else ''}"
        with open(path, "rb") as file:
            self.objects[(bucket, key)] = file.read()
        return {"bucket": bucket, "objectKey": key}

    def get(self, bucket, key):
        return self.objects[(bucket, key)]

    def delete(self, bucket, key):
        self.objects.pop((bucket, key), None)


def _write_video(path, fps, count, base_color):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (32, 24))
    assert writer.isOpened(), "MJPG video writer is required for synthetic integration fixtures"
    for index in range(count):
        frame = np.full((24, 32, 3), (base_color + index) % 255, dtype=np.uint8)
        writer.write(frame)
    writer.release()


def test_synthetic_videos_generate_pair_split_finalize_without_fake_validation_copy(tmp_path, monkeypatch):
    from services import yolo_runtime
    from services.dataset_draft_service import assign_draft_samples, create_draft
    from services.dataset_finalization_service import finalize_draft
    from services.video_dataset_service import VideoDatasetConfig, generate_video_dataset

    class Detector:
        names = {0: "目标"}

    class Prediction:
        detections = []

    monkeypatch.setattr(yolo_runtime, "load_detector", lambda *_args: Detector())
    monkeypatch.setattr(yolo_runtime, "resolve_inference_inputs", lambda *_args: {})
    monkeypatch.setattr(yolo_runtime, "predict_image", lambda *_args: Prediction())

    rgb_path = tmp_path / "rgb.avi"
    x_path = tmp_path / "thermal.avi"
    _write_video(rgb_path, fps=4, count=4, base_color=20)
    _write_video(x_path, fps=2, count=2, base_color=90)
    database = SQLiteDatabase(tmp_path / "workflow.sqlite")
    storage = LocalStorage()
    storage.objects[("models", "annotation.pt")] = b"synthetic model bytes"

    create_draft({
        "id": "paired-draft", "name": "合成多模态样本", "source_type": "video",
        "dataset_mode": "multimodal", "primary_modality": "rgb", "secondary_modality": "thermal",
        "metadata": {"annotation_model_id": "synthetic"},
    }, database)
    result = generate_video_dataset(
        "paired-draft",
        VideoDatasetConfig(enabled=True, mode="multimodal", frame_interval=1,
                           annotation_model_id="synthetic", secondary_modality="thermal",
                           secondary_time_offset=1.25),
        {"primary_video": rgb_path, "secondary_video": x_path, "work_dir": tmp_path / "work-1",
         "model_meta": {"model_mode": "multimodal", "secondary_modality": "thermal",
                        "model_bucket": "models", "model_object_key": "annotation.pt"}},
        database, storage,
    )
    assert result["extracted"] == 4
    assert result["paired"] == 2
    assert result["missing_secondary"] == 2
    with database.get_connection() as conn:
        samples = conn.execute(
            "SELECT id, split, pairing_state, annotations_json FROM dataset_draft_samples "
            "WHERE draft_id='paired-draft' ORDER BY frame_index"
        ).fetchall()
    assert len(samples) == 4
    assert [row[2] for row in samples] == ["paired", "paired", "missing", "missing"]
    assert [json.loads(row[3]) for row in samples] == [[], [], [], []]

    # N=2 is checked against a second actual video pass: 4 source frames yield indices 0 and 2.
    create_draft({
        "id": "interval-draft", "name": "间隔抽帧样本", "source_type": "video",
        "dataset_mode": "single", "primary_modality": "rgb", "metadata": {},
    }, database)
    interval_result = generate_video_dataset(
        "interval-draft",
        VideoDatasetConfig(enabled=True, mode="single", frame_interval=2, annotation_model_id="synthetic"),
        {"primary_video": rgb_path, "work_dir": tmp_path / "work-2",
         "model_meta": {"model_mode": "single", "model_bucket": "models", "model_object_key": "annotation.pt"}},
        database, storage,
    )
    assert interval_result["extracted"] == 2
    with database.get_connection() as conn:
        indices = [row[0] for row in conn.execute(
            "SELECT frame_index FROM dataset_draft_samples WHERE draft_id='interval-draft' ORDER BY frame_index"
        )]
    assert indices == [0, 2]

    assign_draft_samples("paired-draft", [samples[0][0]], "train", database)
    assign_draft_samples("paired-draft", [samples[1][0]], "val", database)
    assign_draft_samples("paired-draft", [samples[2][0], samples[3][0]], "excluded", database)
    finalized = finalize_draft("paired-draft", tmp_path / "archives", database, storage)
    archive_bytes = storage.get(finalized.zip_bucket, finalized.zip_object_key)
    with zipfile.ZipFile(__import__("io").BytesIO(archive_bytes)) as archive:
        members = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
    assert "images/train/frame_00000000.jpg" in members
    assert "images/val/frame_00000001.jpg" in members
    assert "images_thermal/train/frame_00000000.jpg" in members
    assert "images/val/frame_00000000.jpg" not in members
    assert "images/train/frame_00000002.jpg" not in members
    assert len(manifest["samples"]) == 2

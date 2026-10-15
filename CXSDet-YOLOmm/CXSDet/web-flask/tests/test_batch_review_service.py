import json
import sqlite3

import pytest


def _database(tmp_path):
    path = tmp_path / "batch.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE datasets (
            id TEXT PRIMARY KEY, name TEXT, dataset_mode TEXT,
            primary_modality TEXT, secondary_modality TEXT
        );
        CREATE TABLE models (
            id TEXT PRIMARY KEY, dataset_id TEXT
        );
        CREATE TABLE detection_records (
            id TEXT PRIMARY KEY, user_id INTEGER, model_id TEXT,
            human_review_status TEXT, corrected_annotations TEXT,
            feedback_dataset_id TEXT
        );
        CREATE TABLE feedback_samples (
            id TEXT PRIMARY KEY, record_id TEXT NOT NULL, dataset_id TEXT NOT NULL,
            annotation_hash TEXT NOT NULL, export_status TEXT DEFAULT 'pending',
            create_time TEXT NOT NULL
        );
        CREATE UNIQUE INDEX uq_feedback_record_annotation
            ON feedback_samples(record_id, annotation_hash);
        INSERT INTO datasets VALUES ('d1', '原训练集', 'single', 'rgb', NULL);
        INSERT INTO models VALUES ('m1', 'd1');
        INSERT INTO models VALUES ('m-orphan', NULL);
        """
    )
    annotation = json.dumps(
        [{"class_name": "crack", "class_id": 0, "bbox": [0.1, 0.2, 0.7, 0.8]}]
    )
    conn.executemany(
        "INSERT INTO detection_records VALUES (?, ?, ?, ?, ?, NULL)",
        [
            ("r1", 7, "m1", "confirmed", annotation),
            ("r2", 7, "m1", "pending", annotation),
            ("r3", 8, "m1", "confirmed", annotation),
            ("r4", 7, "m-orphan", "confirmed", annotation),
        ],
    )
    conn.commit()
    conn.close()

    class Db:
        def get_connection(self):
            return sqlite3.connect(path)

    return Db(), path


def test_batch_ai_review_deduplicates_and_reports_per_item_failures(tmp_path):
    from services.batch_review_service import run_batch_ai_reviews

    db, _ = _database(tmp_path)
    calls = []

    class Review:
        def __init__(self, record_id):
            self.record_id = record_id

        def to_dict(self):
            return {"record_id": self.record_id, "status": "consistent"}

    def runner(record_id, client, database, storage):
        calls.append(record_id)
        return Review(record_id)

    result = run_batch_ai_reviews(
        ["r1", "r1", "r3", "missing"],
        user_id=7,
        is_admin_user=False,
        database=db,
        storage_client=object(),
        review_runner=runner,
        client_factory=lambda: object(),
    )

    assert calls == ["r1"]
    assert result["requested"] == 4
    assert result["unique"] == 3
    assert result["succeeded"] == 1
    assert result["failed"] == 2
    assert [item["record_id"] for item in result["items"]] == ["r1", "r3", "missing"]
    assert result["items"][1]["status"] == "failed"
    assert result["items"][1]["error"] == "识别记录不存在或无权访问"


def test_batch_ai_review_limits_request_size(tmp_path):
    from services.batch_review_service import BatchValidationError, run_batch_ai_reviews

    db, _ = _database(tmp_path)
    with pytest.raises(BatchValidationError, match="最多批量处理 50 条"):
        run_batch_ai_reviews(
            [f"r{index}" for index in range(51)],
            user_id=7,
            is_admin_user=True,
            database=db,
            storage_client=object(),
            review_runner=lambda *args: None,
            client_factory=lambda: object(),
        )


def test_batch_ai_review_reports_typed_failure_and_continues(tmp_path):
    from services.ai_review_service import AIReviewUpstreamError

    from services.batch_review_service import run_batch_ai_reviews

    db, _ = _database(tmp_path)

    class Review:
        def to_dict(self):
            return {"status": "consistent"}

    def runner(record_id, client, database, storage):
        if record_id == "r1":
            raise AIReviewUpstreamError("provider detail must stay private")
        return Review()

    result = run_batch_ai_reviews(
        ["r1", "r2"],
        user_id=7,
        is_admin_user=False,
        database=db,
        storage_client=object(),
        review_runner=runner,
        client_factory=lambda: object(),
    )

    assert result["succeeded"] == 1
    assert result["failed"] == 1
    assert result["items"][0] == {
        "record_id": "r1",
        "status": "failed",
        "error": "AI服务调用失败，需要人工复核",
    }
    assert result["items"][1]["record_id"] == "r2"
    assert result["items"][1]["status"] == "success"


def test_batch_feedback_uses_each_models_source_dataset_and_is_idempotent(tmp_path):
    from services.batch_review_service import add_batch_confirmed_records

    db, path = _database(tmp_path)
    first = add_batch_confirmed_records(["r1", "r1"], 7, False, db)
    second = add_batch_confirmed_records(["r1"], 7, False, db)

    assert first["requested"] == 2
    assert first["unique"] == 1
    assert first["items"][0]["dataset_id"] == "d1"
    assert first["items"][0]["dataset_name"] == "原训练集"
    assert first["items"][0]["created"] is True
    assert second["items"][0]["created"] is False
    assert first["dataset_groups"] == [
        {"dataset_id": "d1", "dataset_name": "原训练集", "record_ids": ["r1"]}
    ]
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM feedback_samples").fetchone()[0] == 1
    assert conn.execute(
        "SELECT feedback_dataset_id FROM detection_records WHERE id='r1'"
    ).fetchone()[0] == "d1"
    conn.close()


def test_batch_feedback_keeps_processing_after_unconfirmed_and_missing_source(tmp_path):
    from services.batch_review_service import add_batch_confirmed_records

    db, _ = _database(tmp_path)
    result = add_batch_confirmed_records(["r2", "r4", "r3", "r1"], 7, False, db)

    assert result["succeeded"] == 1
    assert result["failed"] == 3
    by_id = {item["record_id"]: item for item in result["items"]}
    assert by_id["r2"]["error"] == "只有人工确认的记录才能加入反馈数据集"
    assert by_id["r4"]["error"] == "检测模型没有关联原训练数据集"
    assert by_id["r3"]["error"] == "识别记录不存在或无权访问"
    assert by_id["r1"]["status"] == "success"


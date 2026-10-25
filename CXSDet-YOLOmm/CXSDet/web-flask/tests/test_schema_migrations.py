import sqlite3


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_legacy_schema_is_upgraded_idempotently_without_data_loss(tmp_path):
    from utils.schema_migrations import run_schema_migrations

    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE datasets (id TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE models (id TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE video_detection_records (
            id TEXT PRIMARY KEY, user_id INTEGER, model_id TEXT
        );
        CREATE TABLE detection_records (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            model_id TEXT NOT NULL,
            model_name TEXT NOT NULL,
            original_image_bucket TEXT NOT NULL,
            original_image_object_key TEXT NOT NULL,
            create_time TEXT NOT NULL
        );
        INSERT INTO datasets (id, name) VALUES ('dataset-1', 'legacy dataset');
        INSERT INTO models (id, name) VALUES ('model-1', 'legacy model');
        INSERT INTO detection_records (
            id, user_id, model_id, model_name,
            original_image_bucket, original_image_object_key, create_time
        ) VALUES ('record-1', 1, 'model-1', 'legacy model', 'images', 'a.jpg', '2026-10-14');
        """
    )

    run_schema_migrations(conn)
    run_schema_migrations(conn)

    assert conn.execute("SELECT name FROM datasets WHERE id='dataset-1'").fetchone()[0] == "legacy dataset"
    assert conn.execute("SELECT name FROM models WHERE id='model-1'").fetchone()[0] == "legacy model"
    assert conn.execute("SELECT model_name FROM detection_records WHERE id='record-1'").fetchone()[0] == "legacy model"

    assert {
        "dataset_mode",
        "primary_modality",
        "secondary_modality",
        "runtime_mapping",
        "parent_dataset_id",
    } <= _columns(conn, "datasets")
    assert {
        "model_mode",
        "primary_modality",
        "secondary_modality",
        "model_yaml",
        "parent_model_id",
        "version",
        "metrics_json",
        "publish_status",
    } <= _columns(conn, "models")
    assert {
        "record_status",
        "secondary_image_bucket",
        "secondary_image_object_key",
        "secondary_image_origin",
        "ai_review_status",
        "ai_review_result",
        "human_review_status",
        "corrected_annotations",
        "reviewed_by",
        "reviewed_at",
        "feedback_dataset_id",
        "review_version",
    } <= _columns(conn, "detection_records")
    assert {
        "dataset_generation_status",
        "dataset_draft_id",
        "dataset_statistics",
    } <= _columns(conn, "video_detection_records")

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "detection_record_reviews" in tables
    assert "feedback_samples" in tables
    assert "model_publish_audit" in tables
    assert "dataset_drafts" in tables
    assert "dataset_draft_samples" in tables

    assert {
        "id",
        "name",
        "creator_id",
        "source_type",
        "source_dataset_id",
        "video_detection_record_id",
        "dataset_mode",
        "primary_modality",
        "secondary_modality",
        "workflow_status",
        "metadata_json",
        "error_message",
        "create_time",
        "update_time",
        "finalized_at",
    } <= _columns(conn, "dataset_drafts")
    assert {
        "id",
        "draft_id",
        "sample_key",
        "split",
        "rgb_bucket",
        "rgb_object_key",
        "secondary_image_bucket",
        "secondary_image_object_key",
        "label_bucket",
        "label_object_key",
        "annotations_json",
        "frame_index",
        "primary_timestamp",
        "secondary_frame_index",
        "secondary_timestamp",
        "pairing_error_ms",
        "pairing_state",
        "create_time",
        "update_time",
    } <= _columns(conn, "dataset_draft_samples")

    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(feedback_samples)")
    }
    assert "uq_feedback_record_annotation" in indexes

    draft_indexes = {
        row[1] for row in conn.execute("PRAGMA index_list(dataset_drafts)")
    }
    assert "uq_dataset_draft_name" in draft_indexes

    sample_indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(dataset_draft_samples)")
    }
    assert "uq_dataset_draft_sample_key" in sample_indexes
    assert "ix_dataset_draft_sample_split" in sample_indexes
    conn.close()

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

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "detection_record_reviews" in tables
    assert "feedback_samples" in tables
    assert "model_publish_audit" in tables

    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(feedback_samples)")
    }
    assert "uq_feedback_record_annotation" in indexes
    conn.close()

"""Small, repeatable SQLite migrations for installations created by older builds."""

from __future__ import annotations

import sqlite3


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    ddl: str,
) -> None:
    if column not in _column_names(conn, table):
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}')


def ensure_index(conn: sqlite3.Connection, name: str, ddl: str) -> None:
    existing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    if not existing:
        conn.execute(ddl)


def run_schema_migrations(conn: sqlite3.Connection) -> None:
    """Upgrade existing databases in place without replacing their data."""
    dataset_columns = {
        "dataset_mode": "TEXT NOT NULL DEFAULT 'single'",
        "primary_modality": "TEXT NOT NULL DEFAULT 'rgb'",
        "secondary_modality": "TEXT",
        "runtime_mapping": "TEXT",
        "parent_dataset_id": "TEXT",
    }
    model_columns = {
        "model_mode": "TEXT NOT NULL DEFAULT 'single'",
        "primary_modality": "TEXT NOT NULL DEFAULT 'rgb'",
        "secondary_modality": "TEXT",
        "model_yaml": "TEXT",
        "parent_model_id": "TEXT",
        "version": "INTEGER NOT NULL DEFAULT 1",
        "metrics_json": "TEXT",
        "publish_status": "TEXT NOT NULL DEFAULT 'draft'",
    }
    detection_columns = {
        "record_status": "TEXT NOT NULL DEFAULT 'completed'",
        "secondary_image_bucket": "TEXT",
        "secondary_image_object_key": "TEXT",
        "secondary_image_origin": "TEXT",
        "ai_review_status": "TEXT NOT NULL DEFAULT 'not_requested'",
        "ai_review_result": "TEXT",
        "human_review_status": "TEXT NOT NULL DEFAULT 'pending'",
        "corrected_annotations": "TEXT",
        "reviewed_by": "INTEGER",
        "reviewed_at": "TEXT",
        "feedback_dataset_id": "TEXT",
        "review_version": "INTEGER NOT NULL DEFAULT 0",
        "request_key": "TEXT",
    }
    video_detection_columns = {
        "dataset_generation_status": "TEXT NOT NULL DEFAULT 'not_requested'",
        "dataset_draft_id": "TEXT",
        "dataset_statistics": "TEXT",
    }

    with conn:
        for name, ddl in dataset_columns.items():
            ensure_column(conn, "datasets", name, ddl)
        for name, ddl in model_columns.items():
            ensure_column(conn, "models", name, ddl)
        for name, ddl in detection_columns.items():
            ensure_column(conn, "detection_records", name, ddl)
        for name, ddl in video_detection_columns.items():
            ensure_column(conn, "video_detection_records", name, ddl)

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS detection_record_reviews (
                id TEXT PRIMARY KEY,
                record_id TEXT NOT NULL,
                reviewer_id INTEGER NOT NULL,
                decision TEXT NOT NULL,
                annotations TEXT NOT NULL,
                previous_version INTEGER NOT NULL,
                new_version INTEGER NOT NULL,
                create_time TEXT NOT NULL,
                FOREIGN KEY (record_id) REFERENCES detection_records (id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback_samples (
                id TEXT PRIMARY KEY,
                record_id TEXT NOT NULL,
                dataset_id TEXT NOT NULL,
                annotation_hash TEXT NOT NULL,
                export_status TEXT NOT NULL DEFAULT 'pending',
                create_time TEXT NOT NULL,
                FOREIGN KEY (record_id) REFERENCES detection_records (id),
                FOREIGN KEY (dataset_id) REFERENCES datasets (id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_publish_audit (
                id TEXT PRIMARY KEY,
                model_id TEXT NOT NULL,
                operator_id INTEGER NOT NULL,
                previous_model_id TEXT,
                create_time TEXT NOT NULL,
                FOREIGN KEY (model_id) REFERENCES models (id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dataset_drafts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                creator_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                source_dataset_id TEXT,
                video_detection_record_id TEXT,
                dataset_mode TEXT NOT NULL DEFAULT 'single',
                primary_modality TEXT NOT NULL DEFAULT 'rgb',
                secondary_modality TEXT,
                workflow_status TEXT NOT NULL DEFAULT 'generating',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                error_message TEXT,
                create_time TEXT NOT NULL,
                update_time TEXT NOT NULL,
                finalized_at TEXT,
                FOREIGN KEY (creator_id) REFERENCES user (id),
                FOREIGN KEY (source_dataset_id) REFERENCES datasets (id),
                FOREIGN KEY (video_detection_record_id)
                    REFERENCES video_detection_records (id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dataset_draft_samples (
                id TEXT PRIMARY KEY,
                draft_id TEXT NOT NULL,
                sample_key TEXT NOT NULL,
                split TEXT NOT NULL DEFAULT 'unassigned',
                rgb_bucket TEXT,
                rgb_object_key TEXT,
                secondary_image_bucket TEXT,
                secondary_image_object_key TEXT,
                label_bucket TEXT,
                label_object_key TEXT,
                annotations_json TEXT NOT NULL DEFAULT '[]',
                frame_index INTEGER,
                primary_timestamp REAL,
                secondary_frame_index INTEGER,
                secondary_timestamp REAL,
                pairing_error_ms REAL,
                pairing_state TEXT NOT NULL DEFAULT 'not_required',
                create_time TEXT NOT NULL,
                update_time TEXT NOT NULL,
                FOREIGN KEY (draft_id) REFERENCES dataset_drafts (id)
                    ON DELETE CASCADE
            )
            """
        )
        ensure_index(
            conn,
            "uq_feedback_record_annotation",
            "CREATE UNIQUE INDEX uq_feedback_record_annotation "
            "ON feedback_samples(record_id, annotation_hash)",
        )
        ensure_index(
            conn,
            "uq_detection_request_key",
            "CREATE UNIQUE INDEX uq_detection_request_key "
            "ON detection_records(request_key) WHERE request_key IS NOT NULL",
        )
        ensure_index(
            conn,
            "uq_dataset_draft_name",
            "CREATE UNIQUE INDEX uq_dataset_draft_name ON dataset_drafts(name)",
        )
        ensure_index(
            conn,
            "uq_dataset_draft_sample_key",
            "CREATE UNIQUE INDEX uq_dataset_draft_sample_key "
            "ON dataset_draft_samples(draft_id, sample_key)",
        )
        ensure_index(
            conn,
            "ix_dataset_draft_sample_split",
            "CREATE INDEX ix_dataset_draft_sample_split "
            "ON dataset_draft_samples(draft_id, split)",
        )

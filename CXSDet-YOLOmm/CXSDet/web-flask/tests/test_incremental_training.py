import sqlite3

import pytest


def _db(tmp_path, dataset_mode="multimodal"):
    path = tmp_path / "incremental.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE datasets (
            id TEXT PRIMARY KEY, dataset_mode TEXT, primary_modality TEXT,
            secondary_modality TEXT
        );
        CREATE TABLE models (
            id TEXT PRIMARY KEY, name TEXT, description TEXT, dataset_id TEXT,
            base_model TEXT, epochs INTEGER, batch_size INTEGER, status INTEGER,
            model_bucket TEXT, model_object_key TEXT, train_result TEXT,
            create_time TEXT, start_time TEXT, complete_time TEXT,
            model_mode TEXT, primary_modality TEXT, secondary_modality TEXT,
            model_yaml TEXT, parent_model_id TEXT, version INTEGER,
            metrics_json TEXT, publish_status TEXT
        );
        INSERT INTO datasets VALUES ('feedback', 'multimodal', 'rgb', 'edge');
        INSERT INTO models VALUES (
            'parent', 'dual base', '', 'feedback', 'yolo11n.pt', 10, 4, 3,
            'models', 'parent.pt', NULL, '2026-01-01', NULL, NULL,
            'multimodal', 'rgb', 'edge', 'mm.yaml', NULL, 1, '{}', 'published'
        );
        """
    )
    if dataset_mode != "multimodal":
        conn.execute("UPDATE datasets SET dataset_mode='single', secondary_modality=NULL")
    conn.commit()
    conn.close()

    class Db:
        def get_connection(self):
            return sqlite3.connect(path)

    return Db(), path


def test_incremental_training_creates_child_without_mutating_parent(tmp_path):
    from services.model_registry_service import start_incremental_training

    db, path = _db(tmp_path)
    child = start_incremental_training(
        "parent", "feedback", {"epochs": 2, "batch_size": 1}, db
    )
    conn = sqlite3.connect(path)
    parent = conn.execute("SELECT status, model_object_key FROM models WHERE id='parent'").fetchone()
    row = conn.execute(
        "SELECT parent_model_id, version, status, model_object_key, epochs FROM models WHERE id=?",
        (child.model_id,),
    ).fetchone()
    conn.close()
    assert parent == (3, "parent.pt")
    assert row == ("parent", 2, 0, None, 2)


def test_incremental_training_rejects_modality_mismatch(tmp_path):
    from services.model_registry_service import ModelRegistryError, start_incremental_training

    db, _ = _db(tmp_path, dataset_mode="single")
    with pytest.raises(ModelRegistryError):
        start_incremental_training("parent", "feedback", {}, db)

import json
import sqlite3

import pytest


def _db(tmp_path):
    path = tmp_path / "registry.db"
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
        CREATE TABLE model_publish_audit (
            id TEXT PRIMARY KEY, model_id TEXT, operator_id INTEGER,
            previous_model_id TEXT, create_time TEXT
        );
        INSERT INTO datasets VALUES ('d1', 'single', 'rgb', NULL);
        INSERT INTO models VALUES (
            'parent', 'base', '', 'd1', 'yolo11n.pt', 1, 1, 3,
            'models', 'base.pt', NULL, '2026-01-01', NULL, NULL,
            'single', 'rgb', NULL, NULL, NULL, 1,
            '{"metrics/mAP50(B)": 0.5, "metrics/precision(B)": 0.7}', 'published'
        );
        INSERT INTO models VALUES (
            'child', 'base v2', '', 'd1', 'yolo11n.pt', 1, 1, 1,
            'models', 'child.pt', NULL, '2026-01-02', NULL, NULL,
            'single', 'rgb', NULL, NULL, 'parent', 2,
            '{"metrics/mAP50(B)": 0.6, "metrics/recall(B)": 0.8}', 'draft'
        );
        """
    )
    conn.commit()
    conn.close()

    class Db:
        def get_connection(self):
            return sqlite3.connect(path)

    return Db(), path


def test_compare_models_preserves_unknown_instead_of_zero(tmp_path):
    from services.model_registry_service import compare_models

    db, _ = _db(tmp_path)
    comparison = compare_models("parent", "child", db).to_dict()
    assert comparison["metrics"]["mAP50"]["delta"] == pytest.approx(0.1)
    assert comparison["metrics"]["precision"]["child"] is None
    assert comparison["metrics"]["precision"]["status"] == "unknown"
    assert comparison["metrics"]["recall"]["parent"] is None


def test_publish_atomically_archives_previous_active_model(tmp_path):
    from services.model_registry_service import publish_model

    db, path = _db(tmp_path)
    result = publish_model("child", 7, db, scope_model_ids=["parent", "child"])
    assert result.previous_model_id == "parent"
    conn = sqlite3.connect(path)
    states = dict(conn.execute("SELECT id, status FROM models").fetchall())
    labels = dict(conn.execute("SELECT id, publish_status FROM models").fetchall())
    audit = conn.execute("SELECT model_id, operator_id, previous_model_id FROM model_publish_audit").fetchone()
    conn.close()
    assert states == {"parent": 1, "child": 3}
    assert labels == {"parent": "archived", "child": "published"}
    assert audit == ("child", 7, "parent")


def test_failed_or_weightless_model_cannot_be_published(tmp_path):
    from services.model_registry_service import ModelRegistryError, publish_model

    db, path = _db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE models SET status=2, model_object_key=NULL WHERE id='child'")
    conn.commit()
    conn.close()
    with pytest.raises(ModelRegistryError):
        publish_model("child", 7, db, scope_model_ids=["parent", "child"])


def test_publish_does_not_archive_models_outside_the_authorised_scope(tmp_path):
    """发布只归档真实验权范围（本人同一项目）内的模型，别的模型必须保持发布。"""
    from services.model_registry_service import publish_model

    db, path = _db(tmp_path)
    # 父模型不在本次核准范围内（例如属于另一项目/另一用户）
    result = publish_model("child", 7, db, scope_model_ids=["child"])
    assert result.previous_model_id is None
    assert result.archived_model_ids == []
    conn = sqlite3.connect(path)
    states = dict(conn.execute("SELECT id, status FROM models").fetchall())
    conn.close()
    assert states == {"parent": 3, "child": 3}

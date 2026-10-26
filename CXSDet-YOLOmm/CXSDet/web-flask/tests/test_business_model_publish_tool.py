"""C-01: a guarded, durable wrapper around the existing model publisher."""

import sqlite3

import pytest

from services.business_tools import BusinessToolAdapter, ResourceSnapshot, ToolConflict


@pytest.fixture
def publisher(tmp_path):
    path = tmp_path / "publish.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE datasets (id TEXT PRIMARY KEY, dataset_mode TEXT, secondary_modality TEXT)"
        )
        conn.execute("INSERT INTO datasets VALUES ('d1', 'single', NULL)")
        conn.execute(
            "CREATE TABLE models (id TEXT PRIMARY KEY, status INTEGER, model_bucket TEXT, "
            "model_object_key TEXT, dataset_id TEXT, model_mode TEXT, primary_modality TEXT, "
            "secondary_modality TEXT, create_time TEXT, publish_status TEXT)"
        )
        conn.executemany(
            "INSERT INTO models VALUES (?, ?, 'models', ?, 'd1', 'single', 'rgb', NULL, ?, ?)",
            [
                ("old", 3, "old.pt", "2026-01-01", "published"),
                ("new", 1, "new.pt", "2026-01-02", None),
                ("other", 1, "other.pt", "2026-01-03", None),
            ],
        )
        conn.execute(
            "CREATE TABLE model_publish_audit (id TEXT PRIMARY KEY, model_id TEXT, "
            "operator_id INTEGER, previous_model_id TEXT, create_time TEXT)"
        )
    versions = {"new": "v1", "other": "v1", "old": "v1"}
    service = BusinessToolAdapter(
        path,
        require_access=lambda principal, action, resource: principal == 7,
        resolve_resource=lambda principal, kind, resource_id: ResourceSnapshot(
            resource_id, "project-1", 7, versions[resource_id]
        ),
        storage_exists=lambda bucket, key: bucket == "models" and key.endswith(".pt"),
    )
    return service, path, versions


def test_model_publish_tool_previews_and_reuses_existing_service(publisher):
    service, path, _ = publisher
    descriptor = next(item for item in service.list_tools(7) if item["name"] == "model.publish")
    assert descriptor["effect"] == "write"
    assert descriptor["supports_status_query"] is True
    prepared = service.prepare_tool(7, "model.publish", {"model_id": "new"})
    assert prepared["preview"]["previous_model_id"] == "old"
    result = service.execute_tool(7, prepared["operation_id"], "publish-request")
    assert result["status"] == "succeeded"
    assert result["outputs"]["publish_status"] == "published"
    assert result["business_refs"][0]["id"] == "new"
    assert service.execute_tool(7, prepared["operation_id"], "publish-request") == result
    assert service.get_operation_by_request_key(7, "publish-request") == result
    assert service.get_resource_state(7, "model", "new")["state"]["status"] == 3
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM models WHERE id='new'").fetchone()[0] == 3
        assert conn.execute("SELECT status FROM models WHERE id='old'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM model_publish_audit").fetchone()[0] == 1


def test_model_publish_tool_rechecks_version_and_rejects_key_reuse(publisher):
    service, _, versions = publisher
    first = service.prepare_tool(7, "model.publish", {"model_id": "new"})
    versions["new"] = "v2"
    with pytest.raises(ToolConflict):
        service.execute_tool(7, first["operation_id"], "publish-request")
    versions["new"] = "v1"
    service.execute_tool(7, first["operation_id"], "publish-request")
    other = service.prepare_tool(7, "model.publish", {"model_id": "other"})
    with pytest.raises(ToolConflict):
        service.execute_tool(7, other["operation_id"], "publish-request")


def test_model_publish_tool_not_exposed_without_storage_check(tmp_path):
    service = BusinessToolAdapter(
        tmp_path / "ops.sqlite3",
        require_access=lambda *_: True,
        resolve_resource=lambda *_: ResourceSnapshot("new", "project-1", 7, "v1"),
    )
    assert all(tool["name"] != "model.publish" for tool in service.list_tools(7))
    with pytest.raises(ValueError):
        service.prepare_tool(7, "model.publish", {"model_id": "new"})


def test_model_publish_requires_explicit_global_domain_permission(publisher):
    service, path, _ = publisher
    service.require_access = lambda principal, action, resource: action != "model:publish-global"
    assert all(tool["name"] != "model.publish" for tool in service.list_tools(7))
    with pytest.raises(PermissionError):
        service.prepare_tool(7, "model.publish", {"model_id": "new"})
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM models WHERE id='old'").fetchone()[0] == 3


def test_model_publish_rejects_changed_archive_set(publisher):
    service, path, _ = publisher
    prepared = service.prepare_tool(7, "model.publish", {"model_id": "new"})
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE models SET status=3 WHERE id='other'")
    with pytest.raises(ToolConflict):
        service.execute_tool(7, prepared["operation_id"], "publish-stale")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM models WHERE id='new'").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM models WHERE id='other'").fetchone()[0] == 3


def test_model_publish_preview_lists_all_models_to_archive(publisher):
    service, path, _ = publisher
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE models SET status=3 WHERE id='other'")
    prepared = service.prepare_tool(7, "model.publish", {"model_id": "new"})
    assert set(prepared["preview"]["archived_model_ids"]) == {"old", "other"}


def test_model_publish_preview_checks_dataset_compatibility(publisher):
    service, path, _ = publisher
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE datasets SET dataset_mode='dual' WHERE id='d1'")
    with pytest.raises(ValueError):
        service.prepare_tool(7, "model.publish", {"model_id": "new"})


def test_model_publish_postcommit_error_is_recorded_as_unknown(publisher, monkeypatch):
    service, path, _ = publisher
    prepared = service.prepare_tool(7, "model.publish", {"model_id": "new"})
    real_publish = service._publish_model

    def publish_then_lose_acknowledgement(*args):
        real_publish(*args)
        raise OSError("operation acknowledgement lost")

    monkeypatch.setattr(service, "_publish_model", publish_then_lose_acknowledgement)
    with pytest.raises(OSError):
        service.execute_tool(7, prepared["operation_id"], "uncertain-publish")
    operation = service.get_operation_by_request_key(7, "uncertain-publish")
    assert operation["status"] == "unknown"
    assert operation["error"]["code"] == "EXECUTION_UNCONFIRMED"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM models WHERE id='new'").fetchone()[0] == 3

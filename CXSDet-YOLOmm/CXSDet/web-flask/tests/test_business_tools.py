"""Contract tests for the server-only C-01 business tool adapter."""

import sqlite3

import pytest

from services.business_tools import BusinessToolAdapter, ResourceSnapshot, ToolConflict


@pytest.fixture
def adapter(tmp_path):
    database = tmp_path / "operations.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE datasets (id TEXT PRIMARY KEY, name TEXT, status INTEGER, "
            "dataset_mode TEXT, primary_modality TEXT, secondary_modality TEXT)"
        )
        conn.executemany(
            "INSERT INTO datasets VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("data-a", "A", 1, "single", "rgb", None),
                ("data-b", "B", 1, "single", "rgb", None),
            ],
        )

    versions = {"data-a": "v1", "data-b": "v1"}

    def resolve_resource(principal, resource_type, resource_id):
        assert resource_type == "dataset"
        return ResourceSnapshot(resource_id, "project-1", 7, versions[resource_id])

    def require_access(principal, action, resource):
        if principal != 7 or (resource and resource.owner_id != principal):
            raise PermissionError("forbidden")
        return True

    service = BusinessToolAdapter(
        database,
        require_access=require_access,
        resolve_resource=resolve_resource,
    )
    return service, versions, database


def test_preparation_and_execution_read_actual_dataset_with_guard(adapter):
    service, _, database = adapter
    prepared = service.prepare_tool(7, "dataset.get_state", {"dataset_id": "data-a"})
    assert prepared["status"] == "prepared"
    assert prepared["preview"]["resource_id"] == "data-a"
    assert prepared["preview"]["effect"] == "read"

    result = service.execute_tool(7, prepared["operation_id"], "request-1")
    assert result["status"] == "succeeded"
    assert result["outputs"]["name"] == "A"
    assert result["business_refs"] == [{"type": "dataset", "id": "data-a", "version": "v1", "project_id": "project-1"}]
    assert service.get_operation(7, result["operation_id"]) == result
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM business_tool_operations").fetchone()[0] == 2


def test_same_request_key_returns_one_result_and_rejects_different_target(adapter):
    service, _, _ = adapter
    first = service.prepare_tool(7, "dataset.get_state", {"dataset_id": "data-a"})
    second = service.prepare_tool(7, "dataset.get_state", {"dataset_id": "data-a"})
    other = service.prepare_tool(7, "dataset.get_state", {"dataset_id": "data-b"})
    result = service.execute_tool(7, first["operation_id"], "same-key")
    assert service.execute_tool(7, second["operation_id"], "same-key") == result
    with pytest.raises(ToolConflict):
        service.execute_tool(7, other["operation_id"], "same-key")


def test_request_key_replays_same_input_after_resource_version_changes(adapter):
    service, versions, _ = adapter
    first = service.prepare_tool(7, "dataset.get_state", {"dataset_id": "data-a"})
    result = service.execute_tool(7, first["operation_id"], "stable-key")
    versions["data-a"] = "v2"
    second = service.prepare_tool(7, "dataset.get_state", {"dataset_id": "data-a"})
    assert service.execute_tool(7, second["operation_id"], "stable-key") == result


def test_operation_read_rechecks_original_resource_grant(tmp_path):
    database = tmp_path / "operations.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE datasets (id TEXT PRIMARY KEY, name TEXT, status INTEGER, "
            "dataset_mode TEXT, primary_modality TEXT, secondary_modality TEXT)"
        )
        conn.execute("INSERT INTO datasets VALUES ('data-a', 'A', 1, 'single', 'rgb', NULL)")
    current_project = ["old-project"]
    revoked = set()

    def policy(_principal, _action, resource):
        return resource is None or resource.project_id not in revoked

    service = BusinessToolAdapter(
        database,
        require_access=policy,
        resolve_resource=lambda *_: ResourceSnapshot("data-a", current_project[0], 7, "v1"),
    )
    prepared = service.prepare_tool(7, "dataset.get_state", {"dataset_id": "data-a"})
    result = service.execute_tool(7, prepared["operation_id"], "project-key")
    current_project[0] = "new-project"
    revoked.add("old-project")
    with pytest.raises(PermissionError):
        service.get_operation(7, result["operation_id"])


def test_operation_status_and_key_replay_survive_resource_removal(adapter):
    service, _, _ = adapter
    prepared = service.prepare_tool(7, "dataset.get_state", {"dataset_id": "data-a"})
    result = service.execute_tool(7, prepared["operation_id"], "removed-resource-key")
    service.resolve_resource = lambda *_: (_ for _ in ()).throw(FileNotFoundError("removed"))
    assert service.get_operation(7, result["operation_id"]) == result
    assert service.get_operation_by_request_key(7, "removed-resource-key") == result
    assert service.execute_tool(7, prepared["operation_id"], "removed-resource-key") == result


def test_permission_and_snapshot_are_rechecked_before_execution(adapter):
    service, versions, _ = adapter
    with pytest.raises(PermissionError):
        service.prepare_tool(8, "dataset.get_state", {"dataset_id": "data-a"})
    prepared = service.prepare_tool(7, "dataset.get_state", {"dataset_id": "data-a"})
    with pytest.raises(PermissionError):
        service.execute_tool(8, prepared["operation_id"], "request-2")
    with pytest.raises(PermissionError):
        service.get_operation(8, prepared["operation_id"])
    versions["data-a"] = "v2"
    with pytest.raises(ToolConflict):
        service.execute_tool(7, prepared["operation_id"], "request-2")


def test_unknown_tool_and_invalid_arguments_are_rejected(adapter):
    service, _, _ = adapter
    with pytest.raises(ValueError):
        service.prepare_tool(7, "shell.run", {"command": "whoami"})
    with pytest.raises(ValueError):
        service.prepare_tool(7, "dataset.get_state", {"dataset_id": "data-a", "extra": True})
    with pytest.raises(ValueError):
        service.prepare_tool(7, "dataset.get_state", {})


def test_unfinished_execution_is_unknown_after_restart(adapter):
    service, _, database = adapter
    prepared = service.prepare_tool(7, "dataset.get_state", {"dataset_id": "data-a"})
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE business_tool_operations SET status='running' WHERE operation_id=?",
            (prepared["operation_id"],),
        )
    assert service.reconcile_unfinished() == 1
    assert service.get_operation(7, prepared["operation_id"])["status"] == "unknown"


def test_prepared_operation_cannot_execute_with_another_key(adapter):
    service, _, _ = adapter
    prepared = service.prepare_tool(7, "dataset.get_state", {"dataset_id": "data-a"})
    service.execute_tool(7, prepared["operation_id"], "first-key")
    with pytest.raises(ToolConflict):
        service.execute_tool(7, prepared["operation_id"], "second-key")


def test_cancelled_preparation_cannot_execute_after_interleaving(adapter):
    service, _, _ = adapter
    prepared = service.prepare_tool(7, "dataset.get_state", {"dataset_id": "data-a"})
    original_resolver = service.resolve_resource

    def cancel_during_execute(principal, resource_type, resource_id):
        service.resolve_resource = original_resolver
        service.cancel_operation(7, prepared["operation_id"])
        return original_resolver(principal, resource_type, resource_id)

    service.resolve_resource = cancel_during_execute
    with pytest.raises(ToolConflict):
        service.execute_tool(7, prepared["operation_id"], "cancelled-key")
    assert service.get_operation(7, prepared["operation_id"])["status"] == "cancelled"


def test_false_access_decision_denies_tool_and_resource(tmp_path):
    database = tmp_path / "denied.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE datasets (id TEXT PRIMARY KEY, name TEXT, status INTEGER, "
            "dataset_mode TEXT, primary_modality TEXT, secondary_modality TEXT)"
        )
        conn.execute("INSERT INTO datasets VALUES ('data-a', 'A', 1, 'single', 'rgb', NULL)")
    service = BusinessToolAdapter(
        database,
        require_access=lambda principal, action, resource: False,
        resolve_resource=lambda principal, resource_type, resource_id:
            ResourceSnapshot("data-a", "project-1", 7, "v1"),
    )
    with pytest.raises(PermissionError):
        service.list_tools(7)
    with pytest.raises(PermissionError):
        service.prepare_tool(7, "dataset.get_state", {"dataset_id": "data-a"})


def test_ambiguous_access_decision_denies_tool(tmp_path):
    database = tmp_path / "unknown.sqlite3"
    service = BusinessToolAdapter(
        database,
        require_access=lambda *_: None,
        resolve_resource=lambda *_: ResourceSnapshot("data-a", "project-1", 7, "v1"),
    )
    with pytest.raises(PermissionError):
        service.list_tools(7)


def test_reconciliation_does_not_turn_unknown_back_into_success(adapter):
    service, _, _ = adapter
    prepared = service.prepare_tool(7, "dataset.get_state", {"dataset_id": "data-a"})
    original_read = service._read_dataset

    def reconcile_during_read(dataset_id):
        service.reconcile_unfinished()
        return original_read(dataset_id)

    service._read_dataset = reconcile_during_read
    result = service.execute_tool(7, prepared["operation_id"], "interrupted-key")
    assert result["status"] == "unknown"

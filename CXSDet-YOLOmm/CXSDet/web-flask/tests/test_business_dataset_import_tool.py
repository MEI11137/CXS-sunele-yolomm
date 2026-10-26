"""C-03: dataset.import through the guarded business tool adapter."""

import hashlib
import sqlite3
import zipfile

import pytest
import yaml

from clients.file_client import FileClient
from services.business_tools import BusinessToolAdapter, ResourceSnapshot, ToolConflict
from services.dataset_import_service import DatasetImportError
from services.file_access import FileAccess, LocalFileAssetGateway


def _write_archive(path, *, broken=False):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "data.yaml",
            yaml.safe_dump({
                "path": ".", "train": "./images/train", "val": "./images/val",
                "names": ["crack", "hotspot"],
            }),
        )
        archive.writestr("images/train/a.jpg", b"rgb")
        archive.writestr("labels/train/a.txt", "0 0.5 0.5 0.2 0.2")
        archive.writestr("images/val/b.jpg", b"rgb")
        if not broken:
            archive.writestr("labels/val/b.txt", "1 0.5 0.5 0.2 0.2")
    return path


class _Fixture:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.db_path = tmp_path / "app.sqlite3"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE datasets (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
                    zip_bucket TEXT NOT NULL, zip_object_key TEXT NOT NULL,
                    structure_info TEXT, upload_time TEXT NOT NULL, file_size INTEGER,
                    status INTEGER DEFAULT 0, dataset_mode TEXT NOT NULL DEFAULT 'single',
                    primary_modality TEXT NOT NULL DEFAULT 'rgb',
                    secondary_modality TEXT, runtime_mapping TEXT
                )
                """
            )
        self.store = tmp_path / "store"
        self.store.mkdir()
        self.storage = FileClient(base_path=self.store)
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.policy = lambda user_id, project_id, action: project_id == "project-1"
        self.access = FileAccess(self.db_path, project_access=self.policy)
        self.gateway = LocalFileAssetGateway(
            self.db_path, self.storage, project_access=self.policy, work_dir=self.work,
        )
        self.projects = {}

    def upload(self, *, broken=False, project_id="project-1", suffix=".zip"):
        local = _write_archive(self.root / f"archive-{suffix.lstrip('.')}-{broken}.zip", broken=broken)
        stored = self.storage.upload("project_uploads", str(local))
        digest = hashlib.sha256(local.read_bytes()).hexdigest()
        self.projects["project-1"] = "v1"
        return self.access.register_upload(
            7, stored["bucket"], stored["objectKey"], "dataset_archive", "application/zip",
            local.stat().st_size, project_id, original_name="dataset.zip",
            sha256=digest, status="uploaded",
        )

    def adapter(self, *, require_access=None):
        def resolve(principal, kind, resource_id):
            assert kind in {"project", "dataset", "model"}
            return ResourceSnapshot(resource_id, "project-1", 7, "v1")

        policy = require_access or (
            lambda principal, action, resource:
            principal == 7 and (resource is None or resource.project_id == "project-1")
        )
        return BusinessToolAdapter(
            self.db_path,
            require_access=policy,
            resolve_resource=resolve,
            file_assets=self.gateway,
            storage_client=self.storage,
        )


def test_preview_lists_classes_counts_and_proposed_registration(tmp_path):
    fixture = _Fixture(tmp_path)
    asset_id = fixture.upload()
    service = fixture.adapter()

    prepared = service.prepare_tool(7, "dataset.import", {
        "asset_id": asset_id, "name": "sea-v1", "dataset_mode": "single",
    })
    preview = prepared["preview"]
    inspection = preview["inspection"]

    assert inspection["class_names"] == ["crack", "hotspot"]
    assert inspection["split_counts"] == {"train": 1, "val": 1, "test": 0}
    assert inspection["ready_to_register"] is True
    assert preview["blocked"] is False
    assert preview["proposed_registration"]["name"] == "sea-v1"
    assert preview["proposed_registration"]["source_file_id"] == asset_id
    assert preview["project_id"] == "project-1"


def test_execute_registers_dataset_and_binds_the_upload(tmp_path):
    fixture = _Fixture(tmp_path)
    asset_id = fixture.upload()
    service = fixture.adapter()
    prepared = service.prepare_tool(7, "dataset.import", {
        "asset_id": asset_id, "name": "sea-v1", "dataset_mode": "single",
    })

    result = service.execute_tool(7, prepared["operation_id"], "import-key")

    assert result["status"] == "succeeded"
    assert result["outputs"]["total_samples"] == 2
    assert result["outputs"]["reused"] is False
    assert result["business_refs"][0]["type"] == "dataset"
    assert result["business_refs"][0]["source_file_id"] == asset_id
    dataset_id = result["outputs"]["dataset_id"]
    with sqlite3.connect(fixture.db_path) as conn:
        assert conn.execute(
            "SELECT status FROM datasets WHERE id=?", (dataset_id,)
        ).fetchone()[0] == 1
        binding = conn.execute(
            "SELECT status, business_type, business_id FROM file_assets WHERE id=?",
            (asset_id,),
        ).fetchone()
    assert binding == ("registered", "dataset", dataset_id)
    assert service.get_resource_state(7, "dataset", dataset_id)["state"]["name"] == "sea-v1"


def test_same_request_key_never_registers_twice(tmp_path):
    fixture = _Fixture(tmp_path)
    asset_id = fixture.upload()
    service = fixture.adapter()
    prepared = service.prepare_tool(7, "dataset.import", {
        "asset_id": asset_id, "name": "sea-v1", "dataset_mode": "single",
    })

    first = service.execute_tool(7, prepared["operation_id"], "import-key")
    again = service.execute_tool(7, prepared["operation_id"], "import-key")

    assert again == first
    with sqlite3.connect(fixture.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 1


def test_retry_with_a_new_key_reuses_the_existing_dataset(tmp_path):
    fixture = _Fixture(tmp_path)
    asset_id = fixture.upload()
    service = fixture.adapter()
    first = service.execute_tool(
        7,
        service.prepare_tool(7, "dataset.import", {
            "asset_id": asset_id, "name": "sea-v1", "dataset_mode": "single",
        })["operation_id"],
        "first-key",
    )
    second = service.execute_tool(
        7,
        service.prepare_tool(7, "dataset.import", {
            "asset_id": asset_id, "name": "sea-v1", "dataset_mode": "single",
        })["operation_id"],
        "second-key",
    )

    assert second["status"] == "succeeded"
    assert second["outputs"]["reused"] is True
    assert second["outputs"]["dataset_id"] == first["outputs"]["dataset_id"]
    with sqlite3.connect(fixture.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 1


def test_invalid_archive_prepares_with_a_fix_list_and_fails_cleanly(tmp_path):
    fixture = _Fixture(tmp_path)
    asset_id = fixture.upload(broken=True)
    service = fixture.adapter()

    prepared = service.prepare_tool(7, "dataset.import", {
        "asset_id": asset_id, "name": "broken-v1", "dataset_mode": "single",
    })
    preview = prepared["preview"]
    assert preview["blocked"] is True
    assert [item["code"] for item in preview["blocked_reasons"]] == ["MISSING_LABEL"]

    with pytest.raises(DatasetImportError):
        service.execute_tool(7, prepared["operation_id"], "broken-key")

    operation = service.get_operation_by_request_key(7, "broken-key")
    assert operation["status"] == "failed"
    assert operation["error"]["code"] == "DATASET_INVALID"
    assert operation["item_results"][0]["code"] == "MISSING_LABEL"
    with sqlite3.connect(fixture.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 0


def test_duplicate_name_is_reported_before_registration(tmp_path):
    fixture = _Fixture(tmp_path)
    first_asset = fixture.upload()
    service = fixture.adapter()
    service.execute_tool(
        7,
        service.prepare_tool(7, "dataset.import", {
            "asset_id": first_asset, "name": "taken", "dataset_mode": "single",
        })["operation_id"],
        "first-key",
    )

    second_asset = fixture.upload()
    prepared = service.prepare_tool(7, "dataset.import", {
        "asset_id": second_asset, "name": "taken", "dataset_mode": "single",
    })
    assert prepared["preview"]["blocked"] is True
    assert prepared["preview"]["blocked_reasons"][0]["code"] == "NAME_CONFLICT"

    with pytest.raises(DatasetImportError):
        service.execute_tool(7, prepared["operation_id"], "second-key")


def test_replaced_upload_cannot_be_imported_under_a_stale_preview(tmp_path):
    fixture = _Fixture(tmp_path)
    asset_id = fixture.upload()
    service = fixture.adapter()
    prepared = service.prepare_tool(7, "dataset.import", {
        "asset_id": asset_id, "name": "sea-v1", "dataset_mode": "single",
    })

    with sqlite3.connect(fixture.db_path) as conn:
        conn.execute(
            "UPDATE file_assets SET sha256='tampered' WHERE id=?", (asset_id,)
        )

    with pytest.raises(ToolConflict):
        service.execute_tool(7, prepared["operation_id"], "stale-key")
    with sqlite3.connect(fixture.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 0


def test_dataset_import_requires_project_create_permission(tmp_path):
    fixture = _Fixture(tmp_path)
    asset_id = fixture.upload()
    service = fixture.adapter(
        require_access=lambda principal, action, resource:
        principal == 7 and action != "dataset:create"
    )

    with pytest.raises(PermissionError):
        service.prepare_tool(7, "dataset.import", {
            "asset_id": asset_id, "name": "sea-v1", "dataset_mode": "single",
        })
    with sqlite3.connect(fixture.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 0


def test_dataset_import_is_hidden_without_a_file_gateway(tmp_path):
    fixture = _Fixture(tmp_path)
    service = BusinessToolAdapter(
        fixture.db_path,
        require_access=lambda *_: True,
        resolve_resource=lambda principal, kind, resource_id:
            ResourceSnapshot(resource_id, "project-1", 7, "v1"),
    )

    assert all(tool["name"] != "dataset.import" for tool in service.list_tools(7))
    with pytest.raises(ValueError):
        service.prepare_tool(7, "dataset.import", {
            "asset_id": "asset", "name": "x", "dataset_mode": "single",
        })


def test_arguments_outside_the_schema_are_rejected(tmp_path):
    fixture = _Fixture(tmp_path)
    asset_id = fixture.upload()
    service = fixture.adapter()

    with pytest.raises(ValueError):
        service.prepare_tool(7, "dataset.import", {
            "asset_id": asset_id, "name": "x", "dataset_mode": "single",
            "owner_id": 1,
        })
    with pytest.raises(ValueError):
        service.prepare_tool(7, "dataset.import", {
            "asset_id": asset_id, "name": "x", "dataset_mode": "dual",
        })
    with pytest.raises(ValueError):
        service.prepare_tool(7, "dataset.import", {
            "asset_id": asset_id, "dataset_mode": "single",
        })


def test_class_names_that_the_archive_already_defines_are_marked_ignored(tmp_path):
    fixture = _Fixture(tmp_path)
    asset_id = fixture.upload()
    service = fixture.adapter()

    prepared = service.prepare_tool(7, "dataset.import", {
        "asset_id": asset_id, "name": "sea-v1", "dataset_mode": "single",
        "class_names": ["other"],
    })

    assert prepared["preview"]["ignored_arguments"] == ["class_names"]
    assert prepared["preview"]["inspection"]["class_names"] == ["crack", "hotspot"]

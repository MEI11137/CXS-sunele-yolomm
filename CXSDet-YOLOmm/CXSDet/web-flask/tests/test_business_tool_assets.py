"""C-07/C-08/C-09/C-10: authorized directories, candidate datasets, documents, MCP."""

import json
import os
import sqlite3
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from clients.file_client import FileClient
from services.authorized_directories import DirectoryStore
from services.business_tools import ENTRY_HUMAN_PAGE, BusinessToolAdapter, ResourceSnapshot
from services.dataset_transform_service import get_candidate
from services.file_access import FileAccess, LocalFileAssetGateway
from services.mcp_registry import McpStore
from services.tool_common import ToolRefused
from utils.db import Database


class Fixture:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.db_path = tmp_path / "app.sqlite3"
        database = Database()
        database.db_path = str(self.db_path)
        database.initialize_db()
        self.store_dir = tmp_path / "store"
        self.store_dir.mkdir()
        self.storage = FileClient(base_path=self.store_dir)
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.denied = set()
        self.policy = lambda user_id, project_id, action: (
            user_id == 7 and project_id == "project-1"
        )
        self.access = FileAccess(self.db_path, project_access=self.policy)
        self.gateway = LocalFileAssetGateway(
            self.db_path, self.storage, project_access=self.policy, work_dir=self.work,
        )
        self.directories = DirectoryStore(self.db_path)
        self.transport = None
        self.mcp = McpStore(
            self.db_path,
            transport_factory=lambda config: self.transport,
            secret_store=None,
        )

    def require_access(self, principal, action, resource):
        return principal == 7 and action not in self.denied

    def resolve_resource(self, principal, kind, resource_id):
        if kind == "workspace":
            return ResourceSnapshot(str(principal), "project-1", 7, "v1")
        return ResourceSnapshot(resource_id, "project-1", 7, "v1")

    def adapter(self, **overrides) -> BusinessToolAdapter:
        options = dict(
            require_access=self.require_access,
            resolve_resource=self.resolve_resource,
            storage_exists=lambda bucket, key: True,
            file_assets=self.gateway,
            storage_client=self.storage,
            work_dir=str(self.work),
            candidate_root=str(self.root / "candidates"),
            directory_store=self.directories,
            mcp_store=self.mcp,
            notify=lambda principal, event: None,
        )
        options.update(overrides)
        return BusinessToolAdapter(self.db_path, **options)

    def sql(self, statement: str, params=()):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(statement, params).fetchall()

    def put(self, bucket: str, name: str, data: bytes) -> dict:
        local = self.root / f"put-{uuid.uuid4().hex[:8]}-{name}"
        local.write_bytes(data)
        return self.storage.upload(bucket, str(local))

    def upload(self, local: Path, kind: str, mime: str = "application/octet-stream") -> str:
        stored = self.storage.upload("project_uploads", str(local))
        return self.access.register_upload(
            7, stored["bucket"], stored["objectKey"], kind, mime,
            local.stat().st_size, "project-1", original_name=local.name, status="uploaded",
        )

    def dataset_with_archive(self, *, name="sea-v1", classes=("crack", "hotspot"),
                             duplicate_across_splits=False) -> str:
        archive = self.root / f"{name}.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("data.yaml", yaml.safe_dump({
                "path": ".", "train": "./images/train", "val": "./images/val",
                "names": list(classes),
            }))
            for key, label in {"train/a": "0 0.5 0.5 0.2 0.2",
                               "val/b": "1 0.5 0.5 0.2 0.2"}.items():
                payload = b"identical-bytes" if duplicate_across_splits else key.encode() * 3
                bundle.writestr(f"images/{key}.jpg", payload)
                bundle.writestr(f"labels/{key}.txt", label)
        stored = self.storage.upload("datasets", str(archive))
        dataset_id = str(uuid.uuid4())
        self.sql(
            "INSERT INTO datasets (id, name, description, zip_bucket, zip_object_key, "
            "structure_info, upload_time, file_size, status, dataset_mode, "
            "primary_modality, secondary_modality, runtime_mapping) "
            "VALUES (?, ?, '', ?, ?, ?, '2026-01-01', ?, 1, 'single', 'rgb', NULL, '{}')",
            (dataset_id, name, stored["bucket"], stored["objectKey"],
             json.dumps({"names": list(classes), "class_count": len(classes)}),
             archive.stat().st_size),
        )
        return dataset_id


@pytest.fixture
def fixture(tmp_path):
    return Fixture(tmp_path)


def _tree(root: Path) -> Path:
    (root / "images" / "train").mkdir(parents=True)
    (root / "images" / "val").mkdir(parents=True)
    (root / "images_edge" / "train").mkdir(parents=True)
    (root / "labels" / "train").mkdir(parents=True)
    (root / "labels" / "val").mkdir(parents=True)
    (root / "data.yaml").write_text(
        yaml.safe_dump({"names": ["crack", "hotspot"]}), encoding="utf-8",
    )
    (root / "images" / "train" / "a.jpg").write_bytes(b"same-bytes")
    (root / "images" / "val" / "a.jpg").write_bytes(b"same-bytes")
    (root / "images_edge" / "train" / "a.png").write_bytes(b"edge-bytes")
    (root / "labels" / "train" / "a.txt").write_text("0 0.5 0.5 0.2 0.2\n",
                                                    encoding="utf-8")
    (root / "labels" / "val" / "a.txt").write_text("1 0.5 0.5 0.2 0.2\n",
                                                  encoding="utf-8")
    return root


# --------------------------------------------------------------------- C-07


def test_directory_inspection_reports_real_duplicates_and_symlinks(fixture, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.jpg").write_bytes(b"secret")
    root = _tree(tmp_path / "authorized")
    try:
        os.symlink(outside / "secret.jpg", root / "images" / "train" / "leak.jpg")
    except (OSError, NotImplementedError):
        pytest.skip("symlink not permitted")

    directory = fixture.directories.register("project-1", str(root), "验收目录", 7)
    adapter = fixture.adapter()

    prepared = adapter.prepare_tool(7, "directory.inspect", {"directory_id": directory["id"]})
    assert prepared["preview"]["sample_count"] == 2
    result = adapter.execute_tool(7, prepared["operation_id"], "inspect-1")
    outputs = result["outputs"]
    assert outputs["splits"] == {"train": 1, "val": 1}
    assert outputs["duplicates"] and outputs["duplicates"][0]["count"] == 2
    assert outputs["leakage"]["count"] == 1
    codes = {issue["code"] for issue in outputs["issues"]}
    assert "SYMLINK_SKIPPED" in codes
    assert all("secret" not in json.dumps(issue) for issue in outputs["issues"])


def test_directory_search_cannot_escape_the_registered_root(fixture, tmp_path):
    root = _tree(tmp_path / "authorized")
    (tmp_path / "authorized-neighbour.txt").write_text("x", encoding="utf-8")
    directory = fixture.directories.register("project-1", str(root), None, 7)
    adapter = fixture.adapter()

    prepared = adapter.prepare_tool(7, "directory.search",
                                   {"directory_id": directory["id"], "query": "a."})
    result = adapter.execute_tool(7, prepared["operation_id"], "search-1")
    assert result["outputs"]["matches"]
    assert all(not match["path"].startswith("..") for match in result["outputs"]["matches"])

    outside = adapter.prepare_tool(7, "directory.search",
                                   {"directory_id": directory["id"], "query": "../"})
    escaped = adapter.execute_tool(7, outside["operation_id"], "search-2")
    assert escaped["outputs"]["matches"] == []


# --------------------------------------------------------------------- C-08


def test_transform_rewrites_classes_and_registers_a_new_dataset(fixture):
    source_id = fixture.dataset_with_archive()
    adapter = fixture.adapter()

    prepared = adapter.prepare_tool(7, "dataset.transform", {
        "dataset_id": source_id,
        "name": "sea-v2",
        "class_map": {"crack": "crack_v2"},
        "split_policy": {"train": 0.5, "val": 0.5},
    })
    preview = prepared["preview"]
    assert preview["blocked"] is False
    assert preview["class_names"] == ["crack_v2", "hotspot"]
    assert preview["counts"] == {"train": 1, "val": 1}

    built = adapter.execute_tool(7, prepared["operation_id"], "transform-1")
    assert built["status"] == "succeeded"
    candidate_id = built["outputs"]["candidate_id"]
    output = Path(built["outputs"]["output_dir"])
    data = yaml.safe_load((output / "data.yaml").read_text(encoding="utf-8"))
    assert data["names"] == ["crack_v2", "hotspot"]
    assert (output / "labels" / "train" / "a.txt").read_text(encoding="utf-8").startswith("0 ")
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"]["dataset_id"] == source_id
    assert manifest["class_map"] == {"crack": "crack_v2", "hotspot": "hotspot"}
    assert get_candidate(adapter, candidate_id)["status"] == "built"

    registration = adapter.prepare_tool(7, "dataset.register_candidate",
                                        {"candidate_id": candidate_id})
    assert registration["preview"]["blocked"] is False
    registered = adapter.execute_tool(7, registration["operation_id"], "register-1")
    assert registered["outputs"]["dataset_id"]
    rows = fixture.sql("SELECT structure_info FROM datasets WHERE id=?",
                       (registered["outputs"]["dataset_id"],))
    assert json.loads(rows[0][0])["names"] == ["crack_v2", "hotspot"]
    # The source dataset is untouched.
    assert fixture.sql("SELECT name FROM datasets WHERE id=?", (source_id,))[0][0] == "sea-v1"


def test_transform_refuses_to_split_leaking_duplicates(fixture):
    source_id = fixture.dataset_with_archive(duplicate_across_splits=True)
    adapter = fixture.adapter()
    prepared = adapter.prepare_tool(7, "dataset.transform", {
        "dataset_id": source_id, "name": "leaky", "class_names": ["crack", "hotspot"],
    })
    codes = {item["code"] for item in prepared["preview"]["conflicts"]}
    assert prepared["preview"]["blocked"] is True
    assert codes & {"SPLIT_LEAKAGE", "SPLIT_UNKNOWN"}


# --------------------------------------------------------------------- C-09


_PDF = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >> endobj
3 0 obj << /Type /Page /Contents 4 0 R >> endobj
4 0 obj << /Length 60 >>
stream
BT /F1 12 Tf 72 720 Td (first page inspection rule) Tj ET
endstream
endobj
5 0 obj << /Type /Page /Contents 6 0 R >> endobj
6 0 obj << /Length 60 >>
stream
BT /F1 12 Tf 72 720 Td (second page inspection rule) Tj ET
endstream
endobj
trailer << /Root 1 0 R >>
%%EOF
"""


def _docx(path: Path) -> Path:
    document = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>巡检手册</w:t></w:r></w:p>
    <w:p><w:r><w:t>巡检要求：每日一次。</w:t></w:r></w:p>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>项目</w:t></w:r></w:p></w:tc>
            <w:tc><w:p><w:r><w:t>频次</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
  </w:body>
</w:document>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("[Content_Types].xml", "<Types/>")
    return path


def test_pdf_pages_keep_the_real_page_number(fixture):
    pdf = fixture.root / "manual.pdf"
    pdf.write_bytes(_PDF)
    asset = fixture.upload(pdf, "document", "application/pdf")
    adapter = fixture.adapter()

    prepared = adapter.prepare_tool(7, "document.parse", {"asset_id": asset})
    assert prepared["preview"]["format"] == "pdf"
    result = adapter.execute_tool(7, prepared["operation_id"], "parse-pdf")
    assert result["outputs"]["status"] == "ready"
    assert result["outputs"]["page_count"] == 2
    document_id = result["outputs"]["document_id"]

    search = adapter.prepare_tool(7, "document.search",
                                  {"document_id": document_id, "query": "second page"})
    hits = adapter.execute_tool(7, search["operation_id"], "search-pdf")["outputs"]["hits"]
    assert hits and hits[0]["page_no"] == 2
    assert "第 2 页" in hits[0]["citation"]


def test_docx_uses_paragraph_and_table_anchors_not_page_numbers(fixture):
    docx = _docx(fixture.root / "manual.docx")
    asset = fixture.upload(docx, "document", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    adapter = fixture.adapter()

    prepared = adapter.prepare_tool(7, "document.parse", {"asset_id": asset})
    result = adapter.execute_tool(7, prepared["operation_id"], "parse-docx")
    assert result["outputs"]["status"] == "ready"
    assert result["outputs"]["page_count"] == 0
    document_id = result["outputs"]["document_id"]

    search = adapter.prepare_tool(7, "document.search",
                                  {"document_id": document_id, "query": "巡检要求"})
    hits = adapter.execute_tool(7, search["operation_id"], "search-docx")["outputs"]["hits"]
    assert hits[0]["page_no"] is None
    assert hits[0]["paragraph_no"] == 2
    assert "第 2 段" in hits[0]["citation"]

    table = adapter.prepare_tool(7, "document.search",
                                 {"document_id": document_id, "query": "频次"})
    rows = adapter.execute_tool(7, table["operation_id"], "search-table")["outputs"]["hits"]
    assert rows[0]["table_no"] == 1


def test_legacy_doc_is_refused_with_a_conversion_hint(fixture):
    doc = fixture.root / "old.doc"
    doc.write_bytes(b"\xd0\xcf\x11\xe0legacy")
    asset = fixture.upload(doc, "document", "application/msword")
    adapter = fixture.adapter()

    prepared = adapter.prepare_tool(7, "document.parse", {"asset_id": asset})
    assert prepared["preview"]["blocked"] is True
    assert "另存为" in prepared["preview"]["blocked_reasons"][0]["message"]


# --------------------------------------------------------------------- C-10


class FakeTransport:
    def __init__(self, fail_write=False):
        self.fail_write = fail_write
        self.calls = []

    def describe(self):
        return {"name": "fake-server", "version": "1.0"}

    def list_tools(self):
        return [
            {"name": "search.web", "description": "只读检索",
             "inputSchema": {"type": "object"},
             "annotations": {"readOnlyHint": True, "idempotentHint": True}},
            {"name": "ticket.create", "description": "创建工单",
             "inputSchema": {"type": "object"}, "annotations": {}},
        ]

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "ticket.create" and self.fail_write:
            raise TimeoutError("gateway timeout")
        return {"ok": True, "echo": arguments}


def test_mcp_connection_is_configured_then_discovered_then_executed(fixture):
    adapter = fixture.adapter()
    # 连接配置属于扩权设置，只能从人工设置页面入口提交（INT-05）
    saved = adapter.prepare_tool(7, "mcp.save_connection", {
        "name": "本机检索服务", "transport": "http", "endpoint": "http://127.0.0.1:9000",
        "credential_ref": "secret/mcp/local",
    }, entry=ENTRY_HUMAN_PAGE)
    result = adapter.execute_tool(7, saved["operation_id"], "mcp-save",
                                 entry=ENTRY_HUMAN_PAGE)
    connection_id = result["outputs"]["connection_id"]
    assert result["outputs"]["status"] == "configured"

    # No transport is configured yet: the site says so instead of pretending.
    untested = adapter.prepare_tool(7, "mcp.test_connection", {"connection_id": connection_id})
    offline = adapter.execute_tool(7, untested["operation_id"], "mcp-test-offline")
    assert offline["outputs"]["connected"] is False
    assert "尚未配置" in offline["outputs"]["error"]

    fixture.transport = FakeTransport()
    tested = adapter.prepare_tool(7, "mcp.test_connection", {"connection_id": connection_id})
    online = adapter.execute_tool(7, tested["operation_id"], "mcp-test-online")
    assert online["outputs"]["connected"] is True

    # Executing before discovery is refused: connecting is not authorising.
    with pytest.raises(ToolRefused):
        adapter.prepare_tool(7, "mcp.execute", {
            "connection_id": connection_id, "tool_name": "search.web", "arguments": {},
        })

    discovered = adapter.prepare_tool(7, "mcp.discover_tools",
                                      {"connection_id": connection_id})
    listing = adapter.execute_tool(7, discovered["operation_id"], "mcp-discover")
    assert {tool["name"] for tool in listing["outputs"]["tools"]} == {
        "search.web", "ticket.create",
    }
    assert listing["outputs"]["tools_version"]
    assert listing["outputs"]["changed"] is False

    call = adapter.prepare_tool(7, "mcp.execute", {
        "connection_id": connection_id, "tool_name": "search.web",
        "arguments": {"q": "巡检"},
    })
    assert call["preview"]["tool"]["writes"] is False
    executed = adapter.execute_tool(7, call["operation_id"], "mcp-call")
    assert executed["outputs"]["effect"] == "read"
    assert executed["outputs"]["result"]["echo"] == {"q": "巡检"}


def test_mcp_write_tool_failure_is_reported_as_unknown(fixture):
    adapter = fixture.adapter()
    saved = adapter.prepare_tool(7, "mcp.save_connection", {
        "name": "工单服务", "transport": "http", "endpoint": "http://127.0.0.1:9000",
    }, entry=ENTRY_HUMAN_PAGE)
    connection_id = adapter.execute_tool(
        7, saved["operation_id"], "mcp-save-2", entry=ENTRY_HUMAN_PAGE,
    )["outputs"]["connection_id"]
    fixture.transport = FakeTransport(fail_write=True)
    adapter.execute_tool(
        7, adapter.prepare_tool(7, "mcp.discover_tools",
                                {"connection_id": connection_id})["operation_id"],
        "mcp-discover-2",
    )

    prepared = adapter.prepare_tool(7, "mcp.execute", {
        "connection_id": connection_id, "tool_name": "ticket.create",
        "arguments": {"subject": "裂纹"},
    })
    with pytest.raises(RuntimeError):
        adapter.execute_tool(7, prepared["operation_id"], "mcp-write")
    state = adapter.get_operation(7, prepared["operation_id"])
    assert state["status"] == "unknown"
    assert fixture.transport.calls.count(("ticket.create", {"subject": "裂纹"})) == 1


def test_mcp_execute_requires_the_policy_to_allow_it(fixture):
    adapter = fixture.adapter()
    fixture.denied.add("mcp:execute")
    with pytest.raises(PermissionError):
        adapter.prepare_tool(7, "mcp.execute", {
            "connection_id": "any", "tool_name": "search.web",
        })

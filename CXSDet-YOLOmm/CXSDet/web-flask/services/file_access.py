"""Resolve file ownership from registered uploads and existing business rows."""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


_BUSINESS_REFS = {
    "detection_records": (
        ("original_image_bucket", "original_image_object_key"),
        ("result_image_bucket", "result_image_object_key"),
        ("secondary_image_bucket", "secondary_image_object_key"),
        ("enhanced_image_bucket", "enhanced_image_object_key"),
    ),
    "video_detection_records": (
        ("original_video_bucket", "original_video_object_key"),
        ("result_video_bucket", "result_video_object_key"),
        ("secondary_video_bucket", "secondary_video_object_key"),
    ),
    "realtime_detection_records": (("result_image_bucket", "result_image_object_key"),),
}

# ``uploaded`` means stored only; ``registered`` means a dataset/model row was
# created from it. Both stay readable for their owner and project members.
_READABLE_STATUSES = {"ready", "uploaded", "partial", "registered"}


class FileAccess:
    def __init__(self, database_path: str | Path, project_access=None) -> None:
        self.database_path = str(database_path)
        self.project_access = project_access
        with sqlite3.connect(self.database_path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS file_assets (
                    id TEXT PRIMARY KEY,
                    bucket TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    owner_id INTEGER NOT NULL,
                    project_id TEXT,
                    kind TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    original_name TEXT,
                    sha256 TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE (bucket, object_key)
                )"""
            )
            existing = self._columns(conn, "file_assets")
            for name, ddl in (
                ("original_name", "TEXT"),
                ("sha256", "TEXT"),
                ("version", "INTEGER NOT NULL DEFAULT 1"),
                # C-03: an upload is not an imported business resource until a
                # dataset/model row is registered against it.
                ("business_type", "TEXT"),
                ("business_id", "TEXT"),
                ("updated_at", "TEXT"),
            ):
                if name not in existing:
                    conn.execute(f'ALTER TABLE file_assets ADD COLUMN "{name}" {ddl}')

    def register_upload(
        self, owner_id: int, bucket: str, object_key: str,
        kind: str, mime_type: str, size: int, project_id: str | None = None,
        *, original_name: str | None = None, sha256: str | None = None,
        status: str = "ready",
    ) -> str:
        if project_id is not None:
            if self.project_access is None or self.project_access(owner_id, project_id, "file:create") is not True:
                raise PermissionError("Project file access unavailable")
        asset_id = str(uuid.uuid4())
        with sqlite3.connect(self.database_path) as conn:
            conn.execute(
                "INSERT INTO file_assets "
                "(id, bucket, object_key, owner_id, project_id, kind, mime_type, "
                "size, status, original_name, sha256, version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (asset_id, bucket, object_key, owner_id, project_id, kind,
                 mime_type, size, status, original_name, sha256, 1,
                 datetime.now(timezone.utc).isoformat()),
            )
        return asset_id

    def require_account(self, user_id: int) -> None:
        with sqlite3.connect(self.database_path) as conn:
            columns = self._columns(conn, "user")
            if "id" not in columns:
                raise PermissionError("Account is not active")
            condition = "id=? AND status=1" if "status" in columns else "id=?"
            row = conn.execute(
                f"SELECT 1 FROM user WHERE {condition}", (user_id,)
            ).fetchone()
        if row is None:
            raise PermissionError("Account is not active")

    def get_asset(self, user_id: int, asset_id: str) -> dict:
        with sqlite3.connect(self.database_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM file_assets WHERE id=?", (asset_id,)).fetchone()
        if row is None:
            raise PermissionError("File is not accessible")
        if row["project_id"] is None:
            if row["owner_id"] != user_id:
                raise PermissionError("File is not accessible")
        else:
            if self.project_access is None or self.project_access(
                user_id, row["project_id"], "file:read"
            ) is not True:
                raise PermissionError("Project file access unavailable")
        return dict(row)

    def require_avatar_assignment(self, user_id: int, bucket: str, object_key: str) -> None:
        with sqlite3.connect(self.database_path) as conn:
            row = conn.execute(
                "SELECT owner_id, project_id, kind, status FROM file_assets "
                "WHERE bucket=? AND object_key=?", (bucket, object_key),
            ).fetchone()
        if (row is None or row[0] != user_id or row[1] is not None or
                row[2] != "avatar" or row[3] != "ready" or bucket != "avatars"):
            raise PermissionError("头像文件未登记或不属于该用户")

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}

    def require_read(self, user_id: int, bucket: str, object_key: str) -> None:
        with sqlite3.connect(self.database_path) as conn:
            conn.row_factory = sqlite3.Row
            asset = conn.execute(
                "SELECT owner_id, project_id, status FROM file_assets WHERE bucket=? AND object_key=?",
                (bucket, object_key),
            ).fetchone()
            if asset is not None:
                if asset["status"] not in _READABLE_STATUSES:
                    raise PermissionError("File is not accessible")
                project_id = asset["project_id"]
                if project_id is None:
                    if asset["owner_id"] != user_id:
                        raise PermissionError("File is not accessible")
                else:
                    if self.project_access is None or self.project_access(user_id, project_id, "file:read") is not True:
                        raise PermissionError("Project file access unavailable")
                return

            # Profile fields are editable and cannot establish ownership. A
            # legacy key shared across users is ambiguous, so deny it.
            for table, references in _BUSINESS_REFS.items():
                columns = self._columns(conn, table)
                if "user_id" not in columns:
                    continue
                for bucket_col, key_col in references:
                    if {bucket_col, key_col} <= columns:
                        foreign = conn.execute(
                            f'SELECT 1 FROM "{table}" WHERE user_id<>? '
                            f'AND "{bucket_col}"=? AND "{key_col}"=? LIMIT 1',
                            (user_id, bucket, object_key),
                        ).fetchone()
                        if foreign:
                            raise PermissionError("File ownership is ambiguous")
            for table, references in _BUSINESS_REFS.items():
                columns = self._columns(conn, table)
                if "user_id" not in columns:
                    continue
                for bucket_col, key_col in references:
                    if {bucket_col, key_col} <= columns:
                        linked = conn.execute(
                            f'SELECT 1 FROM "{table}" WHERE user_id=? '
                            f'AND "{bucket_col}"=? AND "{key_col}"=? LIMIT 1',
                            (user_id, bucket, object_key),
                        ).fetchone()
                        if linked:
                            return
        raise PermissionError("File is not accessible")

    def require_delete(self, user_id: int, bucket: str, object_key: str) -> None:
        with sqlite3.connect(self.database_path) as conn:
            asset = conn.execute(
                "SELECT owner_id, project_id, status FROM file_assets WHERE bucket=? AND object_key=?",
                (bucket, object_key),
            ).fetchone()
        if asset is None or asset[0] != user_id or asset[1] is not None or asset[2] not in {"ready", "uploaded", "partial"}:
            raise PermissionError("File deletion is not available")

    def mark_deleted(self, bucket: str, object_key: str) -> None:
        with sqlite3.connect(self.database_path) as conn:
            conn.execute(
                "UPDATE file_assets SET status='deleted' WHERE bucket=? AND object_key=?",
                (bucket, object_key),
            )

    def get_binding(self, asset_id: str) -> dict | None:
        with sqlite3.connect(self.database_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT business_type, business_id FROM file_assets WHERE id=?",
                (asset_id,),
            ).fetchone()
        if row is None:
            return None
        return {"business_type": row["business_type"], "business_id": row["business_id"]}

    def link_business(self, asset_id: str, business_type: str, business_id: str) -> None:
        """Record that an upload became a business resource.

        An upload may only ever back one resource: re-registering the same
        pair is a no-op, a different pair is rejected so a retry cannot create
        a second dataset or model from one archive.
        """
        if not business_type or not business_id:
            raise ValueError("Business binding is required")
        with sqlite3.connect(self.database_path) as conn:
            self.link_business_with(conn, asset_id, business_type, business_id)

    def link_business_with(self, conn: sqlite3.Connection, asset_id: str,
                           business_type: str, business_id: str) -> None:
        """Same binding inside a caller transaction."""
        if not business_type or not business_id:
            raise ValueError("Business binding is required")
        row = conn.execute(
            "SELECT business_type, business_id FROM file_assets WHERE id=?",
            (asset_id,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError("Uploaded file is not registered")
        if row[1]:
            if (row[0], row[1]) == (business_type, business_id):
                return
            raise ValueError("File is already bound to another business resource")
        cursor = conn.execute(
            "UPDATE file_assets SET status='registered', business_type=?, "
            "business_id=?, updated_at=? WHERE id=? AND business_id IS NULL",
            (business_type, business_id, datetime.now(timezone.utc).isoformat(), asset_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("File binding changed concurrently")

    def require_import_asset(self, user_id: int, asset_id: str) -> dict:
        """Return a project upload that may be imported into a business resource."""
        with sqlite3.connect(self.database_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM file_assets WHERE id=?", (asset_id,)).fetchone()
        if row is None:
            raise PermissionError("Uploaded file is not accessible")
        asset = dict(row)
        if not asset["project_id"]:
            raise PermissionError("Independent uploads cannot become project datasets")
        if self.project_access is None or self.project_access(
            user_id, asset["project_id"], "file:read"
        ) is not True:
            raise PermissionError("Project file access unavailable")
        return asset


class LocalFileAssetGateway:
    """Adapter the business tool layer uses for project uploads.

    It exposes only three operations so the tool layer cannot browse the whole
    upload table: resolve one asset for a principal, materialise its bytes into
    a temporary path, and bind a registered business resource to it.
    """

    def __init__(self, database_path: str | Path, storage, project_access=None,
                 work_dir: str | Path | None = None) -> None:
        self.access = FileAccess(database_path, project_access=project_access)
        self.storage = storage
        self.work_dir = str(work_dir) if work_dir is not None else None

    def resolve(self, principal: int, asset_id: str) -> dict:
        return self.access.require_import_asset(principal, asset_id)

    def materialize(self, bucket: str, object_key: str) -> str:
        """Copy the stored object into a private temporary file."""
        suffix = Path(object_key).suffix.lower() or ".bin"
        with tempfile.NamedTemporaryFile(
            dir=self.work_dir, suffix=suffix, delete=False
        ) as output:
            destination = Path(output.name)
        try:
            self.storage.download(bucket, object_key, str(destination))
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return str(destination)

    def bind(self, asset_id: str, business_type: str, business_id: str) -> None:
        self.access.link_business(asset_id, business_type, business_id)

    def bind_with(self, conn, asset_id: str, business_type: str, business_id: str) -> None:
        """Bind inside the caller's registration transaction."""
        self.access.link_business_with(conn, asset_id, business_type, business_id)

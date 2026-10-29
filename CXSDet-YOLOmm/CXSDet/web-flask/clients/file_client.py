"""Local adapter for backend-owned file operations.

The backend must never call its own public file HTTP route to read model,
dataset or feedback artifacts. HTTP authorization belongs at the route;
business callers must establish resource permission before using this client.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote

from config import FILE_STORE_CONFIG, DB_PATH


class FileClient:
    """Compatibility API backed by the local file store instead of HTTP."""

    def __init__(
        self,
        base_path: str | Path | None = None,
        access_url: str | None = None,
        max_size: int | None = None,
        database_path: str | Path | None = None,
    ) -> None:
        self.base_path = Path(base_path or FILE_STORE_CONFIG["base_path"]).resolve()
        self.server_url = (access_url or FILE_STORE_CONFIG["access_url"]).rstrip("/")
        self.max_size = max_size if max_size is not None else FILE_STORE_CONFIG["max_size"]
        self.database_path = str(database_path) if database_path else None

    def _database(self):
        from flask import current_app, has_app_context
        return self.database_path or (current_app.config['DATABASE'] if has_app_context() else DB_PATH)

    def _path(self, bucket: str, object_key: str) -> Path:
        if (
            not isinstance(bucket, str)
            or not bucket
            or bucket in {".", ".."}
            or any(char in bucket for char in "/\\:\x00")
        ):
            raise ValueError("Invalid storage bucket")
        if not isinstance(object_key, str) or not object_key or "\\" in object_key:
            raise ValueError("Invalid object key")
        parts = object_key.split("/")
        if any(not part or part in {".", ".."} or ":" in part or "\x00" in part for part in parts):
            raise ValueError("Invalid object key")
        if not bucket.startswith('workspace.'):
            from services.project_storage import legacy_location
            alias = legacy_location(self._database(), bucket, object_key)
            if alias:
                if not alias[0].startswith('workspace.'):
                    raise ValueError('Invalid workspace file alias')
                return self._path(*alias)
        if bucket.startswith('workspace.'):
            from services.project_storage import root_for
            parts_bucket = bucket.split('.')
            if len(parts_bucket) != 3:
                raise ValueError('Invalid workspace bucket')
            root = root_for(self._database(), parts_bucket[1])
            if root is None:
                raise ValueError('项目文件夹未绑定，不能读取或写入项目文件')
            bucket_path = root / 'artifacts' / parts_bucket[2]
            if bucket_path.resolve() != bucket_path.absolute():
                raise ValueError('Workspace storage contains redirected directories')
        else:
            root = self.base_path
            bucket_path = (root / bucket).resolve()
        if not bucket_path.is_relative_to(root):
            raise ValueError("Storage bucket escapes configured root")
        path = bucket_path.joinpath(*parts).resolve()
        if not path.is_relative_to(bucket_path) or (bucket.startswith('workspace.') and path != bucket_path.joinpath(*parts).absolute()):
            raise ValueError("Object key escapes storage bucket")
        return path

    def upload(self, bucket: str, file_path: str, is_cache: bool = False) -> dict:
        from services.project_storage import current_scope
        scope = current_scope()
        if scope and bucket != 'avatars' and Path(self._database()).resolve() != Path(scope[0]).resolve():
            raise ValueError('文件存储与项目数据库不一致')
        if scope and bucket.startswith('workspace.') and not bucket.startswith(f'workspace.{scope[1]}.'):
            raise ValueError('不能向其他项目的文件目录写入')
        if scope and bucket != 'avatars' and not bucket.startswith('workspace.'):
            bucket = f'workspace.{scope[1]}.{bucket}'
        source = Path(file_path)
        if not source.is_file():
            raise FileNotFoundError(file_path)
        extension = ".nii.gz" if source.name.lower().endswith(".nii.gz") else source.suffix
        object_key = source.name if is_cache else f"{uuid.uuid4()}{extension}"
        destination = self._path(bucket, object_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
                temporary = Path(output.name)
                with source.open("rb") as input_file:
                    size = 0
                    while chunk := input_file.read(1024 * 1024):
                        size += len(chunk)
                        if size > self.max_size:
                            raise ValueError("File exceeds storage size limit")
                        output.write(chunk)
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {
            "url": self.get_file_url(bucket, object_key),
            "bucket": bucket,
            "objectKey": object_key,
        }

    def delete(self, bucket: str, object_key: str) -> None:
        self._path(bucket, object_key).unlink(missing_ok=True)

    def exists(self, bucket: str, object_key: str) -> bool:
        """Whether the stored object is present, without reading its bytes."""
        try:
            return self._path(bucket, object_key).is_file()
        except (ValueError, OSError):
            return False

    def get(self, bucket: str, object_key: str) -> bytes:
        return self._path(bucket, object_key).read_bytes()

    def get_file_url(self, bucket: str, object_key: str) -> str:
        self._path(bucket, object_key)
        return f"{self.server_url}/{quote(bucket)}/{quote(object_key, safe='/')}"

    def download(self, bucket: str, object_key: str, save_path: str) -> None:
        source = self._path(bucket, object_key)
        destination = Path(save_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
                temporary = Path(output.name)
            shutil.copyfile(source, temporary)
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


file_client = FileClient()

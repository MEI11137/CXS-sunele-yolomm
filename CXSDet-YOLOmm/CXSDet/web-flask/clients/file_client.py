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

from config import FILE_STORE_CONFIG


class FileClient:
    """Compatibility API backed by the local file store instead of HTTP."""

    def __init__(
        self,
        base_path: str | Path | None = None,
        access_url: str | None = None,
        max_size: int | None = None,
    ) -> None:
        self.base_path = Path(base_path or FILE_STORE_CONFIG["base_path"]).resolve()
        self.server_url = (access_url or FILE_STORE_CONFIG["access_url"]).rstrip("/")
        self.max_size = max_size if max_size is not None else FILE_STORE_CONFIG["max_size"]

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
        bucket_path = (self.base_path / bucket).resolve()
        if not bucket_path.is_relative_to(self.base_path):
            raise ValueError("Storage bucket escapes configured root")
        path = bucket_path.joinpath(*parts).resolve()
        if not path.is_relative_to(bucket_path):
            raise ValueError("Object key escapes storage bucket")
        return path

    def upload(self, bucket: str, file_path: str, is_cache: bool = False) -> dict:
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

"""C-02: backend file operations must not depend on public HTTP routes."""

from pathlib import Path
import shutil

import pytest

from clients.file_client import FileClient


def test_internal_file_round_trip_without_http(tmp_path, monkeypatch):
    def reject_http(*args, **kwargs):
        raise AssertionError("internal file operation attempted HTTP")

    monkeypatch.setattr("requests.post", reject_http)
    monkeypatch.setattr("requests.get", reject_http)
    monkeypatch.setattr("requests.delete", reject_http)
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\ninternal-test")
    client = FileClient(base_path=tmp_path / "files", access_url="http://localhost/api/file")

    saved = client.upload("documents", str(source))
    assert saved["bucket"] == "documents"
    assert saved["objectKey"].endswith(".pdf")
    assert client.get("documents", saved["objectKey"]) == source.read_bytes()
    destination = tmp_path / "download" / "copy.pdf"
    client.download("documents", saved["objectKey"], str(destination))
    assert destination.read_bytes() == source.read_bytes()
    client.delete("documents", saved["objectKey"])
    with pytest.raises(FileNotFoundError):
        client.get("documents", saved["objectKey"])


@pytest.mark.parametrize(
    "bucket,key",
    [
        ("..", "secret.txt"),
        ("../other", "secret.txt"),
        ("documents", "../secret.txt"),
        ("documents", "nested/../../secret.txt"),
        ("documents", "C:\\secret.txt"),
    ],
)
def test_internal_file_rejects_path_escape(tmp_path, bucket, key):
    client = FileClient(base_path=tmp_path / "files")
    with pytest.raises(ValueError):
        client.get(bucket, key)
    with pytest.raises(ValueError):
        client.delete(bucket, key)


def test_internal_file_enforces_size_limit_without_partial_object(tmp_path):
    source = tmp_path / "large.zip"
    source.write_bytes(b"123456")
    client = FileClient(base_path=tmp_path / "files", max_size=5)
    with pytest.raises(ValueError):
        client.upload("datasets", str(source))
    assert not list((tmp_path / "files" / "datasets").glob("*"))


def test_internal_file_rejects_symlink_outside_root(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    bucket = tmp_path / "files" / "documents"
    bucket.mkdir(parents=True)
    link = bucket / "alias.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation unavailable on this Windows host")
    client = FileClient(base_path=tmp_path / "files")
    with pytest.raises(ValueError):
        client.get("documents", "alias.txt")


def test_failed_download_does_not_replace_cached_model(tmp_path, monkeypatch):
    source = tmp_path / "files" / "models" / "source.pt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"valid-weight")
    destination = tmp_path / "cache" / "model.pt"
    destination.parent.mkdir()
    destination.write_bytes(b"previous-weight")

    def partial_copy(_source, target):
        Path(target).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copyfile", partial_copy)
    client = FileClient(base_path=tmp_path / "files")
    with pytest.raises(OSError):
        client.download("models", "source.pt", str(destination))
    assert destination.read_bytes() == b"previous-weight"

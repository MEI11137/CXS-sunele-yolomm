import os
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


@pytest.fixture
def isolated_app_paths(tmp_path, monkeypatch):
    db_path = tmp_path / "test.sqlite3"
    temp_dir = tmp_path / "temp"
    file_store = tmp_path / "file_store"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("FLASK_TEMP_DIR", str(temp_dir))
    monkeypatch.setenv("FILE_STORE_PATH", str(file_store))
    return {
        "db_path": db_path,
        "temp_dir": temp_dir,
        "file_store": file_store,
    }

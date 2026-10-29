"""File HTTP requests require current account ownership, including previews."""

import io
import sqlite3

import pytest
from flask import Flask
from PIL import Image

from routes.file import file_bp
from services.file_access import FileAccess
from utils.jwt_util import generate_token, get_user_id


@pytest.fixture
def file_app(tmp_path):
    database = tmp_path / "files.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE user (id INTEGER PRIMARY KEY, avatar_bucket TEXT, avatar_object_key TEXT)"
        )
        conn.executemany("INSERT INTO user VALUES (?, NULL, NULL)", [(7,), (8,)])
        conn.execute(
            "CREATE TABLE detection_records (id TEXT PRIMARY KEY, user_id INTEGER, "
            "original_image_bucket TEXT, original_image_object_key TEXT, "
            "result_image_bucket TEXT, result_image_object_key TEXT, "
            "secondary_image_bucket TEXT, secondary_image_object_key TEXT, "
            "enhanced_image_bucket TEXT, enhanced_image_object_key TEXT)"
        )
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        DATABASE=str(database),
        FILE_STORE_PATH=str(tmp_path / "store"),
        TEMP_DIR=str(tmp_path / "temp"),
    )
    app.register_blueprint(file_bp)
    return app


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, format="PNG")
    return output.getvalue()


def test_avatar_upload_and_download_require_current_owner(file_app):
    client = file_app.test_client()
    anonymous = client.post(
        "/api/file/upload/avatars", data={"file": (io.BytesIO(_png()), "a.png")}
    )
    assert anonymous.status_code == 401

    uploaded = client.post(
        "/api/file/upload/avatars",
        headers={"Authorization": f"Bearer {generate_token(7)}"},
        data={"file": (io.BytesIO(_png()), "a.png")},
    )
    assert uploaded.status_code == 200
    key = uploaded.json["data"]["objectKey"]
    path = f"/api/file/avatars/{key}"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": f"Bearer {generate_token(8)}"}).status_code == 403
    owner = client.get(path, headers={"Authorization": f"Bearer {generate_token(7)}"})
    assert owner.status_code == 200
    assert owner.data == _png()
    client.set_cookie("cxs_file_session", generate_token(7), path="/api/file")
    assert client.get(path).data == _png()


def test_existing_detection_file_is_only_visible_to_record_owner(file_app):
    store = file_app.config["FILE_STORE_PATH"]
    from pathlib import Path

    image = Path(store) / "detection_original" / "existing.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(_png())
    with sqlite3.connect(file_app.config["DATABASE"]) as conn:
        conn.execute(
            "INSERT INTO detection_records (id, user_id, original_image_bucket, "
            "original_image_object_key) VALUES ('r1', 7, 'detection_original', 'existing.png')"
        )
    client = file_app.test_client()
    path = "/api/file/detection_original/existing.png"
    assert client.get(path, headers={"Authorization": f"Bearer {generate_token(7)}"}).status_code == 200
    assert client.get(path, headers={"Authorization": f"Bearer {generate_token(8)}"}).status_code == 403
    assert client.get(path, headers={"Authorization": f"Bearer {generate_token(99)}"}).status_code == 401


def test_file_route_rejects_unreferenced_and_unsafe_object(file_app):
    client = file_app.test_client()
    token = {"Authorization": f"Bearer {generate_token(7)}"}
    store = file_app.config["FILE_STORE_PATH"]
    from pathlib import Path

    unreferenced = Path(store) / "avatars" / "unreferenced.png"
    unreferenced.parent.mkdir(parents=True)
    unreferenced.write_bytes(_png())
    assert client.get("/api/file/avatars/unreferenced.png", headers=token).status_code == 403
    assert client.get("/api/file/avatars/..%5Csecret.txt", headers=token).status_code == 400
    assert client.delete("/api/file/avatars/unreferenced.png").status_code == 401


def test_public_upload_rejects_non_avatar_bucket_and_fake_image(file_app):
    client = file_app.test_client()
    token = {"Authorization": f"Bearer {generate_token(7)}"}
    dataset = client.post(
        "/api/file/upload/datasets", headers=token,
        data={"file": (io.BytesIO(b"not a zip"), "fake.zip")},
    )
    assert dataset.status_code == 403
    fake_avatar = client.post(
        "/api/file/upload/avatars", headers=token,
        data={"file": (io.BytesIO(b"not an image"), "fake.png")},
    )
    assert fake_avatar.status_code == 400


def test_login_sets_http_only_file_preview_cookie_and_logout_clears_it(monkeypatch):
    from routes import user as user_route

    token = generate_token(7)

    class LoginResult:
        def to_dict(self):
            return {"userInfo": {"id": 7}, "token": token}

    monkeypatch.setattr(user_route.user_service, "login", lambda *_: LoginResult())
    app = Flask(__name__)
    app.register_blueprint(user_route.user_bp)
    client = app.test_client()
    login = client.post("/api/user/login", json={"username": "u", "password": "p"})
    cookie = login.headers.get("Set-Cookie", "")
    assert "cxs_file_session=" in cookie
    assert "HttpOnly" in cookie
    assert "Path=/api/file" in cookie
    assert "SameSite=Lax" in cookie
    logout = client.post("/api/user/logout", headers={"Authorization": f"Bearer {token}"})
    assert "cxs_file_session=" in logout.headers.get("Set-Cookie", "")
    assert "Max-Age=0" in logout.headers.get("Set-Cookie", "")


def test_known_legacy_signing_secret_cannot_forge_file_identity():
    import time
    import jwt

    forged = jwt.encode(
        {"user_id": 7, "exp": int(time.time()) + 3600},
        "gjq-web-flask-secret", algorithm="HS256",
    )
    assert get_user_id(forged) is None


def test_project_attachment_requires_policy_and_preserves_upload_state(file_app):
    client = file_app.test_client()
    token = {"Authorization": f"Bearer {generate_token(7)}"}
    without_policy = client.post(
        "/api/file/project/p1/upload", headers=token,
        data={"file": (io.BytesIO(b"%PDF-1.4\nexample"), "manual.pdf")},
    )
    assert without_policy.status_code == 403

    file_app.config["PROJECT_FILE_ACCESS"] = (
        lambda user_id, project_id, action: user_id == 7 and project_id == "p1"
    )
    uploaded = client.post(
        "/api/file/project/p1/upload", headers=token,
        data={"file": (io.BytesIO(b"%PDF-1.4\nexample"), "manual.pdf")},
    )
    assert uploaded.status_code == 200
    data = uploaded.json["data"]
    assert data["status"] == "uploaded"
    assert data["project_id"] == "p1"
    assert data["version"] == 1
    metadata = client.get(
        f"/api/file/metadata/{data['file_id']}", headers=token,
    )
    assert metadata.json["data"]["original_name"] == "manual.pdf"
    assert client.get(
        f"/api/file/metadata/{data['file_id']}",
        headers={"Authorization": f"Bearer {generate_token(8)}"},
    ).status_code == 403
    assert client.get(data["url"], headers=token).data == b"%PDF-1.4\nexample"


def test_project_member_can_read_attachment_when_policy_allows(file_app):
    def policy(user_id, project_id, action):
        return project_id == "p1" and (
            (user_id == 7 and action == "file:create") or
            (user_id in {7, 8} and action == "file:read")
        )

    file_app.config["PROJECT_FILE_ACCESS"] = policy
    client = file_app.test_client()
    uploaded = client.post(
        "/api/file/project/p1/upload",
        headers={"Authorization": f"Bearer {generate_token(7)}"},
        data={"file": (io.BytesIO(b"%PDF-1.4\nexample"), "manual.pdf")},
    )
    assert uploaded.status_code == 200
    asset = uploaded.json["data"]
    member = {"Authorization": f"Bearer {generate_token(8)}"}
    assert client.get(asset["url"], headers=member).data == b"%PDF-1.4\nexample"
    assert client.get(f"/api/file/metadata/{asset['file_id']}", headers=member).status_code == 200


def test_project_upload_rejects_invalid_zip_and_docx(file_app):
    file_app.config["PROJECT_FILE_ACCESS"] = lambda *_: True
    client = file_app.test_client()
    token = {"Authorization": f"Bearer {generate_token(7)}"}
    for filename in ("bad.zip", "bad.docx"):
        response = client.post(
            "/api/file/project/p1/upload", headers=token,
            data={"file": (io.BytesIO(b"not a package"), filename)},
        )
        assert response.status_code == 400
    mislabeled_image = client.post(
        "/api/file/project/p1/upload", headers=token,
        data={"file": (io.BytesIO(_png()), "looks-like-jpeg.jpg")},
    )
    assert mislabeled_image.status_code == 400
    old_word = client.post(
        "/api/file/project/p1/upload", headers=token,
        data={"file": (io.BytesIO(b"legacy"), "manual.doc")},
    )
    assert old_word.status_code == 400
    assert "DOCX" in old_word.json["msg"]


def test_project_permission_revoked_during_upload_removes_saved_object(file_app):
    calls = 0

    def changing_policy(user_id, project_id, action):
        nonlocal calls
        calls += 1
        return calls == 1

    file_app.config["PROJECT_FILE_ACCESS"] = changing_policy
    client = file_app.test_client()
    response = client.post(
        "/api/file/project/p1/upload",
        headers={"Authorization": f"Bearer {generate_token(7)}"},
        data={"file": (io.BytesIO(b"%PDF-1.4\nexample"), "manual.pdf")},
    )
    assert response.status_code == 403
    from pathlib import Path
    assert not list((Path(file_app.config["FILE_STORE_PATH"]) / "project_uploads").glob("*"))


def test_project_policy_must_explicitly_allow_access(file_app):
    file_app.config["PROJECT_FILE_ACCESS"] = lambda *_: None
    client = file_app.test_client()
    response = client.post(
        "/api/file/project/p1/upload",
        headers={"Authorization": f"Bearer {generate_token(7)}"},
        data={"file": (io.BytesIO(b"%PDF-1.4\nexample"), "manual.pdf")},
    )
    assert response.status_code == 403
    access = FileAccess(file_app.config["DATABASE"], project_access=lambda *_: True)
    asset_id = access.register_upload(7, "project_uploads", "p1.pdf", "document", "application/pdf", 10, "p1")
    denied = FileAccess(file_app.config["DATABASE"], project_access=lambda *_: None)
    with pytest.raises(PermissionError):
        denied.require_read(7, "project_uploads", "p1.pdf")
    with pytest.raises(PermissionError):
        denied.get_asset(7, asset_id)


def test_avatar_reference_cannot_grant_access_to_legacy_detection_file(file_app):
    from pathlib import Path

    target = Path(file_app.config["FILE_STORE_PATH"]) / "detection_original" / "private.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(_png())
    with sqlite3.connect(file_app.config["DATABASE"]) as conn:
        conn.execute(
            "INSERT INTO detection_records (id, user_id, original_image_bucket, "
            "original_image_object_key) VALUES ('private', 7, 'detection_original', 'private.png')"
        )
        conn.execute(
            "UPDATE user SET avatar_bucket='detection_original', avatar_object_key='private.png' WHERE id=8"
        )
    response = file_app.test_client().get(
        "/api/file/detection_original/private.png",
        headers={"Authorization": f"Bearer {generate_token(8)}"},
    )
    assert response.status_code == 403


def test_profile_rejects_unowned_avatar_and_role_escalation(file_app, monkeypatch):
    from routes import user as user_route
    from services import user_service as user_module

    class FakeDB:
        db_path = file_app.config["DATABASE"]
        calls = []

        def query(self, *_args, **_kwargs):
            return {"id": 7, "avatar_bucket": None, "avatar_object_key": None}

        def execute(self, *args):
            self.calls.append(args)
            return 1

    database = FakeDB()
    monkeypatch.setattr(user_module, "db", database)
    monkeypatch.setattr(user_route, "is_admin", lambda: False)
    file_app.register_blueprint(user_route.user_bp)
    client = file_app.test_client()
    headers = {"Authorization": f"Bearer {generate_token(7)}"}
    stolen = client.put(
        "/api/user", headers=headers,
        json={"id": 7, "avatarBucket": "detection_original", "avatarObjectKey": "private.png"},
    )
    assert stolen.json["code"] == 403
    elevated = client.put("/api/user", headers=headers, json={"id": 7, "role": 1})
    assert elevated.json["code"] == 403
    assert database.calls == []


def test_detection_image_tools_check_file_owner_before_reading(file_app, monkeypatch):
    from routes.detection import detection_bp
    from routes import detection as detection_route

    file_app.register_blueprint(detection_bp)
    with sqlite3.connect(file_app.config["DATABASE"]) as conn:
        conn.execute(
            "INSERT INTO detection_records (id, user_id, original_image_bucket, "
            "original_image_object_key) VALUES ('private', 7, 'detection_original', 'private.png')"
        )
    monkeypatch.setattr(detection_route.file_client, "get", lambda *_: pytest.fail("unauthorized read"))
    client = file_app.test_client()
    headers = {"Authorization": f"Bearer {generate_token(8)}"}
    payload = {"bucket": "detection_original", "object_key": "private.png"}
    for endpoint in ("enhance-image", "analyze-defects"):
        response = client.post(f"/api/detection/{endpoint}", headers=headers, json=payload)
        assert response.json["code"] == 403


def test_detection_save_cannot_claim_or_delete_foreign_file(file_app, monkeypatch):
    from routes.detection import detection_bp
    from routes import detection as detection_route

    file_app.register_blueprint(detection_bp)
    with sqlite3.connect(file_app.config["DATABASE"]) as conn:
        conn.execute("ALTER TABLE detection_records ADD COLUMN review_version INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE detection_records ADD COLUMN record_status TEXT DEFAULT 'completed'")
        conn.execute(
            "INSERT INTO detection_records (id, user_id, original_image_bucket, "
            "original_image_object_key) VALUES ('victim', 7, 'detection_original', 'private.png')"
        )
        conn.execute("INSERT INTO detection_records (id,user_id,original_image_bucket,original_image_object_key) VALUES ('forged',8,'detection_original','private.png')")

    class FakeDB:
        def get_connection(self):
            return sqlite3.connect(file_app.config['DATABASE'])
        def query(self, *_args, **_kwargs):
            return {
                "original_image_bucket": "detection_original",
                "original_image_object_key": "private.png",
                "result_image_bucket": None,
                "result_image_object_key": None,
                "enhanced_image_bucket": None,
                "enhanced_image_object_key": None,
            }

        def execute(self, *_args, **_kwargs):
            return 1

    monkeypatch.setattr(detection_route, "db", FakeDB())
    monkeypatch.setattr(detection_route, "is_admin", lambda: False)
    monkeypatch.setattr(detection_route.file_client, "delete", lambda *_: pytest.fail("foreign delete"))
    client = file_app.test_client()
    headers = {"Authorization": f"Bearer {generate_token(8)}"}
    denied = client.post(
        "/api/detection/save", headers=headers,
        json={
            "title": "forged",
            "save_data": {
                "original_image_bucket": "detection_original",
                "original_image_object_key": "private.png",
            },
        },
    )
    assert denied.json["code"] == 403
    removed = client.delete("/api/detection/records/forged", headers=headers)
    assert removed.json["code"] == 200

"""资料库与手册接口。

资料是**项目 + 所有者**范围内的数据：所有路径都先过 ``documents.get_owned`` /
``manuals.get_owned``，没有“按 ID 直取”的免鉴权入口。下载走 ``send_file`` 且强制
下载名，避免浏览器把上传内容当页面渲染。
"""

from __future__ import annotations

from flask import Blueprint, request, send_file

from backend import auth, db
from backend.errors import ApiError, ok
from backend.services import documents as document_service
from backend.services import manuals as manual_service
from backend.services import projects as project_service

bp = Blueprint("documents", __name__)


def _user() -> dict:
    user = auth.current_user()
    assert user is not None
    return user


def _flag(name: str) -> bool:
    return str(request.args.get(name) or "").lower() in {"1", "true", "yes"}


@bp.get("/projects/<project_id>/documents")
@auth.login_required
def list_documents(project_id: str):
    conn = db.get_db()
    # 别人的项目按“不存在”处理：空列表会被误读成“这个项目没有资料”。
    project_service.get_owned(conn, project_id, int(_user()["id"]))
    return ok(
        {
            **document_service.list_for_project(
                conn, int(_user()["id"]), project_id, include_deleted=_flag("include_deleted")
            ),
            "stats": document_service.stats(conn, int(_user()["id"]), project_id),
        }
    )


@bp.post("/projects/<project_id>/documents")
@auth.login_required
def upload_document(project_id: str):
    """multipart 上传，或 JSON 登记项目目录里已有的文件（二选一）。"""
    conn = db.get_db()
    user_id = int(_user()["id"])
    upload = request.files.get("file")
    if upload is not None and upload.filename:
        name = (request.form.get("name") or "").strip() or None
        payload = document_service.ingest_bytes(
            conn, user_id, project_id, name=name or upload.filename, filename=upload.filename, data=upload.read()
        )
        return ok({"document": payload})
    body = request.get_json(silent=True) or {}
    rel_path = str(body.get("rel_path") or "").strip()
    if not rel_path:
        raise ApiError(400, "FILE_REQUIRED", "需要上传文件，或提供项目内相对路径 rel_path")
    document = document_service.ingest_path(
        conn, user_id, project_id, rel_path, name=body.get("name")
    )
    return ok({"document": document})


@bp.get("/projects/<project_id>/documents/search")
@auth.login_required
def search_documents(project_id: str):
    conn = db.get_db()
    query = request.args.get("q") or ""
    document_id = (request.args.get("document_id") or "").strip() or None
    return ok(
        document_service.search(
            conn, int(_user()["id"]), project_id, query, document_id=document_id
        )
    )


@bp.get("/projects/<project_id>/documents/<document_id>")
@auth.login_required
def get_document(project_id: str, document_id: str):
    conn = db.get_db()
    row = document_service.get_owned(conn, int(_user()["id"]), project_id, document_id)
    return ok({"document": document_service.public(row)})


@bp.get("/projects/<project_id>/documents/<document_id>/pages")
@auth.login_required
def document_pages(project_id: str, document_id: str):
    conn = db.get_db()
    try:
        page = int(request.args.get("page", "1"))
        span = int(request.args.get("span", "1"))
    except ValueError:
        raise ApiError(400, "VALIDATION", "page/span 必须是整数") from None
    return ok(
        document_service.pages_window(
            conn, int(_user()["id"]), project_id, document_id, page=page, span=span
        )
    )


@bp.get("/projects/<project_id>/documents/<document_id>/download")
@auth.login_required
def download_document(project_id: str, document_id: str):
    conn = db.get_db()
    row, path = document_service.file_path(conn, int(_user()["id"]), project_id, document_id)
    return send_file(path, as_attachment=True, download_name=str(row["filename"]))


@bp.delete("/projects/<project_id>/documents/<document_id>")
@auth.login_required
def delete_document(project_id: str, document_id: str):
    conn = db.get_db()
    return ok(
        {"document": document_service.delete(conn, int(_user()["id"]), project_id, document_id)}
    )


@bp.post("/projects/<project_id>/documents/<document_id>/restore")
@auth.login_required
def restore_document(project_id: str, document_id: str):
    conn = db.get_db()
    return ok(
        {"document": document_service.restore(conn, int(_user()["id"]), project_id, document_id)}
    )


# --------------------------------------------------------------------------- 手册


@bp.get("/projects/<project_id>/manuals")
@auth.login_required
def list_manuals(project_id: str):
    conn = db.get_db()
    project_service.get_owned(conn, project_id, int(_user()["id"]))
    include_deleted = (request.args.get("trash") or "").strip().lower() in {"1", "true", "yes"}
    return ok(
        manual_service.list_manuals(
            conn, int(_user()["id"]), project_id, include_deleted=include_deleted
        )
    )


@bp.post("/projects/<project_id>/manuals")
@auth.login_required
def create_manual(project_id: str):
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    return ok(
        manual_service.create(
            conn,
            int(_user()["id"]),
            project_id,
            name=str(payload.get("name") or ""),
            content=str(payload.get("content") or ""),
            description=str(payload.get("description") or ""),
        )
    )


@bp.get("/projects/<project_id>/manuals/<manual_id>")
@auth.login_required
def read_manual(project_id: str, manual_id: str):
    conn = db.get_db()
    return ok(manual_service.read(conn, int(_user()["id"]), project_id, manual_id))


@bp.patch("/projects/<project_id>/manuals/<manual_id>")
@auth.login_required
def update_manual(project_id: str, manual_id: str):
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    return ok(
        manual_service.update_meta(
            conn,
            int(_user()["id"]),
            project_id,
            manual_id,
            name=payload.get("name"),
            description=payload.get("description"),
        )
    )


@bp.post("/projects/<project_id>/manuals/<manual_id>/versions")
@auth.login_required
def add_manual_version(project_id: str, manual_id: str):
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    return ok(
        manual_service.add_version(
            conn,
            int(_user()["id"]),
            project_id,
            manual_id,
            content=str(payload.get("content") or ""),
            note=str(payload.get("note") or ""),
        )
    )


@bp.post("/projects/<project_id>/manuals/<manual_id>/versions/<version_id>/activate")
@auth.login_required
def activate_manual_version(project_id: str, manual_id: str, version_id: str):
    conn = db.get_db()
    return ok(
        manual_service.activate(conn, int(_user()["id"]), project_id, manual_id, version_id)
    )


@bp.delete("/projects/<project_id>/manuals/<manual_id>")
@auth.login_required
def delete_manual(project_id: str, manual_id: str):
    conn = db.get_db()
    return ok(manual_service.delete(conn, int(_user()["id"]), project_id, manual_id))


@bp.post("/projects/<project_id>/manuals/<manual_id>/restore")
@auth.login_required
def restore_manual(project_id: str, manual_id: str):
    conn = db.get_db()
    return ok(manual_service.restore(conn, int(_user()["id"]), project_id, manual_id))

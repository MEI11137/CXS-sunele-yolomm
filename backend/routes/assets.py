"""资产接口：上传（multipart）、登记本地文件、列表、元信息、内容流。

* multipart 只在**这一个**已鉴权路由上放行（见 ``auth.MULTIPART_ENDPOINTS``），
  其它写接口仍然只接受 JSON，不放宽整体 CSRF 策略；
* 内容流永远先按 owner+project 校验资产归属，再做路径解析与 SHA 校验，
  没有“按 key 直接取桶”的免鉴权入口；
* 图片默认 inline 便于预览，其它类型强制下载，避免浏览器把压缩包当页面渲染。
"""

from __future__ import annotations

from flask import Blueprint, request, send_file

from backend import auth, db
from backend.errors import ApiError, ok
from backend.services import assets as asset_service
from backend.services import projects as project_service
from backend.services import storage

bp = Blueprint("assets", __name__)

MAX_UPLOAD_BYTES = 512 * 1024 * 1024
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}


def _user() -> dict:
    user = auth.current_user()
    assert user is not None
    return user


def _project_id_project(conn, project_id: str):
    return project_service.get_owned(conn, project_id, int(_user()["id"]))


@bp.post("/projects/<project_id>/assets/upload")
@auth.login_required
def upload_asset(project_id: str):
    length = request.content_length or 0
    if length > MAX_UPLOAD_BYTES:
        raise ApiError(413, "FILE_TOO_LARGE", "上传内容超过 512MB 上限")
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise ApiError(400, "FILE_REQUIRED", "缺少上传文件（表单字段 file）")
    data = upload.read()
    conn = db.get_db()
    _project_id_project(conn, project_id)
    session_id = (request.form.get("session_id") or "").strip() or None
    if session_id:
        owned = conn.execute(
            "SELECT id FROM sessions WHERE id = ? AND project_id = ? AND owner_id = ?",
            (session_id, str(project_id), int(_user()["id"])),
        ).fetchone()
        if owned is None:
            raise ApiError(404, "SESSION_NOT_FOUND", "会话不存在或不属于当前项目")
    asset = asset_service.add_bytes(
        conn,
        int(_user()["id"]),
        project_id,
        data=data,
        filename=upload.filename,
        role=(request.form.get("role") or "").strip() or None,
        session_id=session_id,
    )
    return ok({"asset": asset})


@bp.post("/projects/<project_id>/assets/register")
@auth.login_required
def register_asset(project_id: str):
    payload = request.get_json(silent=True) or {}
    rel_path = str(payload.get("rel_path") or "").strip()
    if not rel_path:
        raise ApiError(400, "PATH_REQUIRED", "需要 rel_path（项目目录内的相对路径）")
    conn = db.get_db()
    asset = asset_service.register_local(
        conn,
        int(_user()["id"]),
        project_id,
        rel_path,
        role=str(payload.get("role") or "").strip() or None,
    )
    return ok({"asset": asset})


@bp.get("/projects/<project_id>/assets")
@auth.login_required
def list_assets(project_id: str):
    conn = db.get_db()
    result = asset_service.list_for_project(
        conn,
        int(_user()["id"]),
        project_id,
        kind=(request.args.get("kind") or "").strip() or None,
        role=(request.args.get("role") or "").strip() or None,
        limit=int(request.args.get("limit") or 50),
        before=(request.args.get("before") or "").strip() or None,
    )
    return ok(result)


@bp.get("/projects/<project_id>/assets/candidates")
@auth.login_required
def list_candidates(project_id: str):
    """列出项目目录里可以“登记”的候选文件（图片 / ZIP / PT），不递归深挖。"""
    conn = db.get_db()
    root = storage.project_root(conn, int(_user()["id"]), project_id)
    rel = (request.args.get("path") or ".").strip()
    folder = storage.resolve_rel(root, rel)
    if not folder.is_dir():
        raise ApiError(404, "DIR_NOT_FOUND", "目录不存在")
    entries: list[dict] = []
    for path in sorted(folder.iterdir()):
        if path.is_dir():
            continue
        suffix = path.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            kind = "image"
        elif suffix == ".zip":
            kind = "archive"
        elif suffix == ".pt":
            kind = "weight"
        else:
            continue
        entries.append(
            {
                "rel_path": storage.relative(root, path),
                "name": path.name,
                "kind": kind,
                "size": path.stat().st_size,
            }
        )
    return ok({"path": rel, "entries": entries[:500]})


@bp.get("/projects/<project_id>/assets/<asset_id>")
@auth.login_required
def get_asset(project_id: str, asset_id: str):
    conn = db.get_db()
    row = asset_service.get_owned(conn, int(_user()["id"]), project_id, asset_id)
    return ok({"asset": asset_service.public(row)})


@bp.get("/projects/<project_id>/assets/<asset_id>/content")
@auth.login_required
def get_asset_content(project_id: str, asset_id: str):
    conn = db.get_db()
    row = asset_service.get_owned(conn, int(_user()["id"]), project_id, asset_id)
    path = asset_service.path_of(conn, int(_user()["id"]), project_id, asset_id)
    inline = str(row["kind"]) == "image"
    return send_file(
        path,
        mimetype=str(row["mime"]),
        as_attachment=not inline,
        download_name=str(row["filename"]),
        conditional=True,
        max_age=0,
    )

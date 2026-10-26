"""Authenticated file HTTP boundary; backend jobs use the local FileClient."""

from __future__ import annotations

import io
import hashlib
import tempfile
import zipfile
from pathlib import Path

from flask import Blueprint, current_app, request, send_file
from PIL import Image, UnidentifiedImageError

from clients.file_client import FileClient
from config import FILE_STORE_CONFIG
from services.file_access import FileAccess
from utils.jwt_util import get_user_id
from utils.response import error, success


file_bp = Blueprint("file_bp", __name__, url_prefix="/api/file")
_AVATAR_LIMIT = 2 * 1024 * 1024
_PROJECT_TYPES = {
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".png": ("image", "image/png"),
    ".tif": ("image", "image/tiff"),
    ".tiff": ("image", "image/tiff"),
    ".zip": ("dataset_archive", "application/zip"),
    ".pt": ("model_weight", "application/octet-stream"),
    ".pdf": ("document", "application/pdf"),
    ".docx": ("document", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".md": ("manual", "text/markdown"),
}


def _identity(allow_cookie: bool = False) -> int | None:
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else None
    if token is None and allow_cookie:
        token = request.cookies.get("cxs_file_session")
    user_id = get_user_id(token) if token else None
    if user_id is None:
        return None
    try:
        _access().require_account(user_id)
    except PermissionError:
        return None
    return user_id


def _storage() -> FileClient:
    return FileClient(base_path=current_app.config["FILE_STORE_PATH"])


def _access() -> FileAccess:
    return FileAccess(
        current_app.config["DATABASE"],
        project_access=current_app.config.get("PROJECT_FILE_ACCESS"),
    )


@file_bp.route("/upload/<bucket>", methods=["POST"])
def upload_file(bucket):
    user_id = _identity()
    if user_id is None:
        return error("缺少或无效的认证Token", 401), 401
    # Legacy browser upload is only used for avatars. Dataset and model routes
    # already own their separate authenticated import flows.
    if bucket != "avatars":
        return error("此入口不接受该类文件", 403), 403
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return error("没有选择文件", 400), 400
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png"}:
        return error("头像只能使用 JPG 或 PNG", 400), 400
    content = upload.stream.read(_AVATAR_LIMIT + 1)
    if len(content) > _AVATAR_LIMIT:
        return error("头像超过大小限制", 400), 400
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
            image_format = image.format
    except (UnidentifiedImageError, OSError, ValueError):
        return error("图片内容无效", 400), 400
    if (suffix in {".jpg", ".jpeg"} and image_format != "JPEG") or (
        suffix == ".png" and image_format != "PNG"
    ):
        return error("图片格式与文件名不符", 400), 400

    temp_dir = Path(current_app.config["TEMP_DIR"])
    temp_dir.mkdir(parents=True, exist_ok=True)
    temporary = None
    saved = None
    try:
        with tempfile.NamedTemporaryFile(dir=temp_dir, suffix=suffix, delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
        saved = _storage().upload("avatars", str(temporary))
        _access().register_upload(
            user_id, "avatars", saved["objectKey"], "avatar",
            f"image/{'jpeg' if image_format == 'JPEG' else 'png'}", len(content),
        )
        return success(saved, "上传成功")
    except Exception:
        if saved is not None:
            _storage().delete(saved["bucket"], saved["objectKey"])
        return error("文件保存失败", 500), 500
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@file_bp.route("/<bucket>/<object_key>", methods=["GET"])
def get_file(bucket, object_key):
    user_id = _identity(allow_cookie=True)
    if user_id is None:
        return error("缺少或无效的认证Token", 401), 401
    try:
        path = _storage()._path(bucket, object_key)
    except ValueError:
        return error("文件路径无效", 400), 400
    try:
        _access().require_read(user_id, bucket, object_key)
    except PermissionError:
        return error("没有权限读取该文件", 403), 403
    if not path.is_file():
        return error("文件不存在", 404), 404
    return send_file(path)


@file_bp.route("/<bucket>/<object_key>", methods=["DELETE"])
def delete_file(bucket, object_key):
    user_id = _identity()
    if user_id is None:
        return error("缺少或无效的认证Token", 401), 401
    try:
        _storage()._path(bucket, object_key)
    except ValueError:
        return error("文件路径无效", 400), 400
    try:
        access = _access()
        access.require_delete(user_id, bucket, object_key)
    except PermissionError:
        return error("没有权限删除该文件", 403), 403
    access.mark_deleted(bucket, object_key)
    _storage().delete(bucket, object_key)
    return success(None, "删除成功")


def _validate_project_file(path: Path, suffix: str) -> tuple[str, str]:
    if suffix not in _PROJECT_TYPES:
        raise ValueError("不支持的文件格式")
    kind, mime_type = _PROJECT_TYPES[suffix]
    if kind == "image":
        try:
            with Image.open(path) as image:
                image.verify()
                actual_format = image.format
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ValueError("图片内容无效") from exc
        expected_format = {
            ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG",
            ".tif": "TIFF", ".tiff": "TIFF",
        }[suffix]
        if actual_format != expected_format:
            raise ValueError("图片格式与文件名不符")
    elif suffix == ".pdf":
        with path.open("rb") as source:
            if source.read(5) != b"%PDF-":
                raise ValueError("PDF 文件头无效")
    elif suffix in {".zip", ".docx"}:
        if not zipfile.is_zipfile(path):
            raise ValueError("压缩文件内容无效")
        if suffix == ".docx":
            with zipfile.ZipFile(path) as archive:
                members = set(archive.namelist())
                if not {"[Content_Types].xml", "word/document.xml"} <= members:
                    raise ValueError("DOCX 结构无效")
    elif suffix == ".pt":
        with path.open("rb") as source:
            header = source.read(4)
        if not (header.startswith(b"PK") or header.startswith(b"\x80")):
            raise ValueError("PT 权重文件头无效")
    elif suffix == ".md":
        try:
            path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise ValueError("手册附件必须是 UTF-8 文本") from exc
    return kind, mime_type


@file_bp.route("/project/<project_id>/upload", methods=["POST"])
def upload_project_file(project_id):
    user_id = _identity()
    if user_id is None:
        return error("缺少或无效的认证Token", 401), 401
    policy = current_app.config.get("PROJECT_FILE_ACCESS")
    try:
        allowed = policy is not None and policy(user_id, project_id, "file:create") is True
    except PermissionError:
        allowed = False
    if not allowed:
        return error("项目文件权限未接入或已拒绝", 403), 403
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return error("没有选择文件", 400), 400
    original_name = Path(upload.filename.replace("\\", "/")).name
    suffix = Path(original_name).suffix.lower()
    if suffix == ".doc":
        return error("旧 DOC 文件请先转换为 DOCX", 400), 400
    if suffix not in _PROJECT_TYPES:
        return error("不支持的文件格式", 400), 400
    temp_dir = Path(current_app.config["TEMP_DIR"])
    temp_dir.mkdir(parents=True, exist_ok=True)
    temporary = None
    saved = None
    registered = False
    digest = hashlib.sha256()
    size = 0
    try:
        with tempfile.NamedTemporaryFile(dir=temp_dir, suffix=suffix, delete=False) as output:
            temporary = Path(output.name)
            while chunk := upload.stream.read(1024 * 1024):
                size += len(chunk)
                if size > FILE_STORE_CONFIG["max_size"]:
                    raise ValueError("文件超过大小限制")
                digest.update(chunk)
                output.write(chunk)
        kind, mime_type = _validate_project_file(temporary, suffix)
        saved = _storage().upload("project_uploads", str(temporary))
        access = _access()
        file_id = access.register_upload(
            user_id, saved["bucket"], saved["objectKey"], kind, mime_type,
            size, project_id, original_name=original_name,
            sha256=digest.hexdigest(), status="uploaded",
        )
        registered = True
        return success({
            "file_id": file_id,
            "project_id": project_id,
            "version": 1,
            "status": "uploaded",
            "url": saved["url"],
            "kind": kind,
            "mime_type": mime_type,
            "size": size,
        }, "上传成功，尚未导入业务资源")
    except PermissionError:
        return error("项目文件权限已变更", 403), 403
    except ValueError as exc:
        return error(str(exc), 400), 400
    except Exception:
        return error("文件保存失败", 500), 500
    finally:
        if saved is not None and not registered:
            # Registration failures must not leave unowned objects behind.
            _storage().delete(saved["bucket"], saved["objectKey"])
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@file_bp.route("/metadata/<file_id>", methods=["GET"])
def get_file_metadata(file_id):
    user_id = _identity()
    if user_id is None:
        return error("缺少或无效的认证Token", 401), 401
    try:
        asset = _access().get_asset(user_id, file_id)
    except PermissionError:
        return error("没有权限读取该文件", 403), 403
    return success({
        "file_id": asset["id"],
        "project_id": asset["project_id"],
        "version": asset["version"],
        "status": asset["status"],
        "kind": asset["kind"],
        "mime_type": asset["mime_type"],
        "size": asset["size"],
        "sha256": asset["sha256"],
        "original_name": asset["original_name"],
    })

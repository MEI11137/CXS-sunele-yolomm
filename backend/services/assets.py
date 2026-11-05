"""业务资产：真实内容校验、原始字节保存、owner+project 鉴权。

三条硬规则（对应验收点）：

1. **类型来自内容**：图片/压缩包/权重的类型由字节头与真实解码结果决定，
   扩展名只作为展示信息，不参与判断。绝不会因为叫 ``.jpg`` 就当成图片。
2. **原始字节不被改写**：图片保存的是上传时的字节，推理时的格式转换只发生在
   ``artifacts/`` 里，原图永远可以按 SHA 复核。
3. **图片不是项目文件**：资产在 ``assets`` 表里单独登记，文件浏览页看到的是
   “项目文件”，资产列表看到的是“业务资产”，两者不互相冒充。

鉴权：所有读写都要求 ``owner_id`` + ``project_id`` 双条件，别人的资产按不存在处理。
"""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import sqlite3
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image

from backend import security
from backend.errors import ApiError
from backend.services import storage

MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_WEIGHT_BYTES = 512 * 1024 * 1024
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024

IMAGE_FORMATS = {"JPEG", "PNG", "BMP", "WEBP", "TIFF", "GIF"}
ROLE_FOR_KIND = {"image": "image", "archive": "archive", "weight": "weight", "document": "document"}


# --------------------------------------------------------------------------- 内容识别


def sniff(data: bytes, filename: str) -> dict:
    """按真实内容判断类型；判断不出来就明确报错，不靠扩展名猜。"""
    if not data:
        raise ApiError(400, "EMPTY_FILE", "文件是空的")
    if data[:4] == b"PK\x03\x04":
        # ZIP 既可能是数据集压缩包，也可能是 PyTorch 权重（它本身就是 zip）。
        if _looks_like_torch_zip(data):
            return {"kind": "weight", "mime": "application/octet-stream", "meta": {}}
        return {"kind": "archive", "mime": "application/zip", "meta": {}}
    image = _probe_image(data)
    if image is not None:
        return {"kind": "image", "mime": image["mime"], "meta": image}
    raise ApiError(
        415,
        "UNSUPPORTED_CONTENT",
        "只支持 JPEG/PNG/BMP/WEBP/TIFF 图片、ZIP 数据集和 .pt 权重；"
        f"这个文件（{filename or '未命名'}）的真实内容不在支持范围内",
    )


def _looks_like_torch_zip(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile:
        return False
    # torch.save 的归档里必有 ``<prefix>/data.pkl``；普通数据集压缩包不会有。
    return any(name.endswith("/data.pkl") or name == "data.pkl" for name in names)


def _probe_image(data: bytes) -> dict | None:
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = str(probe.format or "").upper()
            if fmt not in IMAGE_FORMATS:
                return None
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            mode = image.mode
    except Exception:
        return None
    mime = Image.MIME.get(fmt) or mimetypes.guess_type(f"x.{fmt.lower()}")[0] or "application/octet-stream"
    return {"format": fmt, "mime": mime, "width": int(width), "height": int(height), "mode": mode}


def _limit_for(kind: str) -> int:
    return {
        "image": MAX_IMAGE_BYTES,
        "archive": MAX_ARCHIVE_BYTES,
        "weight": MAX_WEIGHT_BYTES,
        "document": MAX_DOCUMENT_BYTES,
    }[kind]


# --------------------------------------------------------------------------- 读写


def public(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    try:
        meta = json.loads(data.get("meta") or "{}")
    except (ValueError, TypeError):
        meta = {}
    return {
        "id": data["id"],
        "project_id": data["project_id"],
        "kind": data["kind"],
        "role": data["role"],
        "origin": data["origin"],
        "filename": data["filename"],
        "mime": data["mime"],
        "size": int(data["size"]),
        "sha256": data["sha256"],
        "width": data.get("width"),
        "height": data.get("height"),
        "source_path": data.get("source_path"),
        "parent_id": data.get("parent_id"),
        "created_at": data["created_at"],
        "meta": meta,
        "url": f"/api/projects/{data['project_id']}/assets/{data['id']}/content",
    }


def get_owned(conn, user_id: int, project_id: str, asset_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM assets WHERE id = ? AND project_id = ? AND owner_id = ?",
        (str(asset_id), str(project_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise ApiError(404, "ASSET_NOT_FOUND", "该资产不存在或不属于当前项目")
    return row


def path_of(conn, user_id: int, project_id: str, asset_id: str, *, must_exist: bool = True) -> Path:
    row = get_owned(conn, user_id, project_id, asset_id)
    root = storage.project_root(conn, user_id, project_id)
    return storage.resolve_rel(root, row["rel_path"], must_exist=must_exist)


def read_bytes(conn, user_id: int, project_id: str, asset_id: str) -> tuple[sqlite3.Row, bytes]:
    row = get_owned(conn, user_id, project_id, asset_id)
    path = path_of(conn, user_id, project_id, asset_id)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise ApiError(409, "ASSET_CORRUPTED", "资产内容与登记时的 SHA 不一致，已拒绝使用")
    return row, data


def _insert(conn, **fields: Any) -> sqlite3.Row:
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO assets ({columns}) VALUES ({placeholders})", tuple(fields.values())
    )
    conn.commit()
    return get_owned(conn, fields["owner_id"], fields["project_id"], fields["id"])


def _store_dir(root: Path, kind: str, asset_id: str) -> Path:
    """每个资产一个目录：同名并发上传（RGB/IR 都叫 pair.png）不会互相覆盖。

    只靠 ``unique_path`` 的“查一次不存在”无法挡住并发：两个线程会同时看到
    “不存在”并拿到同一个路径，后写的那份把前一份覆盖掉。用资产 ID 分目录，
    路径在生成时就唯一，不依赖检查与写入之间的时间差。
    """
    return storage.project_dir(root, storage.UPLOADS, kind, asset_id)


def _store_bytes(root: Path, kind: str, asset_id: str, data: bytes, filename: str) -> Path:
    directory = _store_dir(root, kind, asset_id)
    return storage.write_bytes(root, storage.unique_path(directory, filename), data)


def add_bytes(
    conn,
    user_id: int,
    project_id: str,
    *,
    data: bytes,
    filename: str,
    role: str | None = None,
    session_id: str | None = None,
    origin: str = "upload",
    parent_id: str | None = None,
    source_path: str | None = None,
    meta: dict | None = None,
) -> dict:
    """保存一份资产。``data`` 是**原始字节**，落盘不做任何转码。"""
    root = storage.project_root(conn, user_id, project_id)
    detected = sniff(data, filename)
    kind = detected["kind"]
    if len(data) > _limit_for(kind):
        raise ApiError(413, "FILE_TOO_LARGE", f"文件超过上限（{_limit_for(kind) // (1024 * 1024)}MB）")
    safe = storage.safe_name(filename, fallback=f"{kind}")
    asset_id = security.new_id("ast")
    target = _store_bytes(root, kind, asset_id, data, safe)
    rel = storage.relative(root, target)
    payload_meta = dict(detected["meta"])
    payload_meta.update(meta or {})
    row = _insert(
        conn,
        id=asset_id,
        project_id=str(project_id),
        owner_id=int(user_id),
        session_id=session_id,
        kind=kind,
        role=role or ROLE_FOR_KIND[kind],
        origin=origin,
        filename=safe,
        rel_path=rel,
        mime=detected["mime"],
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        width=detected["meta"].get("width"),
        height=detected["meta"].get("height"),
        source_path=source_path,
        parent_id=parent_id,
        meta=json.dumps(payload_meta, ensure_ascii=False),
        created_at=security.now_iso(),
    )
    return public(row)


def register_local(
    conn, user_id: int, project_id: str, rel_path: str, *, role: str | None = None
) -> dict:
    """把项目根里**已有**的文件登记为资产（不搬动、不复制）。

    路径由服务端按项目根重新解析，前端/模型传来的绝对路径一律不接受；
    登记后仍会在每次读取时校验 SHA，文件被改动会明确报错而不是静默使用。
    """
    root = storage.project_root(conn, user_id, project_id)
    target = storage.resolve_rel(root, rel_path, must_exist=True)
    if target.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ApiError(413, "FILE_TOO_LARGE", "文件超过上限")
    data = target.read_bytes()
    detected = sniff(data, target.name)
    if len(data) > _limit_for(detected["kind"]):
        raise ApiError(413, "FILE_TOO_LARGE", "文件超过上限")
    rel = storage.relative(root, target)
    meta = dict(detected["meta"])
    meta["registered_from"] = rel
    row = _insert(
        conn,
        id=security.new_id("ast"),
        project_id=str(project_id),
        owner_id=int(user_id),
        session_id=None,
        kind=detected["kind"],
        role=role or ROLE_FOR_KIND[detected["kind"]],
        origin="local",
        filename=target.name,
        rel_path=rel,
        mime=detected["mime"],
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        width=detected["meta"].get("width"),
        height=detected["meta"].get("height"),
        source_path=rel,
        parent_id=None,
        meta=json.dumps(meta, ensure_ascii=False),
        created_at=security.now_iso(),
    )
    return public(row)


def derive_edge(conn, user_id: int, project_id: str, parent: sqlite3.Row) -> dict:
    """从 RGB 派生 edge 图（唯一允许的“生成模态”），来源与父资产都记下来。"""
    if str(parent["kind"]) != "image":
        raise ApiError(409, "NOT_AN_IMAGE", "只能从图片派生 edge")
    if str(parent["role"]) in {"ir", "edge"}:
        raise ApiError(409, "DERIVE_REFUSED", "只能从 RGB 原图派生 edge，不能从派生图再派生")
    import cv2
    import numpy as np

    root = storage.project_root(conn, user_id, project_id)
    data = path_of(conn, user_id, project_id, parent["id"]).read_bytes()
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ApiError(409, "IMAGE_UNREADABLE", "RGB 图片无法解码，不能派生 edge")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edge = cv2.cvtColor(cv2.Canny(gray, 80, 160), cv2.COLOR_GRAY2BGR)
    ok, encoded = cv2.imencode(".png", edge)
    if not ok:
        raise ApiError(500, "DERIVE_FAILED", "edge 图编码失败")
    dataset_dir = storage.project_dir(root, storage.CACHE, "edge")
    target = storage.write_bytes(
        root,
        storage.unique_path(dataset_dir, f"{Path(parent['filename']).stem}-edge.png"),
        encoded.tobytes(),
    )
    rel = storage.relative(root, target)
    row = _insert(
        conn,
        id=security.new_id("ast"),
        project_id=str(project_id),
        owner_id=int(user_id),
        session_id=parent["session_id"],
        kind="image",
        role="edge",
        origin="derived",
        filename=target.name,
        rel_path=rel,
        mime="image/png",
        size=int(encoded.nbytes),
        sha256=hashlib.sha256(encoded.tobytes()).hexdigest(),
        width=int(image.shape[1]),
        height=int(image.shape[0]),
        source_path=None,
        parent_id=str(parent["id"]),
        meta=json.dumps({"derived_from": parent["id"], "method": "canny(80,160)"}, ensure_ascii=False),
        created_at=security.now_iso(),
    )
    return public(row)


def list_for_project(
    conn,
    user_id: int,
    project_id: str,
    *,
    kind: str | None = None,
    role: str | None = None,
    limit: int = 50,
    before: str | None = None,
) -> dict:
    """分页列出资产。

    排序是 ``created_at DESC, id DESC``，游标就必须是**同一个复合键**：只按
    ``created_at`` 翻页时，创建时间相同的两条记录会在第二页丢一条（时间相同的
    资产是正常情况——同一批上传经常落在同一秒）。``before`` 传上一页最后一条的
    资产 ID，服务端用它取回当时的 ``(created_at, id)`` 再比较。
    """
    clauses = ["project_id = ?", "owner_id = ?"]
    params: list[Any] = [str(project_id), int(user_id)]
    if kind:
        clauses.append("kind = ?")
        params.append(str(kind))
    if role:
        clauses.append("role = ?")
        params.append(str(role))
    if before:
        cursor = conn.execute(
            "SELECT created_at, id FROM assets WHERE id = ? AND project_id = ? AND owner_id = ?",
            (str(before), str(project_id), int(user_id)),
        ).fetchone()
        if cursor is not None:
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([cursor["created_at"], cursor["created_at"], cursor["id"]])
    limit = max(1, min(int(limit or 50), 200))
    rows = conn.execute(
        f"SELECT * FROM assets WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    page = rows[:limit]
    return {
        "assets": [public(row) for row in page],
        "has_more": len(rows) > limit,
        "next_before": page[-1]["id"] if len(rows) > limit and page else None,
    }


def pair_summary(conn, user_id: int, project_id: str, rgb_asset_id: str) -> dict:
    """给界面/模型用的配对信息：这张 RGB 有没有 IR、是不是派生图。"""
    rgb = get_owned(conn, user_id, project_id, rgb_asset_id)
    meta = json.loads(rgb["meta"] or "{}")
    return {
        "rgb_asset_id": rgb["id"],
        "sha256": rgb["sha256"],
        "width": rgb["width"],
        "height": rgb["height"],
        "role": rgb["role"],
        "origin": rgb["origin"],
        "paired_x_asset_id": meta.get("paired_x_asset_id"),
        "derived": rgb["origin"] == "derived",
    }


__all__ = [
    "add_bytes",
    "derive_edge",
    "get_owned",
    "list_for_project",
    "pair_summary",
    "path_of",
    "public",
    "read_bytes",
    "register_local",
    "sniff",
]

"""大文件分块上传：会话状态在私有库里，块落在项目 ``.cache``，合并后交给 assets 服务。

为什么需要它：WorkBuddy 前置网关对**单个请求体**有硬限制（约 223MB 的 ZIP 连
``Expect: 100-continue`` 都在 0.33 秒内被 413），所以再大的单请求上限也没用；
把文件切成小块、每块单独请求，才是唯一能过网关的办法。

硬规则：

* **归属**：每步都按 ``(owner_id, project_id)`` 重新查会话与项目，不接受客户端给的路径；
* **根的稳定性**：``init`` 记下当时的项目根（realpath）。之后项目改绑、换目录、撤权，
  旧会话立即失效（``UPLOAD_ROOT_CHANGED``），绝不按新根去解析旧 ``rel_path``；
* **校验**：块序号、块大小、块 SHA256 三重校验；合并时**重新读盘**逐块核对
  size 与库里的 SHA（不信任登记过的回执），被改动或丢失一律拒绝；
* **幂等**：重复块只有真实落盘内容仍一致才算幂等成功（不一致就重写修复）；
  重复 finish 返回同一个 asset；并发 finish 只有一个能进入合并；
* **过期**：所有动作对过期语义一致；已完成记录的幂等取回不受过期影响；
* **临时文件**：只在 ``<项目根>/.cache/uploads/<upload_id>/``，受 ``storage.guard`` 约束，
  清理只删这一棵子树，绝不碰其它资产或源文件；清理失败留待下次重试。
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend import security
from backend.errors import ApiError
from backend.services import assets as asset_service
from backend.services import storage

CHUNK_SIZE = 4 * 1024 * 1024
CHUNK_THRESHOLD = 8 * 1024 * 1024
MIN_CHUNK_SIZE = 64
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_CHUNKS = 4096
MAX_ACTIVE_SESSIONS = 4
MAX_TEMP_BYTES = 1024 * 1024 * 1024
TTL_SECONDS = 12 * 3600
# 合并占位（assembling）超过这个时间没有更新，视为上次进程中断：允许重新认领。
ASSEMBLING_STALE_SECONDS = 600
# 已完成会话保留多久，用于「重复 finish 取回同一个资产」。
COMPLETED_RETENTION_SECONDS = 24 * 3600

STATUS_UPLOADING = "uploading"
STATUS_ASSEMBLING = "assembling"
STATUS_COMPLETED = "completed"
STATUS_ABORTED = "aborted"

_READ_BLOCK = 1024 * 1024
_HEX = set("0123456789abcdef")


def _now() -> int:
    return int(security.now_epoch())


def _sha_hex(value: object, field: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise ApiError(400, "BAD_HASH", f"{field} 必须是 64 位十六进制 SHA256")
    return text


def _root_key(root: Path) -> str:
    """项目根的规范化写法；会话里存的就是它，用于识别“换目录了”。"""
    return str(Path(os.path.realpath(str(root))))


def _stale_iso() -> str:
    moment = datetime.now(timezone.utc) - timedelta(seconds=ASSEMBLING_STALE_SECONDS)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- 会话与目录


def _session(conn, user_id: int, project_id: str, upload_id: str):
    row = conn.execute(
        "SELECT * FROM upload_sessions WHERE id = ? AND project_id = ? AND owner_id = ?",
        (str(upload_id), str(project_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise ApiError(404, "UPLOAD_NOT_FOUND", "上传会话不存在，或不属于当前项目")
    return row


def _assert_same_root(row, root: Path) -> None:
    """会话建立时看到的目录必须还是现在这个，否则一律失效（不猜、不迁移）。"""
    recorded = str(row["root_path"] or "")
    if recorded and recorded != _root_key(root):
        raise ApiError(
            409,
            "UPLOAD_ROOT_CHANGED",
            "项目绑定的目录已经变更，这次上传不能再继续；请重新选择文件上传",
        )


def _chunk_dir(root: Path, upload_id: str) -> Path:
    """临时块目录：受 guard 约束的 ``.cache/uploads/<id>``（会创建缺少的层级）。"""
    return storage.project_dir(root, storage.CACHE, "uploads", str(upload_id))


def _chunk_dir_if_exists(root: Path, upload_id: str) -> Path | None:
    # 先看存在性再 guard：对不存在的路径做 realpath 在 Windows 并发下并不稳定。
    raw = root / storage.CACHE / "uploads" / str(upload_id)
    if not raw.is_dir():
        return None
    return storage.guard(root, raw)


def _cleanup(root: Path, upload_id: str) -> bool:
    """只删这一次上传自己的临时目录；返回“目录是否已经不存在”（失败可重试）。"""
    target = _chunk_dir_if_exists(root, upload_id)
    if target is None:
        return True
    try:
        storage.remove_tree(root, target)
    except Exception:
        # 清理失败不掩盖真实结果：调用方据此保留会话记录，下次 sweep 再试。
        return False
    return _chunk_dir_if_exists(root, upload_id) is None


def _file_matches(path: Path, size: int, sha: str) -> bool:
    """流式核对磁盘上这一块是否仍是登记的字节（不整块进内存）。"""
    try:
        if not path.is_file() or path.stat().st_size != size:
            return False
        digest = hashlib.sha256()
        with open(path, "rb") as reader:
            for block in iter(lambda: reader.read(_READ_BLOCK), b""):
                digest.update(block)
        return digest.hexdigest() == sha
    except OSError:
        return False


def public_session(row) -> dict:
    return {
        "id": str(row["id"]),
        "status": str(row["status"]),
        "filename": str(row["filename"]),
        "size": int(row["size"]),
        "chunk_size": int(row["chunk_size"]),
        "chunk_count": int(row["chunk_count"]),
        "received_bytes": int(row["received_bytes"] or 0),
        "asset_id": row["asset_id"],
        "expires_at": int(row["expires_at"]),
        "created_at": str(row["created_at"]),
    }


# --------------------------------------------------------------------------- 过期与清理


def _expire_session(conn, user_id: int, project_id: str, root: Path, row) -> None:
    """对**单个**会话执行过期语义：未完成且过期 → 清理并删除记录，抛 410。

    已完成的会话不受过期影响（幂等取回仍可用）。
    根已经变了的会话不删任何文件——那不在当前授权目录里。
    """
    if str(row["status"]) == STATUS_COMPLETED:
        return
    if int(row["expires_at"]) >= _now():
        return
    if str(row["root_path"] or "") == _root_key(root):
        _cleanup(root, str(row["id"]))
    conn.execute("DELETE FROM upload_sessions WHERE id = ?", (str(row["id"]),))
    conn.commit()
    raise ApiError(410, "UPLOAD_EXPIRED", "上传已过期，请重新选择文件上传")


def _sweep(conn, user_id: int, project_id: str, root: Path) -> None:
    """惰性清理：过期会话、已取消的残留、已完成会话的临时目录、孤儿目录。

    全部限定在**当前项目根**这一棵树里：根已经变了的会话只丢记录，不按新根乱删。
    """
    now = _now()
    key = _root_key(root)
    rows = conn.execute(
        "SELECT id, status, root_path, expires_at FROM upload_sessions"
        " WHERE owner_id = ? AND project_id = ?",
        (int(user_id), str(project_id)),
    ).fetchall()
    keep: set[str] = set()
    for row in rows:
        upload_id = str(row["id"])
        status = str(row["status"])
        same_root = str(row["root_path"] or "") == key
        expired = int(row["expires_at"]) < now
        if status == STATUS_COMPLETED:
            if same_root:
                _cleanup(root, upload_id)
                if (
                    expired
                    and int(row["expires_at"]) + COMPLETED_RETENTION_SECONDS < now
                    and _chunk_dir_if_exists(root, upload_id) is None
                ):
                    conn.execute("DELETE FROM upload_sessions WHERE id = ?", (upload_id,))
                    continue
                keep.add(upload_id)
            else:
                conn.execute("DELETE FROM upload_sessions WHERE id = ?", (upload_id,))
            continue
        if not same_root:
            # 目录已不属于当前绑定：只丢记录，不碰那个目录。
            conn.execute("DELETE FROM upload_sessions WHERE id = ?", (upload_id,))
            continue
        if status == STATUS_ABORTED or expired:
            if _cleanup(root, upload_id):
                conn.execute("DELETE FROM upload_sessions WHERE id = ?", (upload_id,))
            else:
                keep.add(upload_id)
            continue
        keep.add(upload_id)
    conn.commit()
    _sweep_orphans(conn, root, keep)


def _sweep_orphans(conn, root: Path, keep: set[str]) -> None:
    """DB 里已经没有记录的临时块目录（崩溃/清理失败留下）——只扫一层、只删自己的树。

    同一个项目根目录可以绑定多个项目，块目录按 upload_id 全局命名。因此不能只看
    “当前项目的会话”，否则另一个项目的 init/status 会把本项目仍在写块的目录当孤儿删掉。
    删除前逐个复查 upload_sessions：只要还有任何会话引用这个 id 就保留。
    会话行先插入、目录后创建，故“目录存在”必然意味着会话行已提交，这里复查是可靠的。
    """
    raw = root / storage.CACHE / "uploads"
    if not raw.is_dir():
        return
    base = storage.guard(root, raw)
    try:
        entries = list(base.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name in keep or not entry.is_dir():
            continue
        referenced = conn.execute(
            "SELECT 1 FROM upload_sessions WHERE id = ? LIMIT 1", (entry.name,)
        ).fetchone()
        if referenced is not None:
            continue
        try:
            storage.remove_tree(root, entry)
        except ApiError:
            continue


# --------------------------------------------------------------------------- 初始化


def init(
    conn,
    user_id: int,
    project_id: str,
    *,
    filename: str,
    size: int,
    sha256: str | None = None,
    chunk_size: int | None = None,
    session_id: str | None = None,
    role: str | None = None,
) -> dict:
    root = storage.project_root(conn, user_id, project_id)
    _sweep(conn, user_id, project_id, root)

    safe = storage.safe_name(filename, fallback="upload")
    try:
        total = int(size)
    except (TypeError, ValueError):
        raise ApiError(400, "BAD_SIZE", "文件大小必须是整数") from None
    if total <= 0:
        raise ApiError(400, "BAD_SIZE", "文件大小必须大于 0")
    if total > MAX_UPLOAD_BYTES:
        raise ApiError(413, "FILE_TOO_LARGE", "单个文件超过 512MB 上限")

    try:
        unit = int(chunk_size or CHUNK_SIZE)
    except (TypeError, ValueError):
        raise ApiError(400, "BAD_CHUNK_SIZE", "分块大小必须是整数") from None
    if unit < MIN_CHUNK_SIZE or unit > CHUNK_SIZE:
        raise ApiError(400, "BAD_CHUNK_SIZE", f"分块大小必须在 {MIN_CHUNK_SIZE} 到 {CHUNK_SIZE} 之间")
    count = (total + unit - 1) // unit
    if count > MAX_CHUNKS:
        raise ApiError(400, "TOO_MANY_CHUNKS", "分块数过多，请增大分块大小")

    if session_id:
        owned = conn.execute(
            "SELECT id FROM sessions WHERE id = ? AND project_id = ? AND owner_id = ?",
            (str(session_id), str(project_id), int(user_id)),
        ).fetchone()
        if owned is None:
            raise ApiError(404, "SESSION_NOT_FOUND", "会话不存在或不属于当前项目")

    upload_id = security.new_id("upl")
    now_iso = security.now_iso()
    declared = _sha_hex(sha256, "整文件 SHA256") if sha256 else None
    # 角色只接受短 token（ir/rgb/edge/…），不做自由文本落库。
    role_text = str(role or "").strip().lower() or None
    if role_text and (len(role_text) > 32 or not role_text.replace("_", "").isalnum()):
        raise ApiError(400, "BAD_ROLE", "资产角色不合法")

    # 限额与插入必须在同一个即时事务里：否则两个并发 init 都能读到“还没满”。
    # 部署是单进程 Waitress，这条 BEGIN IMMEDIATE 就足够；不引入分布式锁。
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM upload_sessions WHERE owner_id = ? AND project_id = ?"
            " AND status != ?",
            (int(user_id), str(project_id), STATUS_COMPLETED),
        ).fetchone()["c"]
        if int(active) >= MAX_ACTIVE_SESSIONS:
            raise ApiError(
                429,
                "UPLOAD_LIMIT",
                f"同时进行的上传过多（上限 {MAX_ACTIVE_SESSIONS} 个），请先完成或取消其它上传",
            )
        pending = conn.execute(
            "SELECT COALESCE(SUM(size), 0) AS s FROM upload_sessions"
            " WHERE owner_id = ? AND project_id = ? AND status != ?",
            (int(user_id), str(project_id), STATUS_COMPLETED),
        ).fetchone()["s"]
        if int(pending) + total > MAX_TEMP_BYTES:
            raise ApiError(429, "SPACE_LIMIT", "未完成上传占用的临时空间过多，请先完成或取消其它上传")
        conn.execute(
            "INSERT INTO upload_sessions (id, project_id, owner_id, filename, size, chunk_size,"
            " chunk_count, received_bytes, declared_sha256, status, asset_id, session_id, role,"
            " root_path, pending_asset_id, created_at, updated_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
            (
                upload_id,
                str(project_id),
                int(user_id),
                safe,
                total,
                unit,
                count,
                declared,
                STATUS_UPLOADING,
                session_id,
                role_text,
                _root_key(root),
                security.new_id("ast"),
                now_iso,
                now_iso,
                _now() + TTL_SECONDS,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    # 目录在会话落库之后再建：INSERT 失败时不会留下无主的临时目录。
    _chunk_dir(root, upload_id)
    row = _session(conn, user_id, project_id, upload_id)
    return public_session(row)


# --------------------------------------------------------------------------- 写入块


def put_chunk(
    conn,
    user_id: int,
    project_id: str,
    upload_id: str,
    index: int,
    data: bytes,
    *,
    sha256: str | None,
) -> dict:
    root = storage.project_root(conn, user_id, project_id)
    row = _session(conn, user_id, project_id, upload_id)
    _assert_same_root(row, root)
    _expire_session(conn, user_id, project_id, root, row)
    status = str(row["status"])
    if status == STATUS_COMPLETED:
        raise ApiError(409, "UPLOAD_COMPLETED", "这次上传已经完成，不能继续写块")
    if status == STATUS_ABORTED:
        raise ApiError(409, "UPLOAD_ABORTED", "这次上传已取消，请重新选择文件上传")
    if status == STATUS_ASSEMBLING:
        raise ApiError(409, "UPLOAD_BUSY", "正在合并文件，请稍后重试")

    count = int(row["chunk_count"])
    unit = int(row["chunk_size"])
    total = int(row["size"])
    if index < 0 or index >= count:
        raise ApiError(400, "BAD_CHUNK_INDEX", f"块序号超出范围（0..{count - 1}）")
    expected = total - unit * (count - 1) if index == count - 1 else unit
    if len(data) != expected:
        raise ApiError(
            400,
            "CHUNK_SIZE_MISMATCH",
            f"第 {index} 块大小应为 {expected} 字节，实际 {len(data)} 字节",
        )
    declared = _sha_hex(sha256, "块 SHA256")
    actual = hashlib.sha256(data).hexdigest()
    if actual != declared:
        raise ApiError(400, "CHUNK_HASH_MISMATCH", f"第 {index} 块内容与声明的 SHA256 不一致")

    directory = _chunk_dir(root, upload_id)
    # 目录已经由 project_dir 校验过真实路径；块文件名是纯序号，直接拼，
    # 不再对**尚不存在**的文件做 realpath（并发下那一步不稳定）。
    target = directory / f"{index:05d}.part"
    rel = f"{storage.CACHE}/uploads/{upload_id}/{index:05d}.part"

    # 先原子认领这一块（同序号并发只有一个能抢到 INSERT），再写盘。
    # 这样不会出现“两个请求都覆盖磁盘、只有一个能 INSERT”导致的库盘不一致。
    claimed = conn.execute(
        "INSERT INTO upload_chunks (upload_id, index_no, size, sha256, rel_path, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(upload_id, index_no) DO NOTHING",
        (str(upload_id), int(index), len(data), actual, rel, security.now_iso()),
    )
    conn.commit()
    if claimed.rowcount == 1:
        try:
            storage.write_bytes(root, target, data)
        except Exception:
            # 写盘失败就把占位回滚掉，别留下“登记了但没字节”的块。
            conn.execute(
                "DELETE FROM upload_chunks WHERE upload_id = ? AND index_no = ?",
                (str(upload_id), int(index)),
            )
            conn.commit()
            raise
        _refresh_received(conn, upload_id)
        return _status_payload(conn, root, _session(conn, user_id, project_id, upload_id))

    existing = conn.execute(
        "SELECT size, sha256, rel_path FROM upload_chunks WHERE upload_id = ? AND index_no = ?",
        (str(upload_id), int(index)),
    ).fetchone()
    if existing is None:  # pragma: no cover - 并发删除的极窄窗口
        raise ApiError(409, "UPLOAD_BUSY", "这次上传状态刚刚变化，请重试")
    if int(existing["size"]) != len(data) or str(existing["sha256"]) != actual:
        raise ApiError(409, "CHUNK_CONFLICT", f"第 {index} 块已经用不同内容上传过，请先取消本次上传")

    # 同内容重传：只有真实落盘内容仍然一致才算幂等成功；否则用这次字节修复它。
    if not _file_matches(target, len(data), actual):
        storage.write_bytes(root, target, data)
    _refresh_received(conn, upload_id)
    return _status_payload(conn, root, _session(conn, user_id, project_id, upload_id))


def _refresh_received(conn, upload_id: str) -> None:
    """把会话上的已收字节数对齐到真实块表（并发下也能算对，不做自增）。"""
    received = conn.execute(
        "SELECT COALESCE(SUM(size), 0) AS s FROM upload_chunks WHERE upload_id = ?",
        (str(upload_id),),
    ).fetchone()["s"]
    conn.execute(
        "UPDATE upload_sessions SET received_bytes = ?, updated_at = ? WHERE id = ?",
        (int(received), security.now_iso(), str(upload_id)),
    )
    conn.commit()


def _status_payload(conn, root: Path, row) -> dict:
    received = conn.execute(
        "SELECT COALESCE(SUM(size), 0) AS s FROM upload_chunks WHERE upload_id = ?",
        (str(row["id"]),),
    ).fetchone()["s"]
    return {**public_session(row), "received_bytes": int(received)}


# --------------------------------------------------------------------------- 状态 / 取消


def status(conn, user_id: int, project_id: str, upload_id: str) -> dict:
    root = storage.project_root(conn, user_id, project_id)
    # 先按“这一次上传”给出准确答复（404/409 根变更/410 过期），再做全局惰性清理。
    # 反过来会先把过期/失效的会话删掉，用户只能看到 404，读不出真实原因。
    row = _session(conn, user_id, project_id, upload_id)
    _assert_same_root(row, root)
    _expire_session(conn, user_id, project_id, root, row)
    _sweep(conn, user_id, project_id, root)
    fresh = _session(conn, user_id, project_id, upload_id)
    return _status_payload(conn, root, fresh)


def abort(conn, user_id: int, project_id: str, upload_id: str) -> dict:
    root = storage.project_root(conn, user_id, project_id)
    row = _session(conn, user_id, project_id, upload_id)
    _assert_same_root(row, root)
    _expire_session(conn, user_id, project_id, root, row)

    # 原子抢占：只有 uploading 才能被取消。assembling 说明有人正在合并，
    # 取消与合并不能交错成“取消成功但资产后来产生”，这里明确让调用方重试。
    claimed = conn.execute(
        "UPDATE upload_sessions SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
        (STATUS_ABORTED, security.now_iso(), str(upload_id), STATUS_UPLOADING),
    )
    if claimed.rowcount != 1:
        conn.commit()
        fresh = _session(conn, user_id, project_id, upload_id)
        state = str(fresh["status"])
        if state == STATUS_COMPLETED:
            raise ApiError(409, "UPLOAD_COMPLETED", "这次上传已经完成，不能取消")
        if state == STATUS_ASSEMBLING:
            raise ApiError(409, "UPLOAD_BUSY", "正在合并文件，请等它结束再决定是否取消")
        if state == STATUS_ABORTED:
            return {"cancelled": True, "upload_id": str(upload_id), "tmp_cleaned": True}
        raise ApiError(409, "UPLOAD_STATE", f"当前状态（{state}）不能取消")
    conn.commit()

    cleaned = _cleanup(root, upload_id)
    if cleaned:
        conn.execute("DELETE FROM upload_sessions WHERE id = ?", (str(upload_id),))
    conn.commit()
    return {"cancelled": True, "upload_id": str(upload_id), "tmp_cleaned": bool(cleaned)}


# --------------------------------------------------------------------------- 合并


def _recover_completed_asset(conn, user_id: int, project_id: str, row):
    """上次已经把资产登记好、只是没写完成状态：返回那个资产，不重复创建。"""
    pending = str(row["pending_asset_id"] or "")
    if not pending:
        return None
    try:
        return asset_service.get_owned(conn, user_id, project_id, pending)
    except ApiError:
        return None


def finish(conn, user_id: int, project_id: str, upload_id: str) -> dict:
    root = storage.project_root(conn, user_id, project_id)
    row = _session(conn, user_id, project_id, upload_id)
    _assert_same_root(row, root)
    _expire_session(conn, user_id, project_id, root, row)
    row = _session(conn, user_id, project_id, upload_id)
    status = str(row["status"])

    if status == STATUS_COMPLETED:
        asset_id = row["asset_id"]
        if not asset_id:
            raise ApiError(410, "ASSET_GONE", "上传记录已完成但资产已不存在，请重新上传")
        try:
            asset = asset_service.public(
                asset_service.get_owned(conn, user_id, project_id, str(asset_id))
            )
        except ApiError:
            raise ApiError(410, "ASSET_GONE", "上传记录已完成但资产已不存在，请重新上传") from None
        return {"asset": asset, "upload": public_session(row)}
    if status == STATUS_ABORTED:
        raise ApiError(409, "UPLOAD_ABORTED", "这次上传已取消，请重新选择文件上传")

    # 崩溃恢复：资产已经用预分配的 id 保存过，只是完成状态没写下来。
    recovered = _recover_completed_asset(conn, user_id, project_id, row)
    if recovered is not None:
        conn.execute(
            "UPDATE upload_sessions SET status = ?, asset_id = ?, updated_at = ? WHERE id = ?",
            (STATUS_COMPLETED, str(recovered["id"]), security.now_iso(), str(upload_id)),
        )
        conn.commit()
        _cleanup(root, upload_id)
        return {
            "asset": asset_service.public(recovered),
            "upload": public_session(_session(conn, user_id, project_id, upload_id)),
        }

    count = int(row["chunk_count"])
    rows = conn.execute(
        "SELECT index_no, size, sha256, rel_path FROM upload_chunks"
        " WHERE upload_id = ? ORDER BY index_no",
        (str(upload_id),),
    ).fetchall()
    present = {int(item["index_no"]) for item in rows}
    missing = [index for index in range(count) if index not in present]
    if missing:
        raise ApiError(
            409,
            "CHUNKS_MISSING",
            f"还有 {len(missing)} 块没有上传完成，不能合并",
            details={"missing": missing[:20], "missing_count": len(missing)},
        )

    # 原子认领：正常情况只有 uploading 能进合并；assembling 只有在“占位很久没动”
    # （上次进程中断）时才允许重新认领，否则返回 UPLOAD_BUSY。
    claimed = conn.execute(
        "UPDATE upload_sessions SET status = ?, updated_at = ? WHERE id = ?"
        " AND (status = ? OR (status = ? AND updated_at <= ?))",
        (
            STATUS_ASSEMBLING,
            security.now_iso(),
            str(upload_id),
            STATUS_UPLOADING,
            STATUS_ASSEMBLING,
            _stale_iso(),
        ),
    )
    conn.commit()
    if claimed.rowcount != 1:
        raise ApiError(409, "UPLOAD_BUSY", "这次上传正在被处理，请稍后重试")

    directory = _chunk_dir(root, upload_id)
    assembled = directory / "assembled.bin"
    digest = hashlib.sha256()
    written = 0
    corrupted: list[int] = []
    try:
        with open(assembled, "wb") as writer:
            for item in rows:
                index = int(item["index_no"])
                expected_size = int(item["size"])
                expected_sha = str(item["sha256"])
                try:
                    part = storage.resolve_rel(root, str(item["rel_path"]), must_exist=True)
                except ApiError:
                    corrupted.append(index)
                    continue
                block_digest = hashlib.sha256()
                block_size = 0
                with open(part, "rb") as reader:
                    while True:
                        block = reader.read(_READ_BLOCK)
                        if not block:
                            break
                        block_digest.update(block)
                        digest.update(block)
                        writer.write(block)
                        block_size += len(block)
                        written += len(block)
                if block_size != expected_size or block_digest.hexdigest() != expected_sha:
                    corrupted.append(index)
            writer.flush()
            os.fsync(writer.fileno())

        if corrupted:
            raise ApiError(
                409,
                "CHUNK_CORRUPTED",
                f"有 {len(corrupted)} 块与服务端记录不一致（可能被改动或丢失），请重新上传这些块",
                details={"chunks": corrupted[:20], "chunk_count": len(corrupted)},
            )
        if written != int(row["size"]):
            raise ApiError(
                409,
                "UPLOAD_SIZE_MISMATCH",
                f"合并后大小是 {written} 字节，与声明的 {int(row['size'])} 字节不一致",
            )
        actual = digest.hexdigest()
        declared = row["declared_sha256"]
        if declared and str(declared) != actual:
            raise ApiError(
                409,
                "UPLOAD_SHA_MISMATCH",
                "合并后的文件与客户端声明的 SHA256 不一致，已停止登记；请重新上传",
                details={"expected": str(declared), "actual": actual},
            )

        asset = asset_service.add_file(
            conn,
            user_id,
            project_id,
            source=assembled,
            filename=str(row["filename"]),
            session_id=row["session_id"],
            role=row["role"],
            origin="upload",
            asset_id=str(row["pending_asset_id"] or "") or None,
        )
    except Exception:
        # 合并失败：退回 uploading，保留块，允许重试；只删自己那份中间文件。
        if assembled.exists():
            try:
                assembled.unlink()
            except OSError:
                pass
        conn.execute(
            "UPDATE upload_sessions SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (STATUS_UPLOADING, security.now_iso(), str(upload_id), STATUS_ASSEMBLING),
        )
        conn.commit()
        raise

    conn.execute(
        "UPDATE upload_sessions SET status = ?, asset_id = ?, updated_at = ? WHERE id = ?",
        (STATUS_COMPLETED, str(asset["id"]), security.now_iso(), str(upload_id)),
    )
    conn.commit()
    if not _cleanup(root, upload_id):
        # 清理失败：记录保持 completed，下次 sweep 继续尝试删临时目录。
        pass
    fresh = _session(conn, user_id, project_id, upload_id)
    return {"asset": asset, "upload": public_session(fresh)}


# 兼容测试/调用方：显式暴露给路由层用
__all__ = [
    "CHUNK_SIZE",
    "CHUNK_THRESHOLD",
    "MAX_UPLOAD_BYTES",
    "abort",
    "finish",
    "init",
    "put_chunk",
    "status",
]

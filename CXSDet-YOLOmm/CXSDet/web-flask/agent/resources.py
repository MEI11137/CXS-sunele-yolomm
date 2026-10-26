"""业务资源的真实归属登记（INT-03）。

B 的权限判断只认 ``agent_resource`` 索引；C 的业务表（``datasets`` / ``models``
/ ``detection_records`` …）没有 "项目 + 所有者" 字段。本模块负责把两边对齐：

* **归属只能从业务行或文件链路推导**，绝不采信调用方声明的 ``owner_id``；
  推不出来就不登记（宁可拒绝，也不把别人的资源登记成自己的）。
* **登记是幂等的**：``(source_table, source_id)`` 已有条目直接返回，不重复插入。
* **业务成功、登记失败要留痕**：写入 ``agent_registration_failure``，可查询、
  可重试，但绝不因为登记失败去重造业务对象。
* **状态变化递增版本**：样本/标注/发布状态改变后 ``revision + 1``，使旧的
  确认与预览失效。
* **历史资源**：能判定归属的按原归属登记（默认私有）；判定不出的不公开。

只做登记与核对，不创建业务对象，也不替代业务事务。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, List, Optional

from agent import permissions
from agent.errors import AgentError, Code
from agent.store import connect, dumps, new_id, now, query_all, query_one

# kind（B/前端的叫法）→ 业务表（C 的真实来源）
SOURCE_TABLE_BY_KIND: Dict[str, str] = {
    "dataset": "datasets",
    "model": "models",
    "record": "detection_records",
    "detection_record": "detection_records",
    "draft": "dataset_drafts",
    "dataset_draft": "dataset_drafts",
    "document": "documents",
    "file": "file_assets",
    "directory": "authorized_directories",
    "mcp_connection": "mcp_connections",
}

# 业务表 → 默认 kind
KIND_BY_SOURCE_TABLE: Dict[str, str] = {
    "datasets": "dataset",
    "models": "model",
    "detection_records": "detection_record",
    "dataset_drafts": "draft",
    "documents": "document",
    "file_assets": "file",
    "authorized_directories": "directory",
    "mcp_connections": "mcp_connection",
}

# 每张业务表里表示"操作者"的列，按优先级尝试。只读这些已有列，不新增字段。
_OWNER_COLUMNS = {
    "datasets": ("user_id", "owner_id", "creator_id"),
    "models": ("user_id", "owner_id", "creator_id"),
    "detection_records": ("user_id",),
    "dataset_drafts": ("creator_id", "user_id", "owner_id"),
    "documents": ("user_id", "owner_id", "creator_id"),
    "file_assets": ("owner_id",),
    "authorized_directories": ("user_id", "owner_id", "creator_id"),
    "mcp_connections": ("user_id", "owner_id", "creator_id"),
}

_NAME_COLUMNS = ("name", "title", "display_name", "original_name")
_PROJECT_COLUMNS = ("project_id",)


# --------------------------------------------------------------------------- #
# 基础读取
# --------------------------------------------------------------------------- #
def _columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {str(row[1]) for row in conn.execute('PRAGMA table_info("%s")' % table)}
    except sqlite3.Error:
        return set()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        query_one(
            conn,
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
    )


def business_row(source_table: str, business_id: str) -> Optional[Dict[str, Any]]:
    """按业务表和业务 ID 取一行；表不存在或行不存在返回 None。"""
    if not source_table or not business_id:
        return None
    conn = connect()
    try:
        if not _table_exists(conn, source_table):
            return None
        return query_one(
            conn, 'SELECT * FROM "%s" WHERE id = ?' % source_table, (business_id,)
        )
    finally:
        conn.close()


def _display_name(row: Dict[str, Any], business_id: str) -> str:
    for column in _NAME_COLUMNS:
        value = row.get(column)
        if value:
            return str(value)
    return f"{business_id[:12]}"


def _owner_from_row(row: Dict[str, Any], table: str) -> Optional[int]:
    for column in _OWNER_COLUMNS.get(table, ()):
        value = row.get(column)
        if value is None:
            continue
        try:
            owner = int(value)
        except (TypeError, ValueError):
            continue
        if owner > 0:
            return owner
    return None


def _from_file_link(
    conn: sqlite3.Connection, source_table: str, business_id: str, row: Dict[str, Any]
) -> Dict[str, Any]:
    """从文件链路取归属：上传件在登记业务资源时记了 ``business_type/business_id``。

    这是数据集/模型唯一可靠的"操作者"来源——它们的业务表里没有 user_id。
    检测记录没有这条绑定，退回用它原图的 bucket/object_key 反查上传件。
    """
    from agent.store import row_to_dict

    empty: Dict[str, Any] = {"owner_id": None, "project_id": None}
    if not _table_exists(conn, "file_assets"):
        return empty
    columns = _columns(conn, "file_assets")
    if not {"project_id", "business_type", "business_id"} <= columns:
        return empty

    linked = row_to_dict(
        conn.execute(
            "SELECT project_id, owner_id FROM file_assets "
            "WHERE business_id = ? AND business_type IN (?, ?) LIMIT 1",
            (
                business_id,
                source_table,
                KIND_BY_SOURCE_TABLE.get(source_table, source_table),
            ),
        ).fetchone()
    )
    if linked:
        return {
            "owner_id": int(linked["owner_id"]) if linked.get("owner_id") else None,
            "project_id": str(linked["project_id"]) if linked.get("project_id") else None,
        }

    if source_table == "detection_records" and {"bucket", "object_key"} <= columns:
        for bucket_col, key_col in (
            ("original_image_bucket", "original_image_object_key"),
            ("original_video_bucket", "original_video_object_key"),
        ):
            if not {bucket_col, key_col} <= set(row or {}):
                continue
            found = row_to_dict(
                conn.execute(
                    "SELECT project_id, owner_id FROM file_assets "
                    "WHERE bucket = ? AND object_key = ? LIMIT 1",
                    (row.get(bucket_col), row.get(key_col)),
                ).fetchone()
            )
            if found:
                return {
                    "owner_id": int(found["owner_id"]) if found.get("owner_id") else None,
                    "project_id": str(found["project_id"]) if found.get("project_id") else None,
                }
    return empty


def derive_owner(
    source_table: str, business_id: str
) -> Optional[Dict[str, Any]]:
    """推导真实归属。推不出就返回 None —— 不猜、不默认给调用方。"""
    conn = connect()
    try:
        if not _table_exists(conn, source_table):
            return None
        row = query_one(
            conn, 'SELECT * FROM "%s" WHERE id = ?' % source_table, (business_id,)
        )
        if not row:
            return None
        owner_id = _owner_from_row(row, source_table)
        project_id = None
        for column in _PROJECT_COLUMNS:
            if row.get(column):
                project_id = str(row[column])
                break
        if owner_id is None or project_id is None:
            link = _from_file_link(conn, source_table, business_id, row)
            if owner_id is None:
                owner_id = link["owner_id"]
            if project_id is None:
                project_id = link["project_id"]
        return {
            "owner_id": owner_id,
            "project_id": project_id,
            "display_name": _display_name(row, business_id),
            "row": row,
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 默认项目
# --------------------------------------------------------------------------- #
def default_project_id(conn: sqlite3.Connection, owner_id: int) -> str:
    """取（或建）本人默认项目，用于承载未归档资源。"""
    row = query_one(
        conn,
        "SELECT id FROM agent_project WHERE owner_id = ? AND status = 'active' "
        "ORDER BY (name = '默认项目') DESC, created_at ASC LIMIT 1",
        (owner_id,),
    )
    if row:
        return str(row["id"])
    project_id = new_id("prj")
    stamp = now()
    conn.execute(
        "INSERT INTO agent_project "
        "(id, owner_id, name, description, requirements_json, origin, status, "
        " created_at, updated_at) VALUES (?, ?, '默认项目', ?, '{}', ?, 'active', ?, ?)",
        (
            project_id,
            owner_id,
            "系统自动创建，用于承载未归档的会话与资源",
            permissions.ORIGIN_USER,
            stamp,
            stamp,
        ),
    )
    return project_id


# --------------------------------------------------------------------------- #
# 登记
# --------------------------------------------------------------------------- #
def ensure(
    source_table: str,
    source_id: str,
    *,
    kind: Optional[str] = None,
    owner_id: Optional[int] = None,
    project_id: Optional[str] = None,
    display_name: Optional[str] = None,
    access_policy: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    create_missing: bool = True,
) -> Optional[Dict[str, Any]]:
    """幂等登记一条业务资源；返回索引视图（含 ``created`` 标记）。

    ``owner_id`` 只能由服务端内部（业务行推导或本次操作的真实 principal）传入。
    """
    if not source_table or not source_id:
        raise AgentError(Code.VALIDATION, "登记资源需要来源表与业务 ID")
    target_kind = kind or KIND_BY_SOURCE_TABLE.get(source_table, source_table)

    conn = connect()
    try:
        existing = query_one(
            conn,
            "SELECT * FROM agent_resource WHERE source_table = ? AND source_id = ?",
            (source_table, source_id),
        )
        if existing:
            view = permissions._resource_view(existing)
            view["created"] = False
            return view

        if not create_missing:
            return None

        derived = derive_owner(source_table, source_id)
        row_exists = derived is not None or business_row(source_table, source_id) is not None
        if not row_exists:
            # 业务对象不存在就不登记，避免凭 ID 造出一条"资源"
            return None

        resolved_owner = owner_id
        resolved_name = display_name
        if derived is not None:
            resolved_name = resolved_name or derived["display_name"]
            project_id = project_id or derived["project_id"]
            if derived["owner_id"]:
                # 业务行写明的归属优先于任何调用方传入的 owner_id
                resolved_owner = derived["owner_id"]
        if resolved_owner is None:
            return None
        resolved_owner = int(resolved_owner)
        if resolved_owner <= 0:
            return None

        target_project = project_id or default_project_id(conn, resolved_owner)
        project_row = query_one(
            conn, "SELECT owner_id, origin FROM agent_project WHERE id = ?", (target_project,)
        )
        if project_row is None:
            target_project = default_project_id(conn, resolved_owner)

        stamp = now()
        resource_id = new_id("res")
        try:
            conn.execute(
                "INSERT INTO agent_resource "
                "(id, kind, project_id, owner_id, display_name, revision, access_policy, "
                " modality, source_table, source_id, metadata_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
                (
                    resource_id,
                    target_kind,
                    target_project,
                    resolved_owner,
                    resolved_name or source_id[:12],
                    access_policy or permissions.POLICY_PRIVATE,
                    (metadata or {}).get("modality"),
                    source_table,
                    source_id,
                    dumps(metadata or {}),
                    stamp,
                    stamp,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # 并发登记：另一个请求已经插入了同一条来源
            conn.rollback()
            row = query_one(
                conn,
                "SELECT * FROM agent_resource WHERE source_table = ? AND source_id = ?",
                (source_table, source_id),
            )
            if not row:
                raise
            view = permissions._resource_view(row)
            view["created"] = False
            return view

        row = query_one(conn, "SELECT * FROM agent_resource WHERE id = ?", (resource_id,))
        view = permissions._resource_view(row)
        view["created"] = True
        return view
    finally:
        conn.close()


def ensure_kind(
    kind: str,
    business_id: str,
    *,
    owner_id: Optional[int] = None,
    project_id: Optional[str] = None,
    access_policy: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    table = SOURCE_TABLE_BY_KIND.get(kind)
    if not table:
        return None
    return ensure(
        table,
        business_id,
        kind=kind,
        owner_id=owner_id,
        project_id=project_id,
        access_policy=access_policy,
    )


def bump(source_table: str, source_id: str) -> int:
    """内容/状态变化时递增版本；未登记返回 0。"""
    conn = connect()
    try:
        row = query_one(
            conn,
            "SELECT id FROM agent_resource WHERE source_table = ? AND source_id = ?",
            (source_table, source_id),
        )
        if not row:
            return 0
        revision = permissions.bump_revision(conn, row["id"])
        conn.commit()
        return int(revision)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 登记失败留痕
# --------------------------------------------------------------------------- #
def record_failure(
    *,
    source_table: str,
    source_id: str,
    kind: Optional[str] = None,
    principal: Optional[int] = None,
    project_id: Optional[str] = None,
    error: str = "",
) -> None:
    """业务已成功、登记失败：留痕等重试，不重造业务。"""
    conn = connect()
    try:
        row = query_one(
            conn,
            "SELECT id, attempts FROM agent_registration_failure "
            "WHERE source_table = ? AND source_id = ? AND resolved_at IS NULL",
            (source_table, source_id),
        )
        stamp = now()
        if row:
            conn.execute(
                "UPDATE agent_registration_failure SET attempts = attempts + 1, "
                "error = ?, updated_at = ? WHERE id = ?",
                (error[:500], stamp, row["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO agent_registration_failure "
                "(id, source_table, source_id, kind, principal, project_id, error, "
                " attempts, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    new_id("regf"),
                    source_table,
                    source_id,
                    kind,
                    principal,
                    project_id,
                    error[:500],
                    stamp,
                    stamp,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def list_pending_failures(limit: int = 50) -> List[Dict[str, Any]]:
    conn = connect()
    try:
        return query_all(
            conn,
            "SELECT * FROM agent_registration_failure WHERE resolved_at IS NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (max(1, int(limit)),),
        )
    finally:
        conn.close()


def retry_pending(limit: int = 50) -> Dict[str, int]:
    """重试待登记的来源；仍失败则保留记录并累加次数。"""
    stats = {"registered": 0, "skipped": 0, "still_failing": 0}
    for failure in list_pending_failures(limit):
        try:
            view = ensure(
                failure["source_table"],
                failure["source_id"],
                kind=failure.get("kind"),
                owner_id=failure.get("principal"),
                project_id=failure.get("project_id"),
            )
        except Exception as exc:  # noqa: BLE001 - 重试失败继续记账
            record_failure(
                source_table=failure["source_table"],
                source_id=failure["source_id"],
                kind=failure.get("kind"),
                principal=failure.get("principal"),
                project_id=failure.get("project_id"),
                error=f"{type(exc).__name__}: {exc}",
            )
            stats["still_failing"] += 1
            continue
        if view is None:
            stats["skipped"] += 1
            continue
        _resolve_failure(failure["id"])
        stats["registered"] += 1
    return stats


def _resolve_failure(failure_id: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE agent_registration_failure SET resolved_at = ?, updated_at = ? "
            "WHERE id = ?",
            (now(), now(), failure_id),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 执行后钩子
# --------------------------------------------------------------------------- #
def register_refs(
    principal: int,
    refs: Iterable[Dict[str, Any]],
    *,
    project_id: Optional[str] = None,
    state_changed: bool = False,
) -> Dict[str, int]:
    """业务执行成功后，按 ``business_refs`` 登记真实归属（必要时递增版本）。

    返回计数，便于日志与测试断言；单条失败不影响其它条目，也不会抛给调用方
    （业务已经成功，抛错会让上层误判为"业务失败"）。
    """
    stats = {"registered": 0, "existing": 0, "bumped": 0, "failed": 0}
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        kind = str(ref.get("kind") or ref.get("type") or "").strip()
        business_id = str(ref.get("id") or "").strip()
        if not kind or not business_id:
            continue
        table = SOURCE_TABLE_BY_KIND.get(kind)
        if not table:
            # 未声明的类型：不登记也不算失败（例如外部 MCP 资源）
            continue
        try:
            view = ensure(
                table,
                business_id,
                kind=kind,
                owner_id=principal,
                project_id=project_id,
            )
        except Exception as exc:  # noqa: BLE001 - 留痕，不掩盖业务成功
            record_failure(
                source_table=table,
                source_id=business_id,
                kind=kind,
                principal=principal,
                project_id=project_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            stats["failed"] += 1
            continue
        if view is None:
            record_failure(
                source_table=table,
                source_id=business_id,
                kind=kind,
                principal=principal,
                project_id=project_id,
                error="归属无法确认，未登记",
            )
            stats["failed"] += 1
            continue
        if not view.get("created"):
            stats["existing"] += 1
            if state_changed:
                base = int(view.get("revision") or 1)
                bump(table, business_id)
                stats["bumped"] += 1
                if ref.get("version") in (None, "", str(base)):
                    ref["version"] = str(base + 1)
        else:
            stats["registered"] += 1
    return stats


def access_check(
    kind: str,
    business_id: str,
    user_id: int,
    action: str = "read",
) -> Dict[str, Any]:
    """原页面使用的"本人资源"授权：解析真实归属后再判权限。

    与 Agent 共用同一条规则：本人可读写；系统默认资源（共享只读）可读；
    管理员可维护；归属无法确认一律拒绝（不公开）。
    """
    from agent.identity import load_actor

    table = SOURCE_TABLE_BY_KIND.get(kind)
    if not table:
        raise PermissionError(f"未声明的资源类型：{kind}")
    actor = load_actor(int(user_id))

    conn = connect()
    try:
        row = query_one(
            conn,
            "SELECT * FROM agent_resource WHERE source_table = ? AND source_id = ?",
            (table, business_id),
        )
    finally:
        conn.close()
    view = permissions._resource_view(row) if row else None
    if view is None:
        # 历史/新写入但尚未登记：能推导出归属就顺手补登记（幂等、不猜）
        view = ensure(table, business_id, kind=kind)

    exists = view is not None or business_row(table, business_id) is not None
    if not exists:
        raise LookupError("资源不存在")
    if actor.is_admin:
        # 管理员维护历史资源；未登记时不返回索引元数据，只放行页面自身的查询
        return view or {
            "id": business_id,
            "kind": kind,
            "owner_id": actor.user_id,
            "access_policy": permissions.POLICY_PRIVATE,
            "revision": 0,
        }
    if view is None:
        raise PermissionError("资源归属无法确认")
    try:
        permissions.require_access(actor, view, action)
    except AgentError as exc:
        # 页面层只区分"存在但无权"与"不存在"，不把内部错误码透给浏览器
        raise PermissionError(getattr(exc, "message", str(exc))) from exc
    return view

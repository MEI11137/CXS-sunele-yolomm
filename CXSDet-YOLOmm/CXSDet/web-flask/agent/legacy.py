"""历史业务数据的归属迁移（B-01）。

现有 ``datasets`` / ``models`` / ``detection_records`` 没有"项目 + 所有者"概念。
本模块把它们登记进资源索引，规则与方案 3.4 一致：

* 已确认的默认资源（``default-`` 前缀）→ 共享只读，归入"系统默认资源"项目；
* 其余历史数据集与模型 → 原管理员（最早的管理员账号）的"历史项目"，保持私有；
* 历史检测记录 → 保留原提交用户，归入该用户自己的"历史项目"；
* 无法判定归属的旧文件不对外公开，等管理员核对后再归档。

迁移幂等：``agent_resource`` 上 ``(source_table, source_id)`` 有唯一索引，
重复执行不会产生重复条目。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.permissions import ORIGIN_LEGACY, ORIGIN_SYSTEM, POLICY_PRIVATE
from agent.permissions import POLICY_SHARED_READONLY
from agent.store import connect, dumps, new_id, now, query_all, query_one

_MIGRATION_KEY = "business_resource_ownership"


def _project_for(
    conn,
    owner_id: int,
    name: str,
    description: str,
    origin: str,
) -> str:
    row = query_one(
        conn,
        "SELECT id FROM agent_project WHERE owner_id = ? AND name = ?",
        (owner_id, name),
    )
    if row:
        return row["id"]
    project_id = new_id("prj")
    stamp = now()
    conn.execute(
        "INSERT INTO agent_project "
        "(id, owner_id, name, description, requirements_json, origin, status, "
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
        (project_id, owner_id, name, description, dumps({}), origin, stamp, stamp),
    )
    return project_id


def _register(
    conn,
    *,
    kind: str,
    project_id: str,
    owner_id: int,
    display_name: str,
    source_table: str,
    source_id: str,
    access_policy: str,
    modality: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    existing = query_one(
        conn,
        "SELECT id FROM agent_resource WHERE source_table = ? AND source_id = ?",
        (source_table, source_id),
    )
    if existing:
        return False
    stamp = now()
    conn.execute(
        "INSERT INTO agent_resource "
        "(id, kind, project_id, owner_id, display_name, revision, access_policy, "
        " modality, source_table, source_id, metadata_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id("res"),
            kind,
            project_id,
            owner_id,
            display_name,
            access_policy,
            modality,
            source_table,
            source_id,
            dumps(metadata or {}),
            stamp,
            stamp,
        ),
    )
    return True


def _default_admin_id(conn) -> Optional[int]:
    if not _table_exists(conn, "user"):
        return None
    row = query_one(
        conn,
        "SELECT id FROM user WHERE role = 1 ORDER BY id LIMIT 1",
    )
    return int(row["id"]) if row else None


def _table_exists(conn, table: str) -> bool:
    row = query_one(
        conn, "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return bool(row)


def migrate_legacy_resources() -> Dict[str, int]:
    """执行一次性迁移，返回各类登记数量，便于日志与测试断言。"""
    result = {"datasets": 0, "models": 0, "detection_records": 0, "skipped": 0}
    conn = connect()
    try:
        admin_id = _default_admin_id(conn)
        if admin_id is None:
            # 没有管理员时不动历史数据，交由管理员登录后的迁移执行
            return result

        legacy_project = _project_for(
            conn,
            admin_id,
            "历史项目",
            "原系统已有的数据集与模型，按管理员归属保留，未对外公开",
            ORIGIN_LEGACY,
        )
        system_project = _project_for(
            conn,
            admin_id,
            "系统默认资源",
            "已确认的系统默认数据集与模型，登录用户可只读使用",
            ORIGIN_SYSTEM,
        )

        if _table_exists(conn, "datasets"):
            for row in query_all(conn, "SELECT id, name, dataset_mode FROM datasets"):
                is_default = str(row["id"]).startswith("default-")
                created = _register(
                    conn,
                    kind="dataset",
                    project_id=system_project if is_default else legacy_project,
                    owner_id=admin_id,
                    display_name=row.get("name") or row["id"],
                    source_table="datasets",
                    source_id=row["id"],
                    access_policy=(
                        POLICY_SHARED_READONLY if is_default else POLICY_PRIVATE
                    ),
                    modality=row.get("dataset_mode") or "single",
                    metadata={"migrated": True, "is_system_default": is_default},
                )
                result["datasets"] += int(created)

        if _table_exists(conn, "models"):
            for row in query_all(conn, "SELECT id, name, model_mode FROM models"):
                is_default = str(row["id"]).startswith("default-")
                created = _register(
                    conn,
                    kind="model",
                    project_id=system_project if is_default else legacy_project,
                    owner_id=admin_id,
                    display_name=row.get("name") or row["id"],
                    source_table="models",
                    source_id=row["id"],
                    access_policy=(
                        POLICY_SHARED_READONLY if is_default else POLICY_PRIVATE
                    ),
                    modality=row.get("model_mode") or "single",
                    metadata={"migrated": True, "is_system_default": is_default},
                )
                result["models"] += int(created)

        if _table_exists(conn, "detection_records"):
            users: Dict[int, str] = {}
            for row in query_all(conn, "SELECT id, user_id, title FROM detection_records"):
                owner_id = int(row.get("user_id") or 0)
                if owner_id <= 0:
                    result["skipped"] += 1
                    continue
                if owner_id not in users:
                    users[owner_id] = _project_for(
                        conn,
                        owner_id,
                        "历史项目",
                        "账号已有的检测记录，保持原提交用户归属",
                        ORIGIN_LEGACY,
                    )
                created = _register(
                    conn,
                    kind="detection_record",
                    project_id=users[owner_id],
                    owner_id=owner_id,
                    display_name=row.get("title") or f"检测记录 {row['id'][:8]}",
                    source_table="detection_records",
                    source_id=row["id"],
                    access_policy=POLICY_PRIVATE,
                    metadata={"migrated": True},
                )
                result["detection_records"] += int(created)

        conn.commit()
    finally:
        conn.close()
    return result

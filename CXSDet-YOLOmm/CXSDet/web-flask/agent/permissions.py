"""项目工作区与资源索引（B-01）。

同一套 ``require_access`` / ``resolve_resource`` 同时约束原页面与 Agent：
路由层不自己判断归属，只有通过这里才能拿到可操作的对象与版本。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from agent.errors import AgentError, Code
from agent.identity import ActorContext
from agent.store import connect, dumps, loads, new_id, now, query_all, query_one

ORIGIN_USER = "user"
ORIGIN_LEGACY = "legacy"
ORIGIN_SYSTEM = "system"

POLICY_PRIVATE = "private"
POLICY_SHARED_READONLY = "shared_readonly"

READ_OPERATIONS = {"read", "list", "query"}
WRITE_OPERATIONS = {"write", "update", "execute", "publish", "train", "delete"}


# --------------------------------------------------------------------------- #
# 项目工作区
# --------------------------------------------------------------------------- #
def create_project(
    actor: ActorContext,
    name: str,
    description: str = "",
    *,
    origin: str = ORIGIN_USER,
    owner_id: Optional[int] = None,
    root_directory: Optional[str] = None,
    requirements: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    clean = (name or "").strip()
    if not clean:
        raise AgentError(Code.VALIDATION, "项目名称不能为空")
    if requirements is not None:
        _validate_requirements(requirements)
    # owner_id 只允许后台迁移使用；HTTP 入口传入的值会被忽略
    target_owner = owner_id if actor.source != "http" and owner_id else actor.user_id
    if actor.source == "http":
        actor.assert_owner(target_owner)

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if root_directory is not None:
            from agent.project_folders import validate_root
            root_directory = validate_root(actor, root_directory, conn)
        exists = query_one(
            conn,
            "SELECT id FROM agent_project WHERE owner_id = ? AND name = ?",
            (target_owner, clean),
        )
        if exists:
            raise AgentError(Code.PROJECT_EXISTS, "同名项目已存在", http_status=409)
        project_id = new_id("prj")
        stamp = now()
        conn.execute(
            "INSERT INTO agent_project "
            "(id, owner_id, name, description, requirements_json, origin, status, "
            " created_at, updated_at, root_directory) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (project_id, target_owner, clean, description, dumps(requirements or {}), origin, stamp, stamp, root_directory),
        )
        conn.commit()
    finally:
        conn.close()
    return get_project(actor, project_id)


def get_project(actor: ActorContext, project_id: str) -> Dict[str, Any]:
    conn = connect()
    try:
        row = query_one(conn, "SELECT * FROM agent_project WHERE id = ?", (project_id,))
    finally:
        conn.close()
    if not row or row.get("status") != "active":
        raise AgentError(Code.PROJECT_NOT_FOUND, "项目不存在", http_status=404)
    if int(row["owner_id"]) != actor.user_id:
        # 不暴露其他用户的项目是否存在
        raise AgentError(Code.PROJECT_NOT_FOUND, "项目不存在", http_status=404)
    return _project_view(row)


def find_project(project_id: str) -> Optional[Dict[str, Any]]:
    """不做归属过滤地读项目行，供权限桥判断项目策略时使用。"""
    if not project_id:
        return None
    conn = connect()
    try:
        row = query_one(conn, "SELECT * FROM agent_project WHERE id = ?", (project_id,))
    finally:
        conn.close()
    return _project_view(row) if row and row.get("status") == "active" else None


def project_access_policy(project: Dict[str, Any]) -> str:
    """项目可见性：系统默认资源项目对登录用户只读，其余项目私有。"""
    origin = str(project.get("origin") or ORIGIN_USER)
    if origin == ORIGIN_SYSTEM:
        return POLICY_SHARED_READONLY
    return POLICY_PRIVATE


def list_projects(actor: ActorContext) -> List[Dict[str, Any]]:
    conn = connect()
    try:
        rows = query_all(
            conn,
            "SELECT * FROM agent_project WHERE owner_id = ? AND status = 'active' "
            "ORDER BY updated_at DESC",
            (actor.user_id,),
        )
    finally:
        conn.close()
    return [_project_view(row) for row in rows]


def update_project(
    actor: ActorContext,
    project_id: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    requirements: Optional[Dict[str, Any]] = None,
    root_directory: Optional[str] = None,
) -> Dict[str, Any]:
    project = get_project(actor, project_id)
    updates: List[str] = []
    params: List[Any] = []
    if name is not None:
        clean = name.strip()
        if not clean:
            raise AgentError(Code.VALIDATION, "项目名称不能为空")
        updates.append("name = ?")
        params.append(clean)
    if description is not None:
        updates.append("description = ?")
        params.append(description)
    if requirements is not None:
        _validate_requirements(requirements)
        updates.append("requirements_json = ?")
        params.append(dumps(requirements))
    if not updates and root_directory is None:
        return project
    updates.append("updated_at = ?")
    params.append(now())
    params.append(project_id)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if root_directory is not None:
            from agent.project_folders import validate_root
            actual = query_one(conn, "SELECT root_directory FROM agent_project WHERE id=?", (project_id,))
            if actual["root_directory"]:
                raise AgentError(Code.VALIDATION, "工作区已绑定目录，不能直接更换；请新建工作区")
            folder = validate_root(actor, root_directory, conn, project_id)
            updates.insert(0, "root_directory = ?")
            params.insert(0, folder)
        conn.execute(f"UPDATE agent_project SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
    finally:
        conn.close()
    return get_project(actor, project_id)


def _validate_requirements(requirements: Dict[str, Any]) -> None:
    """项目要求只能细化任务，不能扩权。出现越权声明直接拒绝写入。"""
    if not isinstance(requirements, dict):
        raise AgentError(Code.VALIDATION, "项目要求必须是对象")
    forbidden_keys = {
        "allow_unconfirmed_writes",
        "skip_confirmation",
        "auto_approve",
        "grant_all_tools",
        "expand_permissions",
    }
    for key in forbidden_keys:
        if requirements.get(key):
            raise AgentError(
                Code.VALIDATION,
                "项目要求不能扩大权限或跳过人工确认",
                http_status=400,
                details={"field": key},
            )
    text = str(requirements.get("notes") or "")
    if "跳过确认" in text or "自动批准" in text:
        raise AgentError(Code.VALIDATION, "项目要求不能包含跳过确认的说明")


def ensure_default_project(actor: ActorContext, name: str = "默认项目") -> Dict[str, Any]:
    existing = list_projects(actor)
    if existing:
        return existing[0]
    return create_project(actor, name, "系统自动创建，用于承载未归档的会话与资源")


def _project_view(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row.get("description") or "",
        "root_directory": row.get("root_directory"),
        "owner_id": int(row["owner_id"]),
        "origin": row.get("origin") or ORIGIN_USER,
        "status": row.get("status") or "active",
        "requirements": loads(row.get("requirements_json")),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# --------------------------------------------------------------------------- #
# 资源索引
# --------------------------------------------------------------------------- #
def register_resource(
    actor: ActorContext,
    *,
    project_id: str,
    kind: str,
    display_name: str,
    source_table: Optional[str] = None,
    source_id: Optional[str] = None,
    access_policy: str = POLICY_PRIVATE,
    modality: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    owner_id: Optional[int] = None,
) -> Dict[str, Any]:
    """登记业务资源归属。第三人新建数据集/模型/记录后必须调用本函数。

    调用约定（INT-03）：

    * 本函数是**服务端内部接口**，不接受把请求体原样透传；``source_table`` /
      ``source_id`` 只能由已授权的业务服务传入（HTTP 边界的拦截在
      :mod:`agent.routes` 的登记端点里）；
    * 命中"已存在的来源"时先做可见性判断再返回，不能借幂等返回把别人的索引
      元数据（项目、名称、版本）透露出去。
    """
    project = get_project(actor, project_id)  # 顺带校验项目归属
    target_owner = int(project["owner_id"])
    if owner_id is not None and actor.source != "http":
        target_owner = owner_id

    conn = connect()
    try:
        if source_table and source_id:
            existing = query_one(
                conn,
                "SELECT * FROM agent_resource WHERE source_table = ? AND source_id = ?",
                (source_table, source_id),
            )
            if existing:
                view = _resource_view(existing)
                try:
                    require_access(actor, view, "read")
                except AgentError:
                    # 别人已登记的同源资源对当前账号等同于不存在
                    raise AgentError(
                        Code.RESOURCE_NOT_FOUND, "引用的资源不存在", http_status=404
                    ) from None
                return view
        resource_id = new_id("res")
        stamp = now()
        conn.execute(
            "INSERT INTO agent_resource "
            "(id, kind, project_id, owner_id, display_name, revision, access_policy, "
            " modality, source_table, source_id, metadata_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
            (
                resource_id,
                kind,
                project_id,
                target_owner,
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
        conn.commit()
        row = query_one(conn, "SELECT * FROM agent_resource WHERE id = ?", (resource_id,))
    finally:
        conn.close()
    return _resource_view(row)


def bump_revision(conn, resource_id: str) -> int:
    """资源内容变化时递增版本，使旧确认失效。"""
    conn.execute(
        "UPDATE agent_resource SET revision = revision + 1, updated_at = ? WHERE id = ?",
        (now(), resource_id),
    )
    row = query_one(conn, "SELECT revision FROM agent_resource WHERE id = ?", (resource_id,))
    return int(row["revision"]) if row else 0


def list_project_resources(
    actor: ActorContext,
    project_id: str,
    kinds: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    get_project(actor, project_id)
    sql = "SELECT * FROM agent_resource WHERE project_id = ? AND deleted_at IS NULL"
    params: List[Any] = [project_id]
    kind_list = [k for k in (kinds or []) if k]
    if kind_list:
        from agent.resources import SOURCE_TABLE_BY_KIND
        tables = sorted({SOURCE_TABLE_BY_KIND[k] for k in kind_list if k in SOURCE_TABLE_BY_KIND})
        sql += " AND (kind IN (" + ",".join("?" for _ in kind_list) + ")"
        params.extend(kind_list)
        if tables:
            sql += " OR source_table IN (" + ",".join("?" for _ in tables) + ")"
            params.extend(tables)
        sql += ")"
    sql += " ORDER BY updated_at DESC"
    conn = connect()
    try:
        rows = query_all(conn, sql, params)
    finally:
        conn.close()
    return [_resource_view(row) for row in rows]


def resolve_resource(actor: ActorContext, ref: Any) -> Dict[str, Any]:
    """把模型/前端给的引用解析成真实对象与版本。

    只接受 ID、``{resource_id}``、``{kind,id}``、``{source_table,source_id}`` 这类
    结构，不接受文件路径或任意字符串拼接，避免路径穿越与越权枚举。
    """
    resource_row = None
    conn = connect()
    try:
        if isinstance(ref, str):
            resource_row = query_one(
                conn, "SELECT * FROM agent_resource WHERE id = ?", (ref,)
            )
            if not resource_row and ref:
                resource_row = query_one(
                    conn,
                    "SELECT * FROM agent_resource WHERE source_table = ? AND source_id = ?",
                    (str(ref).split(":", 1)[0], str(ref).split(":", 1)[-1]),
                )
        elif isinstance(ref, dict):
            if ref.get("resource_id"):
                resource_row = query_one(
                    conn, "SELECT * FROM agent_resource WHERE id = ?", (ref["resource_id"],)
                )
            elif ref.get("source_table") and ref.get("source_id"):
                resource_row = query_one(
                    conn,
                    "SELECT * FROM agent_resource WHERE source_table = ? AND source_id = ?",
                    (ref["source_table"], ref["source_id"]),
                )
            elif ref.get("id"):
                resource_row = query_one(
                    conn, "SELECT * FROM agent_resource WHERE id = ?", (ref["id"],)
                )
                if not resource_row and ref.get("kind"):
                    from agent.resources import SOURCE_TABLE_BY_KIND
                    table = SOURCE_TABLE_BY_KIND.get(ref['kind'])
                    resource_row = query_one(
                        conn,
                        "SELECT * FROM agent_resource WHERE source_id = ? AND " + ("source_table = ?" if table else "kind = ?"),
                        (ref["id"], table or ref["kind"]),
                    )
        else:
            raise AgentError(Code.VALIDATION, "资源引用格式不正确")
    finally:
        conn.close()

    if not resource_row or resource_row.get('deleted_at'):
        raise AgentError(Code.RESOURCE_NOT_FOUND, "引用的资源不存在", http_status=404)

    owner_id = int(resource_row["owner_id"])
    if owner_id != actor.user_id and resource_row["access_policy"] != POLICY_SHARED_READONLY:
        # 其他用户的私有资源统一按"不存在"处理，避免探测
        raise AgentError(Code.RESOURCE_NOT_FOUND, "引用的资源不存在", http_status=404)
    if not find_project(resource_row["project_id"]):
        raise AgentError(Code.RESOURCE_NOT_FOUND, "资源所属项目已移入回收站", http_status=404)
    return _resource_view(resource_row)


def require_access(actor: ActorContext, resource: Dict[str, Any], operation: str) -> bool:
    """统一授权入口：原页面与 Agent 工具都必须先过这一关。"""
    op = (operation or "read").lower()
    if resource.get('deleted_at'):
        raise AgentError(Code.RESOURCE_NOT_FOUND, '资源已移入回收站，请先恢复', http_status=404)
    if resource.get("project_id") and not find_project(resource["project_id"]):
        raise AgentError(Code.RESOURCE_NOT_FOUND, "资源所属项目已移入回收站", http_status=404)
    if op not in READ_OPERATIONS and op not in WRITE_OPERATIONS:
        raise AgentError(Code.VALIDATION, f"未知的操作类型：{operation}")
    owner_id = int(resource.get("owner_id") or 0)
    policy = resource.get("access_policy") or POLICY_PRIVATE

    if owner_id == actor.user_id:
        return True
    if policy == POLICY_SHARED_READONLY and op in READ_OPERATIONS:
        return True
    # 管理员的跨用户写操作必须走显式管理入口，不在这里放行
    raise AgentError(
        Code.RESOURCE_FORBIDDEN,
        "该资源不在当前账号的授权范围内",
        http_status=403,
        details={"resource_id": resource.get("id"), "operation": op},
    )


def allowed_actions(actor: ActorContext, resource: Dict[str, Any]) -> List[str]:
    actions = ["read"]
    if int(resource.get("owner_id") or 0) == actor.user_id:
        actions.extend(["write", "execute"])
        if resource.get("kind") == "model":
            actions.append("publish")
    return actions


def _resource_view(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "project_id": row["project_id"],
        "owner_id": int(row["owner_id"]),
        "display_name": row["display_name"],
        "revision": int(row["revision"]),
        "access_policy": row["access_policy"],
        "modality": row.get("modality"),
        "source_table": row.get("source_table"),
        "source_id": row.get("source_id"),
        "metadata": loads(row.get("metadata_json")),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "deleted_at": row.get("deleted_at"),
    }


def decorate_with_actions(actor: ActorContext, resource: Dict[str, Any]) -> Dict[str, Any]:
    """补上第一人任务书约定的 ``allowed_actions`` 字段。"""
    view = dict(resource)
    view["allowed_actions"] = allowed_actions(actor, resource)
    return view

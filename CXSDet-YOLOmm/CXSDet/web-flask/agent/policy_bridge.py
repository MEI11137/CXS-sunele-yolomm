"""B↔C 权限桥（INT-01）。

C 的业务工具层要求注入两个回调：

- ``require_access(principal:int, action:str, snapshot|None) -> True``，否则抛 ``PermissionError``
- ``resolve_resource(principal:int, kind:str, resource_id:str) -> ResourceSnapshot``

而 B 的公共权限是围绕 ``ActorContext`` + ``agent_resource`` 索引设计的，动作名与
资源 kind 命名都不一样。本模块做显式映射，**不做参数顺序交换**，也不给未知动作
兜底放行：任何没在表里声明的动作都按拒绝处理。

本模块只负责"能不能做"的判断；真实归属登记、版本递增与登记失败留痕在
:mod:`agent.resources`（INT-03），解析时遇到尚未登记的业务行会按业务行的
真实归属补登记一次，不采信调用方声明的归属。
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, Optional

from agent import permissions, resources
from agent.errors import AgentError, Code
from agent.identity import ActorContext, load_actor

# C 的 kind → B 索引里的 source_table。``record`` 与 ``detection_record`` 是同一个
# 业务对象的两种叫法，统一映射到 detection_records。映射表放在 resources 里，
# 让"登记"和"解析"用的是同一份定义。
KIND_TO_SOURCE_TABLE: Dict[str, str] = dict(resources.SOURCE_TABLE_BY_KIND)

# C 的动作 → B 的读/写语义。B 只认 read/list/query 与
# write/update/execute/publish/train/delete，这里显式翻译。
# 未列出的动作 = 未知动作 = 拒绝（fail closed）。
ACTION_TO_B_OPERATION: Dict[str, str] = {
    "dataset:read": "read",
    "model:read": "read",
    "file:read": "read",
    "detection:read": "read",
    "draft:read": "read",
    "document:read": "read",
    "directory:read": "read",
    "dataset:create": "write",
    "dataset:update": "write",
    "dataset:delete": "write",
    "model:create": "write",
    "model:update": "write",
    "model:delete": "write",
    "model:publish": "publish",
    "draft:create": "write",
    "draft:update": "write",
    "draft:finalize": "write",
    "detection:create": "write",
    "detection:review": "write",
    "training:create": "train",
    "document:create": "write",
    "document:update": "write",
    "file:create": "write",
    "file:update": "write",
    "file:delete": "write",
    "directory:manage": "write",
    "mcp:manage": "write",
    "mcp:execute": "execute",
}

# 账号级（快照为空）也允许的动作：只是为了通过工具清单过滤，
# 真正的新资源归属仍要由 INT-03 登记到本人项目。
ACCOUNT_LEVEL_ACTIONS = {"tool:list"} | {
    "dataset:create",
    "model:create",
    "detection:create",
    "draft:create",
    "document:create",
    "training:create",
    "mcp:manage",
    "mcp:execute",
}

# 需要全局权限的动作。普通用户发布自己的模型不应依赖它（INT-05 会移除该依赖），
# 在这里先按真实语义只给管理员。
ADMIN_ONLY_ACTIONS = {"model:publish-global"}

# 文件动作 → 读/写语义。C 的 FileAccess 用 "file:create"/"file:read" 调这里，
# 未列出的动作按写处理（更严格）。
_FILE_ACTIONS: Dict[str, str] = {
    "file:read": "read",
    "file:preview": "read",
    "file:download": "read",
    "file:list": "read",
    "file:create": "write",
    "file:update": "write",
    "file:delete": "write",
}


def _snapshot_owner(snapshot: Any) -> int:
    return int(getattr(snapshot, "owner_id", 0) or 0)


class AgentPolicyBridge:
    """把 B 的权限语义暴露成 C 需要的签名。"""

    def __init__(self) -> None:
        # 记录拒绝次数，便于装配报告里给出"为什么工具不可用"的可核实数字。
        self.denied_actions: Dict[str, int] = {}

    # ------------------------------------------------------------------ 内部

    def _actor(self, principal: int) -> ActorContext:
        """C 的契约只认 ``PermissionError``，这里统一转换，避免上层 500。"""
        if not isinstance(principal, int) or isinstance(principal, bool):
            raise PermissionError("principal 必须是整数用户 ID")
        try:
            return load_actor(principal)
        except AgentError as exc:
            raise PermissionError(getattr(exc, "message", str(exc))) from exc

    def _deny(self, action: str, reason: str) -> None:
        self.denied_actions[action] = self.denied_actions.get(action, 0) + 1
        raise PermissionError(reason)

    def _resource_dict(self, snapshot: Any) -> Dict[str, Any]:
        """把 C 的快照整理成 B 的 require_access 能读的结构。"""
        return {
            "id": getattr(snapshot, "id", None),
            "owner_id": _snapshot_owner(snapshot),
            "access_policy": self._access_policy(getattr(snapshot, "id", None)),
        }

    def _access_policy(self, resource_id: Optional[str]) -> str:
        """索引里登记的可见性；查不到就按私有处理（默认不共享）。"""
        if not resource_id:
            return permissions.POLICY_PRIVATE
        try:
            from agent.store import connect, query_one

            conn = connect()
            try:
                row = query_one(
                    conn,
                    "SELECT access_policy FROM agent_resource WHERE id = ? OR source_id = ?",
                    (resource_id, resource_id),
                )
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - 读不到索引时按最严格处理
            return permissions.POLICY_PRIVATE
        return (row or {}).get("access_policy") or permissions.POLICY_PRIVATE

    # ----------------------------------------------------------- C 要求的两个

    def require_access(self, principal: int, action: str, snapshot: Any) -> bool:
        """C 的授权入口。允许返回 True，拒绝抛 PermissionError。"""
        if not action:
            self._deny(str(action), "缺少权限动作")
        if action in ADMIN_ONLY_ACTIONS:
            actor = self._actor(principal)
            if not actor.is_admin:
                self._deny(action, "该动作仅限管理员")
            return True
        if action in ACCOUNT_LEVEL_ACTIONS or action == "tool:list":
            # 账号级动作：确认账号真实存在且未被停用即可，不代表拥有任何业务对象。
            self._actor(principal)
            return True
        if action not in ACTION_TO_B_OPERATION:
            self._deny(action, f"未声明的权限动作：{action}")
        if snapshot is None:
            self._deny(action, "该动作必须绑定具体业务资源")
        actor = self._actor(principal)
        try:
            return bool(
                permissions.require_access(
                    actor, self._resource_dict(snapshot), ACTION_TO_B_OPERATION[action]
                )
            )
        except AgentError as exc:
            self._deny(action, exc.message if hasattr(exc, "message") else str(exc))
            return False  # 不可达，_deny 必抛

    def resolve_resource(self, principal: int, kind: str, resource_id: str):
        """C 的资源解析入口，返回带真实业务 ID/项目/归属/版本的快照。"""
        from services.tool_common import ResourceSnapshot

        actor = self._actor(principal)
        if not isinstance(resource_id, str) or not resource_id:
            raise PermissionError("缺少业务资源 ID")
        snapshot = self._resolve(actor, kind, resource_id)
        if snapshot is None:
            raise PermissionError("引用的业务资源不存在或未登记授权")
        return snapshot

    # ------------------------------------------------------------------ 解析

    def _resolve(self, actor: ActorContext, kind: str, resource_id: str):
        from services.tool_common import ResourceSnapshot

        if kind == "workspace":
            # 账号级虚拟工作区：解析成默认项目，归属本人。
            project = permissions.ensure_default_project(actor)
            return ResourceSnapshot(
                id=project["id"],
                project_id=project["id"],
                owner_id=int(project["owner_id"]),
                version=str(project.get("updated_at") or project.get("created_at") or "v1"),
            )
        if kind == "project":
            project = permissions.get_project(actor, resource_id)
            return ResourceSnapshot(
                id=project["id"],
                project_id=project["id"],
                owner_id=int(project["owner_id"]),
                version=str(project.get("updated_at") or project.get("created_at") or "v1"),
            )

        table = KIND_TO_SOURCE_TABLE.get(kind)
        if not table:
            raise PermissionError(f"未声明的资源类型：{kind}")

        row = self._index_row(table, resource_id)
        if row is None:
            # 业务已写入但还没登记（历史数据或刚创建）：按业务行的真实归属补登记。
            # 归属推不出来就不登记，也不放行。
            if resources.ensure_kind(kind, resource_id) is None:
                return None
            row = self._index_row(table, resource_id)
        if row is None:
            return None
        owner_id = int(row["owner_id"])
        policy = row.get("access_policy") or permissions.POLICY_PRIVATE
        if owner_id != actor.user_id and policy != permissions.POLICY_SHARED_READONLY:
            # 别人的私有资源按"不存在"处理，不泄漏是否存在。
            return None
        return ResourceSnapshot(
            id=str(row["source_id"] or resource_id),
            project_id=str(row["project_id"]),
            owner_id=owner_id,
            version=f"r{int(row.get('revision') or 1)}",
        )

    def _index_row(self, source_table: str, resource_id: str) -> Optional[Dict[str, Any]]:
        """按来源表 + 业务 ID 查索引；索引 ID 与业务 ID 不混用。"""
        from agent.store import connect, query_one

        conn = connect()
        try:
            return query_one(
                conn,
                "SELECT * FROM agent_resource WHERE source_table = ? AND source_id = ?",
                (source_table, resource_id),
            )
        finally:
            conn.close()

    # ------------------------------------------------------- C 的文件访问策略

    def project_file_access(self, user_id: int, project_id: str, action: str) -> bool:
        """``app.config['PROJECT_FILE_ACCESS']`` 的实现：上传/预览/下载共用同一策略。

        规则与资源索引一致，不再"只有管理员能传文件"：

        * 项目归属人：读写都允许；
        * 系统默认资源项目（``origin=system``）：登录用户只读，不能写入；
        * 管理员：用于维护历史资源；
        * 其他：拒绝。
        """
        if not project_id:
            return False
        try:
            actor = load_actor(int(user_id))
        except Exception:  # noqa: BLE001 - 未登录或停用账号一律拒绝
            return False

        project = permissions.find_project(project_id)
        if project is None:
            return False

        operation = _FILE_ACTIONS.get(str(action or "").lower(), "write")
        if int(project["owner_id"]) == actor.user_id:
            return True
        if actor.is_admin:
            return True
        if permissions.project_access_policy(project) == permissions.POLICY_SHARED_READONLY:
            return operation == "read"
        return False

    # ------------------------------------------------------------- 装配报告用

    def denial_summary(self) -> Dict[str, int]:
        return dict(self.denied_actions)


def iter_business_refs(event: Any) -> Iterator[Dict[str, Any]]:
    """从 C 的通知事件里取出业务引用（结构见 business_tools._notify_change）。"""
    if not isinstance(event, dict):
        return iter(())
    refs = event.get("business_refs") or event.get("refs") or []
    if not isinstance(refs, list):
        return iter(())
    return iter([ref for ref in refs if isinstance(ref, dict)])


class BusinessNotifier:
    """C 的业务变更通知接收器。

    只做"记账 + 唤起核对"：按业务引用找到本用户正处于等待状态的任务，追加一条
    事件让前端刷新，**不改任务状态、不代替真实查询**。真正的接力推进与自动
    resume 属于 INT-09。
    """

    def __init__(self) -> None:
        self.received: int = 0
        self.matched_tasks: int = 0
        self.errors: list = []

    def __call__(self, principal: int, event: Dict[str, Any]) -> None:
        self.received += 1
        try:
            self._deliver(principal, event)
        except Exception as exc:  # noqa: BLE001 - 通知失败不能影响业务写入
            self.errors.append(f"{type(exc).__name__}: {exc}"[:200])

    def _deliver(self, principal: int, event: Dict[str, Any]) -> None:
        from agent import tasks as task_store

        refs = list(iter_business_refs(event))
        if not refs:
            return
        wanted = {
            (str(ref.get("type") or ref.get("kind") or ""), str(ref.get("id") or ""))
            for ref in refs
        }
        wanted = {item for item in wanted if item[1]}
        if not wanted:
            return
        for task in task_store.tasks_in_statuses(task_store.WAITING_STATUSES):
            if int(task.get("owner_id") or 0) != int(principal):
                continue
            mine = {
                (str(ref.get("kind") or ref.get("type") or ""), str(ref.get("id") or ""))
                for ref in (task.get("business_refs") or [])
                if isinstance(ref, dict)
            }
            if not (mine & wanted):
                continue
            task_store.append_event(
                task["session_id"],
                task["id"],
                "business.changed",
                {
                    "tool": event.get("tool") or event.get("tool_name"),
                    "refs": refs,
                    "note": "业务对象已变更，请核实真实状态",
                },
            )
            self.matched_tasks += 1

"""原页面的资源级授权（INT-03）。

改造前 C 的数据集/模型路由大多挂着 ``admin_required``：普通用户一律不可用，
而管理员可以操作任意人的资源。这里把它们统一改成"本人资源"授权：

* 资源归属来自服务端（``agent_resource`` 索引 + 业务行推导），不看请求体；
* 本人可读写，系统默认资源项目只读，管理员可维护历史资源；
* 归属无法判定的资源**不公开**，与 Agent 侧完全同一套判断；
* 列表类接口按同一规则过滤，不能靠删装饰器把别人的资源列出来。
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Dict, Iterable, Optional, Tuple

from flask import g, request
from utils.jwt_util import token_required
from utils.response import error

_LOGIN_HINT = "登录状态无效，请重新登录"


def _param_value(param: str) -> Optional[str]:
    view_args = request.view_args or {}
    if param in view_args and view_args[param]:
        return str(view_args[param])
    body = request.get_json(silent=True) if request.method in {"POST", "PUT", "PATCH"} else None
    if isinstance(body, dict) and body.get(param):
        return str(body[param])
    if request.args.get(param):
        return str(request.args[param])
    return None


def _current_user_id() -> int:
    return int(getattr(g, "user_id") or 0)


def check_resource(kind: str, business_id: str, action: str = "read") -> Optional[Any]:
    """授权通过返回 None，否则返回可直接 return 的错误响应。"""
    from agent.errors import AgentError
    from agent.resources import access_check

    user_id = _current_user_id()
    if not user_id:
        return error(_LOGIN_HINT, 401)
    try:
        access_check(kind, str(business_id), user_id, action)
    except LookupError:
        return error("资源不存在", 404)
    except PermissionError as exc:
        message = str(exc)
        if "未声明" in message:
            return error("资源不存在", 404)
        return error("无权访问该资源", 403)
    except AgentError as exc:
        return error(getattr(exc, "message", str(exc)), getattr(exc, "http_status", 403))
    return None


def bump_resource(kind: str, business_id: str) -> None:
    """内容/状态变化后递增资源版本，使旧确认与预览失效。"""
    from agent import resources

    table = resources.SOURCE_TABLE_BY_KIND.get(kind)
    if not table or not business_id:
        return
    try:
        resources.bump(table, str(business_id))
    except Exception as exc:  # noqa: BLE001 - 版本递增失败不能影响已成功的业务
        try:
            from flask import current_app

            current_app.logger.warning(
                "resource_revision_bump_failed kind=%s id=%s error=%s",
                kind,
                business_id,
                exc,
            )
        except Exception:  # noqa: BLE001
            pass


def _is_success(response) -> bool:
    """C 的响应体固定 HTTP 200，成功与否看 ``body['code']``。"""
    payload = None
    try:
        body = response[0] if isinstance(response, tuple) else response
        payload = body.get_json(silent=True)
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(payload, dict):
        return False
    return int(payload.get("code") or 0) == 200


def resource_required(kind: str, param: str, action: str = "read"):
    """校一个路由参数指向的资源是否属于当前账号。"""

    def decorator(func):
        @wraps(func)
        @token_required
        def wrapper(*args, **kwargs):
            resource_id = _param_value(param)
            if not resource_id:
                return error("缺少资源 ID", 400)
            blocked = check_resource(kind, resource_id, action)
            if blocked is not None:
                return blocked
            response = func(*args, **kwargs)
            if action != "read" and _is_success(response):
                # 写成功才算一次状态变化，避免失败的请求也让旧确认失效
                bump_resource(kind, resource_id)
            return response

        return wrapper

    return decorator


def resources_required(checks: Iterable[Tuple[str, str, str]]):
    """一次校多个资源（例如父模型与子模型都要可读）。"""

    def decorator(func):
        @wraps(func)
        @token_required
        def wrapper(*args, **kwargs):
            for kind, param, action in checks:
                resource_id = _param_value(param)
                if not resource_id:
                    return error(f"缺少资源 ID：{param}", 400)
                blocked = check_resource(kind, resource_id, action)
                if blocked is not None:
                    return blocked
            return func(*args, **kwargs)

        return wrapper

    return decorator


def project_target_required(func):
    """Validate explicit project targets before the legacy page writes anything."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        from flask import request
        from agent.identity import load_actor
        from agent import permissions
        from agent.errors import AgentError
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return error('请求体必须是对象', 400), 400
        project_id = request.form.get('project_id') or payload.get('project_id')
        if project_id:
            try:
                actor = load_actor(_current_user_id())
                project = permissions.get_project(actor, str(project_id))
                if project['owner_id'] != actor.user_id or project.get('origin') == 'system':
                    return error('目标项目必须是本人的可写项目', 403), 403
            except AgentError as exc:
                return error(exc.message, exc.http_status), exc.http_status
        return func(*args, **kwargs)
    return wrapper


def register_new(
    kind: str,
    business_id: str,
    *,
    project_id: Optional[str] = None,
    display_name: Optional[str] = None,
) -> None:
    """原页面创建资源后登记真实归属（失败只留痕，不影响已经成功的业务）。"""
    from agent import resources

    table = resources.SOURCE_TABLE_BY_KIND.get(kind)
    user_id = _current_user_id()
    if not table or not business_id or not user_id:
        return
    try:
        resources.ensure(
            table,
            str(business_id),
            kind=kind,
            owner_id=user_id,
            project_id=project_id,
            display_name=display_name,
        )
    except Exception as exc:  # noqa: BLE001 - 业务已成功，登记失败要留痕
        resources.record_failure(
            source_table=table,
            source_id=str(business_id),
            kind=kind,
            principal=user_id,
            project_id=project_id,
            error=f"{type(exc).__name__}: {exc}",
        )


def visible_ids(kind: str, action: str = "read") -> Optional[set]:
    """列表接口使用的可见 ID 集合；管理员返回 ``None`` 表示不过滤。"""
    from agent.identity import load_actor
    from agent import permissions, resources
    from agent.store import connect, query_all

    user_id = _current_user_id()
    if not user_id:
        return set()
    try:
        actor = load_actor(user_id)
    except Exception:  # noqa: BLE001 - 未登录/停用按空集处理
        return set()
    if actor.is_admin:
        return None

    table = resources.SOURCE_TABLE_BY_KIND.get(kind)
    if not table:
        return set()
    conn = connect()
    try:
        rows = query_all(
            conn,
            "SELECT source_id, owner_id, access_policy FROM agent_resource "
            "WHERE source_table = ? AND source_id IS NOT NULL",
            (table,),
        )
        # 尚未登记但业务行写明了归属的（例如刚生成的草稿），只能被真实归属人看到
        derived = _derived_ids(conn, kind, user_id)
    finally:
        conn.close()
    allowed = set()
    read_only = action in permissions.READ_OPERATIONS
    for row in rows:
        if int(row["owner_id"]) == user_id:
            allowed.add(str(row["source_id"]))
        elif read_only and row.get("access_policy") == permissions.POLICY_SHARED_READONLY:
            allowed.add(str(row["source_id"]))
    allowed |= derived
    return allowed


def _derived_ids(conn, kind: str, user_id: int) -> set:
    """按业务行的归属列补充可见 ID（不登记、不公开未判定资源）。"""
    from agent.store import query_all

    rules = {
        "draft": ("dataset_drafts", "creator_id"),
        "record": ("detection_records", "user_id"),
        "detection_record": ("detection_records", "user_id"),
    }
    rule = rules.get(kind)
    if not rule:
        return set()
    table, column = rule
    try:
        columns = {
            str(row[1]) for row in conn.execute('PRAGMA table_info("%s")' % table)
        }
    except Exception:  # noqa: BLE001 - 表不存在时没有可补充的 ID
        return set()
    if column not in columns:
        return set()
    rows = query_all(
        conn,
        'SELECT id FROM "%s" WHERE "%s" = ?' % (table, column),
        (user_id,),
    )
    return {str(row["id"]) for row in rows}

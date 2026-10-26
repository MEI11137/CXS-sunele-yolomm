"""Agent 对外 HTTP 接口（B-01 ~ B-09 的唯一出口）。

约定：

* 身份**只**来自 ``Authorization`` 头里的 JWT，再回库校验账号状态；
  请求体里的任何 ``user_id`` / ``owner_id`` / ``approved`` 都不被采信。
* 成功响应沿用站点既有结构 ``{code, msg, data}``，``code`` 为 200。
* 失败响应 HTTP 状态码与错误语义一致，``data.code`` 是稳定错误码字符串，
  ``data.execution_state`` 说明动作是否已执行（前端不能把 unknown 当成未执行）。
* 所有涉及他人的资源都会在 ``permissions``/``sessions``/``tasks`` 层被拒绝，
  这里不做任何"信任前端"的兜底。
"""

from __future__ import annotations

import json
import time
from functools import wraps
from typing import Any, Callable, Dict, List, Optional

from flask import Blueprint, Response, g, request, stream_with_context

from agent import __version__
from agent import actions as action_store
from agent import connections
from agent import identity
from agent import manuals as manual_store
from agent import memory as memory_store
from agent import operations as operation_store
from agent import permissions
from agent import recovery
from agent import sessions as session_store
from agent import tasks as task_store
from agent.errors import AgentError, Code
from agent.identity import ActorContext
from agent.runtime import get_runtime
from agent.tools import grants as grant_store
from agent.tools.registry import get_registry
from utils.jwt_util import get_user_id
from utils.response import success

agent_bp = Blueprint("agent", __name__, url_prefix="/api/agent")

# SSE 单次连接最长保持时间与轮询间隔（秒）
_STREAM_MAX_SECONDS = 60
_STREAM_TICK = 0.5
_STREAM_IDLE_TICKS = 20


# --------------------------------------------------------------------------- #
# 请求基础工具
# --------------------------------------------------------------------------- #
def _body() -> Dict[str, Any]:
    raw = request.get_json(silent=True)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AgentError(Code.VALIDATION, "请求体必须是 JSON 对象")
    return raw


def _str_field(
    body: Dict[str, Any],
    key: str,
    *,
    required: bool = True,
    default: str = "",
    max_chars: int = 4000,
) -> str:
    raw = body.get(key, default)
    if raw is None:
        raw = default
    if not isinstance(raw, str):
        raise AgentError(Code.VALIDATION, f"字段 {key} 必须是字符串")
    value = raw.strip()
    if required and not value:
        raise AgentError(Code.VALIDATION, f"缺少必填字段：{key}")
    if len(value) > max_chars:
        raise AgentError(Code.VALIDATION, f"字段 {key} 超过 {max_chars} 字上限")
    return value


def _int_arg(name: str, default: int, *, minimum: int = 0, maximum: int = 1000) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise AgentError(Code.VALIDATION, f"查询参数 {name} 必须是整数")
    return max(minimum, min(maximum, value))


def _optional_int(value: Any, field_name: str) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise AgentError(Code.VALIDATION, f"字段 {field_name} 必须是整数")


def _actor() -> ActorContext:
    token = (request.headers.get("Authorization") or "").strip()
    if not token:
        raise AgentError(Code.UNAUTHENTICATED, "缺少认证 Token", http_status=401)
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    user_id = get_user_id(token)
    if not user_id:
        raise AgentError(Code.UNAUTHENTICATED, "登录状态无效或已过期", http_status=401)
    return identity.load_actor(
        int(user_id),
        request_id=request.headers.get("X-Request-Id", ""),
        source="http",
    )


def auth_required(handler: Callable[..., Any]) -> Callable[..., Any]:
    """解析出可信身份并放入 ``g.actor``。"""

    @wraps(handler)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        g.actor = _actor()
        return handler(*args, **kwargs)

    return wrapper


def _current() -> ActorContext:
    actor = getattr(g, "actor", None)
    if actor is None:  # 理论上不会发生，防御性兜底
        raise AgentError(Code.UNAUTHENTICATED, "缺少登录状态", http_status=401)
    return actor


def _ok(data: Any = None, msg: str = "操作成功") -> Response:
    return success(data, msg)


def _failure(exc: AgentError) -> Response:
    payload = {"code": exc.http_status, "msg": exc.message, "data": exc.to_dict()}
    return Response(
        json.dumps(payload, ensure_ascii=False),
        status=exc.http_status,
        content_type="application/json; charset=utf-8",
    )


@agent_bp.errorhandler(AgentError)
def _handle_agent_error(exc: AgentError) -> Response:
    return _failure(exc)


# --------------------------------------------------------------------------- #
# 元信息
# --------------------------------------------------------------------------- #
@agent_bp.get("/ping")
@auth_required
def ping() -> Response:
    actor = _current()
    return _ok(
        {
            "version": __version__,
            "server_time": task_store.now(),
            "actor": actor.to_dict(),
        }
    )


@agent_bp.get("/workspace")
@auth_required
def workspace() -> Response:
    """首屏聚合：默认项目、连接状态、能力、工具与手册数量。"""
    actor = _current()
    project = permissions.ensure_default_project(actor)
    conn_view = connections.get_connection_view(actor)
    try:
        caps = connections.capabilities(actor)
    except AgentError:
        caps = {"configured": False}
    specs = get_registry().specs(actor, project["id"])
    return _ok(
        {
            "version": __version__,
            "project": project,
            "projects": permissions.list_projects(actor),
            "connection": conn_view,
            "capabilities": caps,
            "tool_count": len(specs),
            "manual_count": len(manual_store.list_manuals(actor)),
            "sessions": session_store.list_sessions(actor, project["id"], limit=10),
        }
    )


# --------------------------------------------------------------------------- #
# B-01 项目与资源
# --------------------------------------------------------------------------- #
@agent_bp.get("/projects")
@auth_required
def list_projects() -> Response:
    actor = _current()
    permissions.ensure_default_project(actor)
    return _ok(permissions.list_projects(actor))


@agent_bp.post("/projects")
@auth_required
def create_project() -> Response:
    actor = _current()
    body = _body()
    project = permissions.create_project(
        actor,
        _str_field(body, "name", max_chars=80),
        _str_field(body, "description", required=False, max_chars=2000),
    )
    return _ok(project, "项目已创建")


@agent_bp.get("/projects/<project_id>")
@auth_required
def get_project(project_id: str) -> Response:
    actor = _current()
    return _ok(permissions.get_project(actor, project_id))


@agent_bp.patch("/projects/<project_id>")
@auth_required
def update_project(project_id: str) -> Response:
    actor = _current()
    body = _body()
    requirements = body.get("requirements")
    if requirements is not None and not isinstance(requirements, dict):
        raise AgentError(Code.VALIDATION, "requirements 必须是 JSON 对象")
    project = permissions.update_project(
        actor,
        project_id,
        name=_str_field(body, "name", required=False, max_chars=80) or None,
        description=(
            _str_field(body, "description", required=False, max_chars=2000) or None
        ),
        requirements=requirements,
    )
    return _ok(project, "项目已更新")


@agent_bp.get("/projects/<project_id>/resources")
@auth_required
def list_resources(project_id: str) -> Response:
    actor = _current()
    kinds_raw = request.args.get("kinds", "")
    kinds = [item.strip() for item in kinds_raw.split(",") if item.strip()]
    return _ok(
        permissions.list_project_resources(actor, project_id, kinds=kinds or None)
    )


@agent_bp.post("/projects/<project_id>/resources")
@auth_required
def register_resource(project_id: str) -> Response:
    actor = _current()
    body = _body()
    metadata = body.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise AgentError(Code.VALIDATION, "metadata 必须是 JSON 对象")
    if body.get("source_table") or body.get("source_id"):
        # 业务来源只能由服务端在业务成功后登记，客户端不能自己认领别人的业务行
        raise AgentError(
            Code.FORBIDDEN,
            "业务来源由服务端登记，请求不能自报 source_table/source_id",
            http_status=403,
        )
    resource = permissions.register_resource(
        actor,
        project_id=project_id,
        kind=_str_field(body, "kind", max_chars=40),
        display_name=_str_field(body, "display_name", max_chars=200),
        source_table=None,
        source_id=None,
        access_policy=_str_field(
            body, "access_policy", required=False, default="private"
        ),
        modality=_str_field(body, "modality", required=False) or None,
        metadata=metadata,
        owner_id=None,  # 身份只取服务端，忽略请求体里的 owner_id
    )
    return _ok(resource, "资源已登记")


# --------------------------------------------------------------------------- #
# B-02 个人大模型连接
# --------------------------------------------------------------------------- #
@agent_bp.get("/connection")
@auth_required
def get_connection() -> Response:
    actor = _current()
    view = connections.get_connection_view(actor)
    return _ok(view or {"configured": False})


@agent_bp.put("/connection")
@auth_required
def save_connection() -> Response:
    actor = _current()
    body = _body()
    view = connections.save_connection(
        actor,
        provider=_str_field(body, "provider", required=False, default="deepseek"),
        api_url=_str_field(
            body, "api_url", required=False, default="https://api.deepseek.com"
        ),
        model=_str_field(body, "model", required=False, default="deepseek-chat"),
        api_key=_str_field(body, "api_key", required=False, max_chars=400) or None,
    )
    return _ok(view, "连接已保存")


@agent_bp.delete("/connection")
@auth_required
def delete_connection() -> Response:
    actor = _current()
    removed = connections.delete_connection(actor)
    if not removed:
        raise AgentError(Code.NOT_FOUND, "当前账号没有已保存的连接", http_status=404)
    return _ok({"configured": False}, "连接已删除")


@agent_bp.post("/connection/check")
@auth_required
def check_connection() -> Response:
    actor = _current()
    return _ok(connections.check_connection(actor))


@agent_bp.post("/connection/import-legacy")
@auth_required
def import_legacy_connection() -> Response:
    """把本机 ``llm_config.local.json`` 里的一次性迁移到本人加密连接。"""
    actor = _current()
    return _ok(connections.import_legacy_local_connection(actor))


@agent_bp.get("/capabilities")
@auth_required
def get_capabilities() -> Response:
    actor = _current()
    return _ok(connections.capabilities(actor))


@agent_bp.put("/credentials/<namespace>/<name>")
@auth_required
def put_credential(namespace: str, name: str) -> Response:
    """外部工具/MCP 的凭据加密保存；任何接口都不回读明文。"""
    actor = _current()
    body = _body()
    record = connections.store_credential(
        actor, namespace, name, _str_field(body, "value", max_chars=4000)
    )
    return _ok(record, "凭据已保存")


@agent_bp.delete("/credentials/<namespace>/<name>")
@auth_required
def remove_credential(namespace: str, name: str) -> Response:
    actor = _current()
    if not connections.delete_credential(actor, namespace, name):
        raise AgentError(Code.NOT_FOUND, "凭据不存在", http_status=404)
    return _ok({"deleted": True}, "凭据已删除")


# --------------------------------------------------------------------------- #
# B-03 会话 / 消息 / 事件
# --------------------------------------------------------------------------- #
@agent_bp.post("/sessions")
@auth_required
def create_session() -> Response:
    actor = _current()
    body = _body()
    project_id = _str_field(body, "project_id", required=False)
    if not project_id:
        project_id = permissions.ensure_default_project(actor)["id"]
    session = session_store.create_session(
        actor,
        project_id,
        _str_field(body, "title", required=False, max_chars=120),
        manual_id=_str_field(body, "manual_id", required=False) or None,
        manual_version=_optional_int(body.get("manual_version"), "manual_version"),
    )
    return _ok(session, "会话已创建")


@agent_bp.get("/sessions")
@auth_required
def list_sessions() -> Response:
    actor = _current()
    return _ok(
        session_store.list_sessions(
            actor,
            request.args.get("project_id") or None,
            limit=_int_arg("limit", 50, minimum=1, maximum=200),
        )
    )


@agent_bp.get("/sessions/<session_id>")
@auth_required
def get_session(session_id: str) -> Response:
    actor = _current()
    session = session_store.get_session(actor, session_id)
    active = task_store.find_active_task(actor, session_id)
    return _ok({"session": session, "active_task": active})


@agent_bp.get("/sessions/<session_id>/messages")
@auth_required
def list_messages(session_id: str) -> Response:
    actor = _current()
    session_store.get_session(actor, session_id)
    limit_raw = request.args.get("limit")
    limit = _int_arg("limit", 0, minimum=0, maximum=500) if limit_raw else None
    return _ok(session_store.list_messages(session_id, limit=limit))


@agent_bp.post("/sessions/<session_id>/messages")
@auth_required
def post_message(session_id: str) -> Response:
    """一轮对话：模型规划 → 工具调用 → （必要时）确认卡 → 回复。"""
    actor = _current()
    body = _body()
    refs = body.get("resource_refs") or []
    if not isinstance(refs, list):
        raise AgentError(Code.VALIDATION, "resource_refs 必须是数组")
    result = get_runtime().handle_message(
        actor,
        session_id,
        _str_field(body, "text", max_chars=8000),
        resource_refs=refs,
    )
    return _ok(result)


@agent_bp.get("/sessions/<session_id>/events")
@auth_required
def list_events(session_id: str) -> Response:
    """断线续取：``after`` 为客户端已收到的最大序号。"""
    actor = _current()
    session_store.get_session(actor, session_id)
    return _ok(
        {
            "events": task_store.list_events(
                session_id,
                after_sequence=_int_arg("after", 0, maximum=10_000_000),
                limit=_int_arg("limit", 200, minimum=1, maximum=500),
            ),
            "latest_sequence": task_store.latest_sequence(session_id),
        }
    )


@agent_bp.get("/sessions/<session_id>/events/stream")
@auth_required
def stream_events(session_id: str) -> Response:
    """SSE 推送；每帧带 ``sequence``，客户端按序号去重。"""
    actor = _current()
    session_store.get_session(actor, session_id)
    cursor = _int_arg("after", 0, maximum=10_000_000)
    max_seconds = min(
        _STREAM_MAX_SECONDS, _int_arg("max_seconds", 25, minimum=1, maximum=60)
    )

    def generate():
        position = cursor
        deadline = time.monotonic() + max_seconds
        idle_ticks = 0
        yield "retry: 3000\n\n"
        while time.monotonic() < deadline:
            try:
                events = task_store.list_events(
                    session_id, after_sequence=position, limit=200
                )
            except Exception as exc:  # noqa: BLE001 - 流内异常要显式告知前端
                payload = {"code": Code.NOT_IMPLEMENTED, "message": str(exc)[:200]}
                yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                return
            if events:
                idle_ticks = 0
                for event in events:
                    position = int(event["sequence"])
                    yield (
                        "event: agent\n"
                        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    )
            else:
                idle_ticks += 1
                if idle_ticks % _STREAM_IDLE_TICKS == 0:
                    yield ": keep-alive\n\n"
            time.sleep(_STREAM_TICK)
        yield f"event: done\ndata: {json.dumps({'sequence': position})}\n\n"

    response = Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
    )
    response.headers["Cache-Control"] = "no-cache, no-transform"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response


# --------------------------------------------------------------------------- #
# B-03 / B-06 任务
# --------------------------------------------------------------------------- #
@agent_bp.get("/tasks")
@auth_required
def list_tasks() -> Response:
    actor = _current()
    statuses_raw = request.args.get("statuses", "")
    statuses = [item.strip() for item in statuses_raw.split(",") if item.strip()]
    return _ok(
        task_store.list_tasks(
            actor,
            request.args.get("project_id") or None,
            statuses=statuses or None,
            limit=_int_arg("limit", 50, minimum=1, maximum=200),
        )
    )


@agent_bp.get("/tasks/<task_id>")
@auth_required
def get_task(task_id: str) -> Response:
    actor = _current()
    task = task_store.get_task(actor, task_id)
    return _ok(
        {
            "task": task_store.snapshot(task),
            "actions": action_store.list_actions(task_id),
            "operations": operation_store.list_for_task(task_id),
            "note": memory_store.get_note(task_id),
        }
    )


@agent_bp.post("/tasks/<task_id>/plan/confirm")
@auth_required
def confirm_plan(task_id: str) -> Response:
    actor = _current()
    return _ok(get_runtime().confirm_plan(actor, task_id))


@agent_bp.post("/tasks/<task_id>/stop")
@auth_required
def stop_task(task_id: str) -> Response:
    actor = _current()
    body = _body()
    reason = _str_field(body, "reason", required=False, max_chars=200)
    return _ok(get_runtime().stop_task(actor, task_id, reason=reason or "用户要求停止"))


@agent_bp.post("/tasks/<task_id>/reconcile")
@auth_required
def reconcile_task(task_id: str) -> Response:
    """核实等待中的业务状态；拿不到可信状态时如实标记待核实。"""
    actor = _current()
    body = _body()
    return _ok(
        recovery.reconcile_task(
            actor,
            task_id,
            auto_continue=bool(body.get("auto_continue", True)),
        )
    )


@agent_bp.get("/tasks/<task_id>/note")
@auth_required
def get_note(task_id: str) -> Response:
    actor = _current()
    return _ok(memory_store.ensure_note(actor, task_id))


@agent_bp.post("/tasks/<task_id>/note/errors")
@auth_required
def flag_note_error(task_id: str) -> Response:
    actor = _current()
    body = _body()
    note = memory_store.flag_error(
        actor, task_id, _str_field(body, "description", max_chars=1000)
    )
    return _ok(note, "已记录纠错")


# --------------------------------------------------------------------------- #
# B-05 动作确认
# --------------------------------------------------------------------------- #
@agent_bp.get("/actions/<action_id>")
@auth_required
def get_action(action_id: str) -> Response:
    actor = _current()
    return _ok(action_store.get_action(actor, action_id))


@agent_bp.post("/actions/<action_id>/confirm")
@auth_required
def confirm_action(action_id: str) -> Response:
    actor = _current()
    body = _body()
    return _ok(
        get_runtime().confirm_action(
            actor,
            action_id,
            expected_revision=_optional_int(
                body.get("expected_revision"), "expected_revision"
            ),
        )
    )


@agent_bp.post("/actions/<action_id>/reject")
@auth_required
def reject_action(action_id: str) -> Response:
    actor = _current()
    body = _body()
    reason = _str_field(body, "reason", required=False, max_chars=500)
    return _ok(get_runtime().reject_action(actor, action_id, reason or ""))


# --------------------------------------------------------------------------- #
# B-08 任务手册
# --------------------------------------------------------------------------- #
@agent_bp.get("/manuals")
@auth_required
def list_manuals() -> Response:
    actor = _current()
    return _ok(manual_store.list_manuals(actor))


@agent_bp.get("/manuals/<manual_id>")
@auth_required
def get_manual(manual_id: str) -> Response:
    actor = _current()
    return _ok(manual_store.get_manual(actor, manual_id))


@agent_bp.post("/manuals")
@auth_required
def create_manual() -> Response:
    actor = _current()
    body = _body()
    manual = manual_store.create_user_manual(
        actor,
        name=_str_field(body, "name", max_chars=120),
        content_md=_str_field(body, "content_md", max_chars=20000),
        summary=_str_field(body, "summary", required=False, max_chars=500),
        keywords=_str_field(body, "keywords", required=False, max_chars=300),
    )
    return _ok(manual, "手册已创建")


# --------------------------------------------------------------------------- #
# B-09 外部工具授权
# --------------------------------------------------------------------------- #
@agent_bp.get("/grants")
@auth_required
def list_grants() -> Response:
    actor = _current()
    return _ok(grant_store.list_grants(actor))


@agent_bp.post("/grants")
@auth_required
def create_grant() -> Response:
    """放行"某个连接里的某个具体只读工具"，绑定工具版本，版本变化自动失效。"""
    actor = _current()
    body = _body()
    grant = grant_store.grant_readonly(
        actor,
        _str_field(body, "server_id", max_chars=120),
        _str_field(body, "tool_name", max_chars=120),
        _str_field(body, "tool_version", required=False, default="1") or "1",
        project_id=_str_field(body, "project_id", required=False) or None,
    )
    return _ok(grant, "已放行该只读工具")


@agent_bp.delete("/grants/<grant_id>")
@auth_required
def revoke_grant(grant_id: str) -> Response:
    actor = _current()
    if not grant_store.revoke_grant(actor, grant_id):
        raise AgentError(Code.NOT_FOUND, "授权记录不存在", http_status=404)
    return _ok({"revoked": True}, "授权已撤销")


# --------------------------------------------------------------------------- #
# B-05 工具清单
# --------------------------------------------------------------------------- #
@agent_bp.get("/tools")
@auth_required
def list_tools() -> Response:
    """前端展示"当前能用什么"，与模型看到的清单来自同一处。"""
    actor = _current()
    project_id = request.args.get("project_id") or None
    specs = get_registry().specs(actor, project_id)
    return _ok([spec.to_dict() for spec in specs])

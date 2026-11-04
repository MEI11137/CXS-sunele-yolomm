"""工具清单：模型能看到的全部能力，以及它们的参数 schema 与风险等级。

设计约束：

* 只有这里注册过的工具才可能被调用；引擎侧拿到的清单也来自这里，
  所以“工具不存在”与“无权使用”是同一个硬边界，不靠提示词约束。
* 目录授权、账号管理、人工复核结论、检测/训练都不在清单里 —— 模型无法“顺便”做这些事。
* 参数只能是真实 ID、真实路径与普通文本；没有 SQL、没有命令行、没有正则执行。
* 后续检测/复核/回流能力通过 :func:`register_service` 挂进来，
  本轮只登记“未接入”，不会伪装成可调用工具。
"""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable

from backend.agent import fsops, policy
from backend.agent.context import Actor, ToolError

MAX_TEXT_ARG = 200_000


@dataclasses.dataclass
class ToolContext:
    conn: sqlite3.Connection
    actor: Actor
    args: dict
    emit: Callable[[str, dict], None]
    jobs: dict[str, Callable[[str], dict]]


@dataclasses.dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str
    risk: str
    schema: dict
    handler: Callable[[ToolContext], dict]
    risk_for: Callable[[ToolContext], str] | None = None
    requires_root: bool = False
    summary: Callable[[dict], str] | None = None

    def effective_risk(self, ctx: ToolContext) -> str:
        if self.risk_for is not None:
            return self.risk_for(ctx)
        return self.risk

    def preview(self, args: dict) -> str:
        if self.summary is not None:
            try:
                return str(self.summary(args))[:300]
            except Exception:  # 摘要失败不影响主流程
                return ""
        return ""


TOOLS: dict[str, ToolSpec] = {}

# 未来能力：登记但标记为未接入，任何界面与模型提示都不得说成可用。
SERVICES: dict[str, dict] = {
    "detection": {"registered": False, "note": "检测能力在 WB17-03 接入，本轮不可调用"},
    "review": {"registered": False, "note": "人工复核在 WB17-04 接入，本轮不可调用"},
    "feedback": {"registered": False, "note": "数据回流在 WB17-04 接入，本轮不可调用"},
    "training": {"registered": False, "note": "训练评估在 WB17-05 接入，本轮不可调用"},
}


def register(spec: ToolSpec) -> ToolSpec:
    if spec.name in TOOLS:
        raise RuntimeError(f"工具重复注册：{spec.name}")
    TOOLS[spec.name] = spec
    return spec


def register_service(name: str, spec: dict) -> None:
    """给后续阶段挂业务工具用；不允许借这里另建第二套业务。"""
    if name not in SERVICES:
        raise RuntimeError(f"未知服务位：{name}")
    SERVICES[name] = {**spec, "registered": True}


def get(name: str) -> ToolSpec:
    spec = TOOLS.get(str(name or ""))
    if spec is None:
        raise ToolError("TOOL_NOT_FOUND", f"没有这个工具：{name}（不会调用任何未注册能力）", status=404)
    return spec


def catalogue() -> list[dict]:
    """给引擎/MCP 的工具清单：只暴露名称、说明与入参 schema。"""
    return [
        {
            "name": spec.name,
            "title": spec.title,
            "description": spec.description,
            "inputSchema": spec.schema,
        }
        for spec in TOOLS.values()
    ]


# --------------------------------------------------------------------------- 校验


_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


def validate_args(schema: dict, args: Any) -> dict:
    """按 schema 校验入参；未知字段与类型不符一律拒绝，不做“猜测式”纠正。"""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("BAD_ARGS", "工具参数必须是一个 JSON 对象")
    properties: dict = schema.get("properties") or {}
    required: list = schema.get("required") or []
    cleaned: dict = {}

    unknown = [key for key in args if key not in properties]
    if unknown:
        raise ToolError("UNKNOWN_ARGUMENT", "不认识的参数：" + "、".join(sorted(unknown)[:5]))
    for key in required:
        if key not in args or args[key] is None or args[key] == "":
            raise ToolError("MISSING_ARGUMENT", f"缺少必填参数：{key}")

    for key, value in args.items():
        rule = properties[key]
        expected = rule.get("type")
        if value is None:
            continue
        if expected in _TYPES and not isinstance(value, _TYPES[expected]):
            raise ToolError("BAD_ARGUMENT_TYPE", f"参数 {key} 类型应为 {expected}")
        if expected == "integer" and isinstance(value, bool):
            raise ToolError("BAD_ARGUMENT_TYPE", f"参数 {key} 类型应为 integer")
        if expected == "string":
            text = str(value)
            if len(text) > MAX_TEXT_ARG:
                raise ToolError("ARGUMENT_TOO_LARGE", f"参数 {key} 过长")
            pattern = rule.get("pattern")
            if pattern and not re.match(pattern, text):
                raise ToolError("BAD_ARGUMENT_FORMAT", f"参数 {key} 格式不正确")
            if "maxLength" in rule and len(text) > int(rule["maxLength"]):
                raise ToolError("ARGUMENT_TOO_LARGE", f"参数 {key} 超过长度上限")
        if expected in {"integer", "number"}:
            minimum, maximum = rule.get("minimum"), rule.get("maximum")
            if minimum is not None and value < minimum:
                raise ToolError("ARGUMENT_OUT_OF_RANGE", f"参数 {key} 小于下限 {minimum}")
            if maximum is not None and value > maximum:
                raise ToolError("ARGUMENT_OUT_OF_RANGE", f"参数 {key} 大于上限 {maximum}")
        if expected == "array":
            if "maxItems" in rule and len(value) > int(rule["maxItems"]):
                raise ToolError("ARGUMENT_TOO_LARGE", f"参数 {key} 元素过多")
        cleaned[key] = value
    return cleaned


# --------------------------------------------------------------------------- 工具


def _root(ctx: ToolContext) -> Path:
    return fsops.require_root(ctx.actor.root_path)


def _project_row(ctx: ToolContext) -> sqlite3.Row:
    row = ctx.conn.execute(
        "SELECT * FROM projects WHERE id = ? AND owner_id = ?",
        (ctx.actor.project_id, int(ctx.actor.user_id)),
    ).fetchone()
    if row is None:
        raise ToolError("PROJECT_NOT_FOUND", "当前项目不存在或不属于你", status=404)
    return row


def _session_row(ctx: ToolContext, session_id: str) -> sqlite3.Row:
    row = ctx.conn.execute(
        "SELECT * FROM sessions WHERE id = ? AND owner_id = ? AND project_id = ?",
        (str(session_id), int(ctx.actor.user_id), ctx.actor.project_id),
    ).fetchone()
    if row is None:
        raise ToolError("SESSION_NOT_FOUND", "该会话不存在、不属于你或不在当前项目", status=404)
    return row


# ---- 概况 / 项目管理 -------------------------------------------------------


def _project_overview(ctx: ToolContext) -> dict:
    project = _project_row(ctx)
    root = project["root_path"]
    summary: dict[str, Any] = {
        "project": {
            "id": project["id"],
            "name": project["name"],
            "description": project["description"],
            "status": project["status"],
        },
        "directory": {"bound": bool(root), "path": root},
        "mode": ctx.actor.mode,
        "model": {"provider": ctx.actor.provider, "model": ctx.actor.model},
        "services": {
            name: ("ready" if info.get("registered") else "not_available")
            for name, info in SERVICES.items()
        },
        "notes": [
            "目录授权由管理员或本人设置，模型不能修改",
            "人工复核结论必须由本人在界面提交",
        ],
    }
    if root:
        try:
            base = fsops.resolve(Path(root), ".", must_exist=True)
            listing = fsops.list_dir(base, ".", limit=20)
            summary["directory"]["top_entries"] = [
                {"name": item["name"], "is_dir": item["is_dir"], "size": item["size"]}
                for item in listing["entries"]
            ]
            summary["directory"]["truncated"] = listing["truncated"]
        except ToolError as error:
            summary["directory"]["error"] = error.message
    sessions = ctx.conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE project_id = ? AND owner_id = ? AND status = 'active'",
        (ctx.actor.project_id, int(ctx.actor.user_id)),
    ).fetchone()[0]
    summary["sessions"] = {"active": int(sessions), "current": ctx.actor.session_id}
    return summary


def _list_projects(ctx: ToolContext) -> dict:
    rows = ctx.conn.execute(
        "SELECT id, name, description, root_path, status, created_at FROM projects "
        "WHERE owner_id = ? AND status = 'active' ORDER BY created_at DESC",
        (int(ctx.actor.user_id),),
    ).fetchall()
    return {
        "projects": [
            {
                "id": row["id"],
                "name": row["name"],
                "has_directory": bool(row["root_path"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    }


def _create_project(ctx: ToolContext) -> dict:
    from backend.services import projects as project_service

    name = str(ctx.args.get("name") or "").strip()
    created = project_service.create(
        ctx.conn, int(ctx.actor.user_id), name=name, description=str(ctx.args.get("description") or "")
    )
    ctx.emit("projects.changed", {"action": "created", "project_id": created["id"]})
    return {"project": {"id": created["id"], "name": created["name"], "directory_bound": False}}


def _rename_project(ctx: ToolContext) -> dict:
    from backend.services import projects as project_service

    project = _project_row(ctx)
    updated = project_service.update(ctx.conn, project, name=str(ctx.args.get("name") or ""))
    return {"project": {"id": updated["id"], "name": updated["name"]}}


def _list_sessions(ctx: ToolContext) -> dict:
    rows = ctx.conn.execute(
        "SELECT id, title, status, updated_at FROM sessions "
        "WHERE project_id = ? AND owner_id = ? AND status = 'active' ORDER BY updated_at DESC LIMIT 50",
        (ctx.actor.project_id, int(ctx.actor.user_id)),
    ).fetchall()
    return {
        "sessions": [
            {
                "id": row["id"],
                "title": row["title"],
                "updated_at": row["updated_at"],
                "current": row["id"] == ctx.actor.session_id,
            }
            for row in rows
        ]
    }


def _create_session(ctx: ToolContext) -> dict:
    from backend.services import sessions as session_service

    title = str(ctx.args.get("title") or "").strip() or "新会话"
    created = session_service.create(
        ctx.conn, project_id=ctx.actor.project_id, owner_id=int(ctx.actor.user_id), title=title
    )
    ctx.emit("sessions.changed", {"action": "created", "session_id": created["id"]})
    return {"session": {"id": created["id"], "title": created["title"]}}


def _rename_session(ctx: ToolContext) -> dict:
    from backend.services import sessions as session_service

    session = _session_row(ctx, str(ctx.args.get("session_id") or ""))
    updated = session_service.rename(ctx.conn, session, str(ctx.args.get("title") or ""))
    ctx.emit("sessions.changed", {"action": "renamed", "session_id": updated["id"]})
    return {"session": {"id": updated["id"], "title": updated["title"]}}


# ---- 文件 -----------------------------------------------------------------


def _list_files(ctx: ToolContext) -> dict:
    root = _root(ctx)
    return fsops.list_dir(
        root,
        ctx.args.get("path") or ".",
        limit=int(ctx.args.get("limit") or 100),
        cursor=str(ctx.args.get("cursor") or ""),
    )


def _search_files(ctx: ToolContext) -> dict:
    root = _root(ctx)
    return fsops.search(
        root,
        ctx.args.get("query"),
        start=ctx.args.get("path") or ".",
        limit=int(ctx.args.get("limit") or 30),
    )


def _read_file(ctx: ToolContext) -> dict:
    root = _root(ctx)
    return fsops.read_file(
        root,
        ctx.args.get("path"),
        offset=int(ctx.args.get("offset") or 0),
        limit=ctx.args.get("limit"),
    )


def _write_risk(ctx: ToolContext) -> str:
    """改成已存在的文件属于“改”，风险高于新建。"""
    try:
        root = _root(ctx)
        target = fsops.resolve(root, ctx.args.get("path"))
        return policy.RISK_DESTRUCTIVE if target.exists() else policy.RISK_WRITE
    except ToolError:
        return policy.RISK_WRITE


def _write_file(ctx: ToolContext) -> dict:
    root = _root(ctx)
    return fsops.write_file(
        root,
        ctx.args.get("path"),
        ctx.args.get("content"),
        mode=str(ctx.args.get("mode") or "auto"),
    )


def _move_path(ctx: ToolContext) -> dict:
    root = _root(ctx)
    return fsops.move_path(
        root,
        ctx.args.get("from"),
        ctx.args.get("to"),
        overwrite=bool(ctx.args.get("overwrite")),
    )


def _copy_path(ctx: ToolContext) -> dict:
    root = _root(ctx)
    return fsops.copy_path(
        root,
        ctx.args.get("from"),
        ctx.args.get("to"),
        overwrite=bool(ctx.args.get("overwrite")),
    )


def _delete_path(ctx: ToolContext) -> dict:
    root = _root(ctx)
    return fsops.delete_path(
        root,
        ctx.args.get("path"),
        recursive=bool(ctx.args.get("recursive")),
    )


# ---- 对话 / 界面 / 任务 ---------------------------------------------------


def _open_page(ctx: ToolContext) -> dict:
    page = str(ctx.args.get("page") or "").strip()
    routes = {
        "project": {"name": "project", "params": {"projectId": ctx.actor.project_id}},
        "session": {
            "name": "session",
            "params": {"projectId": ctx.actor.project_id, "sessionId": ctx.actor.session_id},
        },
        "settings": {"name": "settings", "params": {}},
        "trash": {"name": "trash", "params": {}},
    }
    if page not in routes:
        raise ToolError("BAD_PAGE", "只能打开项目、会话、设置或回收站页面")
    ctx.emit("ui.navigate", {"page": page, "route": routes[page]})
    return {"opened": page, "note": "已在用户界面发起跳转"}


def _ask_user(ctx: ToolContext) -> dict:
    question = str(ctx.args.get("question") or "").strip()
    if len(question) < 2:
        raise ToolError("QUESTION_TOO_SHORT", "问题太短，请写清楚需要用户补充什么")
    options = ctx.args.get("options") or []
    ctx.emit("question", {"question": question[:500], "options": [str(o)[:80] for o in options][:5]})
    return {
        "asked": True,
        "note": "问题已显示给用户。请结束本轮，等用户下一条消息再继续，不要重复提问。",
    }


def _tool_status(ctx: ToolContext) -> dict:
    job_id = str(ctx.args.get("job_id") or "").strip()
    if not job_id:
        raise ToolError("JOB_ID_REQUIRED", "需要 job_id")
    row = ctx.conn.execute(
        "SELECT * FROM tool_jobs WHERE id = ? AND owner_id = ?",
        (job_id, int(ctx.actor.user_id)),
    ).fetchone()
    if row is None:
        return {
            "job_id": job_id,
            "status": "unknown",
            "note": "没有这个任务记录。不要重复提交同一操作，先向用户确认。",
        }
    try:
        result = json.loads(row["result"] or "{}")
    except ValueError:
        result = {}
    return {
        "job_id": job_id,
        "status": row["status"],
        "tool": row["tool"],
        "result": result,
        "error": (
            {"code": row["error_code"], "message": row["error_message"]}
            if row["error_code"]
            else None
        ),
    }


def _connection_status(ctx: ToolContext) -> dict:
    row = ctx.conn.execute(
        "SELECT provider, model, capabilities, checked_at FROM connections WHERE user_id = ?",
        (int(ctx.actor.user_id),),
    ).fetchone()
    if row is None:
        return {"configured": False, "note": "本人尚未保存个人模型连接"}
    try:
        capabilities = json.loads(row["capabilities"] or "{}")
    except ValueError:
        capabilities = {}
    return {
        "configured": True,
        "provider": row["provider"],
        "model": row["model"],
        "capabilities": {
            key: bool(capabilities.get(key)) for key in ("auth", "chat", "vision", "tools")
        },
        "checked_at": row["checked_at"],
        "note": "能力来自本人点击“检查连接”的实测结论；未检查过不代表不可用。",
    }


# --------------------------------------------------------------------------- 注册


_PATH = {"type": "string", "maxLength": 1000}

register(
    ToolSpec(
        name="hx_project_overview",
        title="查看当前项目概况",
        description="返回当前项目、绑定目录概况、会话数、权限模式与可用服务。回答项目相关问题时先用它。",
        risk=policy.RISK_READ,
        schema={"type": "object", "properties": {}, "required": []},
        handler=_project_overview,
    )
)
register(
    ToolSpec(
        name="hx_list_projects",
        title="列出我的项目",
        description="列出当前账号的项目（ID 与名称）。只能用返回的真实 ID，不要编造。",
        risk=policy.RISK_READ,
        schema={"type": "object", "properties": {}, "required": []},
        handler=_list_projects,
    )
)
register(
    ToolSpec(
        name="hx_create_project",
        title="新建项目",
        description="为当前账号新建一个项目。目录绑定仍需用户本人在界面完成，工具不能绑定目录。",
        risk=policy.RISK_WRITE,
        schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": 80},
                "description": {"type": "string", "maxLength": 400},
            },
            "required": ["name"],
        },
        handler=_create_project,
        summary=lambda args: f"新建项目「{args.get('name', '')}」",
    )
)
register(
    ToolSpec(
        name="hx_rename_project",
        title="重命名当前项目",
        description="只改当前项目名称，不改目录绑定。",
        risk=policy.RISK_WRITE,
        schema={
            "type": "object",
            "properties": {"name": {"type": "string", "maxLength": 80}},
            "required": ["name"],
        },
        handler=_rename_project,
        summary=lambda args: f"项目改名为「{args.get('name', '')}」",
    )
)
register(
    ToolSpec(
        name="hx_list_sessions",
        title="列出当前项目会话",
        description="列出当前项目里本人的会话（ID、标题、是否当前）。",
        risk=policy.RISK_READ,
        schema={"type": "object", "properties": {}, "required": []},
        handler=_list_sessions,
    )
)
register(
    ToolSpec(
        name="hx_create_session",
        title="新建会话",
        description="在当前项目里新建一个会话。返回真实 session_id。",
        risk=policy.RISK_WRITE,
        schema={
            "type": "object",
            "properties": {"title": {"type": "string", "maxLength": 80}},
            "required": ["title"],
        },
        handler=_create_session,
        summary=lambda args: f"新建会话「{args.get('title', '')}」",
    )
)
register(
    ToolSpec(
        name="hx_rename_session",
        title="重命名会话",
        description="重命名当前项目里本人的某个会话；session_id 必须来自 hx_list_sessions。",
        risk=policy.RISK_WRITE,
        schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "pattern": r"^ses_[0-9a-f]{16}$"},
                "title": {"type": "string", "maxLength": 80},
            },
            "required": ["session_id", "title"],
        },
        handler=_rename_session,
        summary=lambda args: f"会话改名为「{args.get('title', '')}」",
    )
)

register(
    ToolSpec(
        name="hx_list_files",
        title="列出目录",
        description=(
            "列出当前项目绑定目录内的子项。path 可写相对路径或绝对路径，但必须落在绑定目录内。"
            "返回真实路径、大小与是否目录；条目按“目录在前、名称排序”稳定排列，"
            "用返回的 next_cursor 作为下一次的 cursor 继续翻页（不会漏项）。"
        ),
        risk=policy.RISK_READ,
        requires_root=True,
        schema={
            "type": "object",
            "properties": {
                "path": _PATH,
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "cursor": {"type": "string", "maxLength": 200},
            },
            "required": [],
        },
        handler=_list_files,
        summary=lambda args: f"列出目录 {args.get('path') or '.'}",
    )
)
register(
    ToolSpec(
        name="hx_search_files",
        title="按名称搜索文件",
        description="在绑定目录内按文件名搜索（不搜内容），返回相对路径。",
        risk=policy.RISK_READ,
        requires_root=True,
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 100},
                "path": _PATH,
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["query"],
        },
        handler=_search_files,
        summary=lambda args: f"搜索文件名「{args.get('query', '')}」",
    )
)
register(
    ToolSpec(
        name="hx_read_file",
        title="读取文本文件",
        description=(
            "读取绑定目录内的文本文件，按字节分页。二进制文件只返回元信息。"
            "大文件请先用小 limit 读开头，再按 next_offset 继续。"
        ),
        risk=policy.RISK_READ,
        requires_root=True,
        schema={
            "type": "object",
            "properties": {
                "path": _PATH,
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200000},
            },
            "required": ["path"],
        },
        handler=_read_file,
        summary=lambda args: f"读取 {args.get('path')}",
    )
)
register(
    ToolSpec(
        name="hx_write_file",
        title="写入文本文件",
        description=(
            "在绑定目录内新建或修改文本文件。mode=create 只新建、overwrite 只覆盖已有、"
            "append 追加、auto 自动判断。动作可能按权限模式要求用户确认。"
        ),
        risk=policy.RISK_WRITE,
        risk_for=_write_risk,
        requires_root=True,
        schema={
            "type": "object",
            "properties": {
                "path": _PATH,
                "content": {"type": "string", "maxLength": MAX_TEXT_ARG},
                "mode": {"type": "string", "pattern": "^(auto|create|overwrite|append)$"},
            },
            "required": ["path", "content"],
        },
        handler=_write_file,
        summary=lambda args: (
            f"写入 {args.get('path')}（{len(str(args.get('content') or '').encode('utf-8'))} 字节）"
        ),
    )
)
register(
    ToolSpec(
        name="hx_move_path",
        title="移动或重命名",
        description="在绑定目录内移动/重命名文件或目录；目标已存在时必须显式允许覆盖。",
        risk=policy.RISK_DESTRUCTIVE,
        requires_root=True,
        schema={
            "type": "object",
            "properties": {
                "from": _PATH,
                "to": _PATH,
                "overwrite": {"type": "boolean"},
            },
            "required": ["from", "to"],
        },
        handler=_move_path,
        summary=lambda args: f"移动 {args.get('from')} → {args.get('to')}",
    )
)
register(
    ToolSpec(
        name="hx_copy_path",
        title="复制文件或目录",
        description=(
            "在绑定目录内复制文件/目录。to 必须是完整的目标文件名（不会自动放进同名目录）。"
            "目标已存在时必须显式允许覆盖，且目录不会被递归替换。"
        ),
        risk=policy.RISK_WRITE,
        requires_root=True,
        schema={
            "type": "object",
            "properties": {
                "from": _PATH,
                "to": _PATH,
                "overwrite": {"type": "boolean"},
            },
            "required": ["from", "to"],
        },
        handler=_copy_path,
        summary=lambda args: f"复制 {args.get('from')} → {args.get('to')}",
    )
)
register(
    ToolSpec(
        name="hx_delete_path",
        title="删除文件或目录",
        description=(
            "把绑定目录内的文件/目录移入本项目下的 .hx17-trash 回收区（附清单，可人工找回），"
            "不是不可逆删除。删除目录需 recursive。"
        ),
        risk=policy.RISK_DESTRUCTIVE,
        requires_root=True,
        schema={
            "type": "object",
            "properties": {
                "path": _PATH,
                "recursive": {"type": "boolean"},
            },
            "required": ["path"],
        },
        handler=_delete_path,
        summary=lambda args: f"删除 {args.get('path')}（可回收）",
    )
)

register(
    ToolSpec(
        name="hx_open_page",
        title="在界面上打开页面",
        description="让用户界面跳到项目、当前会话、设置或回收站。只能跳这四处。",
        risk=policy.RISK_READ,
        schema={
            "type": "object",
            "properties": {"page": {"type": "string", "pattern": "^(project|session|settings|trash)$"}},
            "required": ["page"],
        },
        handler=_open_page,
    )
)
register(
    ToolSpec(
        name="hx_ask_user",
        title="向用户提问",
        description=(
            "缺少必要信息时用对话询问用户。问完就结束本轮等用户回答，不要自己编造答案。"
            "问题要具体，必要时给 2-4 个选项。"
        ),
        risk=policy.RISK_READ,
        schema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "maxLength": 500},
                "options": {"type": "array", "maxItems": 5},
            },
            "required": ["question"],
        },
        handler=_ask_user,
    )
)
register(
    ToolSpec(
        name="hx_tool_status",
        title="查询工具任务状态",
        description=(
            "按 job_id 查询长任务/上次调用的结果。拿到 unknown 时说明该任务没有记录，"
            "不要重复提交同一操作。"
        ),
        risk=policy.RISK_READ,
        schema={
            "type": "object",
            "properties": {"job_id": {"type": "string", "maxLength": 120}},
            "required": ["job_id"],
        },
        handler=_tool_status,
    )
)
register(
    ToolSpec(
        name="hx_connection_status",
        title="查看个人模型连接状态",
        description="返回本人个人模型连接的 provider/model 与上次能力检查结论（不含任何密钥）。",
        risk=policy.RISK_READ,
        schema={"type": "object", "properties": {}, "required": []},
        handler=_connection_status,
    )
)

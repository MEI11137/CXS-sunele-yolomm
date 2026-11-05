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
    "detection": {"registered": True, "note": "真实 YOLO/YOLOMM 推理，结果写入识别记录"},
    "ai_review": {"registered": True, "note": "用本人当前连接做看图核查，结论仅供参考"},
    "datasets": {"registered": True, "note": "ZIP/目录导入，逐样本校验后登记"},
    "models": {"registered": True, "note": "PT 导入与项目内发布，类别映射显式确认"},
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
        "records": {"name": "records", "params": {"projectId": ctx.actor.project_id}},
        "datasets": {"name": "datasets", "params": {"projectId": ctx.actor.project_id}},
        "models": {"name": "models", "params": {"projectId": ctx.actor.project_id}},
        "assets": {"name": "assets", "params": {"projectId": ctx.actor.project_id}},
        "settings": {"name": "settings", "params": {}},
        "trash": {"name": "trash", "params": {}},
    }
    if page not in routes:
        raise ToolError("BAD_PAGE", "只能打开项目、会话、记录、数据集、模型、资产、设置或回收站页面")
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
            "properties": {
                "page": {
                    "type": "string",
                    "pattern": "^(project|session|records|datasets|models|assets|settings|trash)$",
                }
            },
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


# ---- 业务：资产 / 数据集 / 模型 / 检测 / 核查 -------------------------------


def _service(call: Callable[[], Any]) -> Any:
    """调用业务服务并把它抛的业务错误翻成工具错误。

    业务服务与工具层是**同一套**实现（网页与 Agent 共用），但两边错误类型不同：
    服务抛 ``ApiError``、工具层认 ``ToolError``。这里保留机器码、中文说明与结构化 details，
    既不吞错也不另造一套文案——否则模型会把“类别没对上”看成“工具坏了”。
    """
    from backend.errors import ApiError

    try:
        return call()
    except ApiError as error:
        raise ToolError(
            error.code,
            error.msg,
            status=error.status,
            details=error.details,
        ) from error


def _emit_job(ctx: ToolContext, job: dict, *, page: str, note: str = "") -> None:
    ctx.emit(
        "business.job",
        {
            "job_id": job["id"],
            "kind": job["kind"],
            "status": job["status"],
            "page": page,
            "note": note,
        },
    )


def _list_datasets(ctx: ToolContext) -> dict:
    from backend.services import datasets as dataset_service

    return _service(
        lambda: dataset_service.list_for_project(
            ctx.conn, int(ctx.actor.user_id), ctx.actor.project_id
        )
    )


def _import_dataset(ctx: ToolContext) -> dict:
    from backend.services import datasets as dataset_service

    dataset = _service(
        lambda: dataset_service.import_dataset(
            ctx.conn,
            int(ctx.actor.user_id),
            ctx.actor.project_id,
            name=str(ctx.args.get("name") or ""),
            modalities=[str(item) for item in (ctx.args.get("modalities") or ["rgb"])],
            secondary_modality=ctx.args.get("secondary_modality"),
            classes=ctx.args.get("classes"),
            asset_id=ctx.args.get("asset_id"),
            rel_path=ctx.args.get("rel_path"),
        )
    )
    ctx.emit("datasets.changed", {"action": "imported", "dataset_id": dataset["id"]})
    return {"dataset": dataset}


def _list_models(ctx: ToolContext) -> dict:
    from backend.services import models as model_service

    return _service(
        lambda: model_service.list_for_project(
            ctx.conn, int(ctx.actor.user_id), ctx.actor.project_id
        )
    )


def _import_model(ctx: ToolContext) -> dict:
    from backend.services import models as model_service

    model = _service(
        lambda: model_service.import_model(
            ctx.conn,
            int(ctx.actor.user_id),
            ctx.actor.project_id,
            name=str(ctx.args.get("name") or ""),
            family=str(ctx.args.get("family") or ""),
            model_mode=ctx.args.get("model_mode"),
            secondary_modality=ctx.args.get("secondary_modality"),
            dataset_id=ctx.args.get("dataset_id"),
            class_map=ctx.args.get("class_map"),
            asset_id=ctx.args.get("asset_id"),
            rel_path=ctx.args.get("rel_path"),
            notes=str(ctx.args.get("notes") or ""),
        )
    )
    ctx.emit("models.changed", {"action": "imported", "model_id": model["id"]})
    return {
        "model": model,
        "note": (
            "导入完成但未发布。类别映射已经落在模型上；如果用户已明确要发布，直接调 hx_publish_model，"
            "不要为“再确认一次”额外开一轮对话。缺映射时会返回 NEED_CLASS_MAPPING，那时再问用户要映射。"
            if model["status"] != "published"
            else ""
        ),
    }


def _publish_model(ctx: ToolContext) -> dict:
    from backend.services import models as model_service

    model = _service(
        lambda: model_service.publish(
            ctx.conn,
            int(ctx.actor.user_id),
            ctx.actor.project_id,
            str(ctx.args.get("model_id") or ""),
        )
    )
    ctx.emit("models.changed", {"action": "published", "model_id": model["id"]})
    return {"model": model}


def _register_asset(ctx: ToolContext) -> dict:
    from backend.services import assets as asset_service

    asset = _service(
        lambda: asset_service.register_local(
            ctx.conn,
            int(ctx.actor.user_id),
            ctx.actor.project_id,
            str(ctx.args.get("rel_path") or ""),
            role=ctx.args.get("role"),
        )
    )
    return {"asset": asset}


def _detect_images(ctx: ToolContext) -> dict:
    from backend.services import pipelines

    rgb_ids = ctx.args.get("rgb_asset_ids") or []
    if not rgb_ids:
        raise ToolError("NO_IMAGES", "需要 rgb_asset_ids（先用 hx_list_files 或 hx_register_asset 拿到）")
    x_ids = ctx.args.get("x_asset_ids") or []
    items = [
        {"rgb_asset_id": str(asset_id), "x_asset_id": (str(x_ids[index]) if index < len(x_ids) else None)}
        for index, asset_id in enumerate(rgb_ids)
    ]
    job, created = _service(
        lambda: pipelines.start_detection_batch(
            ctx.conn,
            int(ctx.actor.user_id),
            ctx.actor.project_id,
            model_id=str(ctx.args.get("model_id") or ""),
            items=items,
            request_key=str(ctx.args.get("request_key") or ""),
            confidence=float(ctx.args.get("confidence") or 0.25),
            session_id=ctx.actor.session_id,
            turn_id=ctx.actor.turn_id,
        )
    )
    _emit_job(ctx, job, page="records")
    return {
        "job": job,
        "created": created,
        "note": "检测在后台进行；用 hx_job_status 查询（每次等待不超过 10 秒），不要重复提交同一请求键。",
    }


def _inspect_directory(ctx: ToolContext) -> dict:
    from backend.services import pipelines

    job, created = _service(
        lambda: pipelines.start_inspection(
            ctx.conn,
            int(ctx.actor.user_id),
            ctx.actor.project_id,
            model_id=str(ctx.args.get("model_id") or ""),
            directory=str(ctx.args.get("directory") or "."),
            request_key=str(ctx.args.get("request_key") or ""),
            confidence=float(ctx.args.get("confidence") or 0.25),
            limit=int(ctx.args.get("limit") or 20),
            review=bool(ctx.args.get("review")),
            x_directory=ctx.args.get("x_directory"),
            session_id=ctx.actor.session_id,
            turn_id=ctx.actor.turn_id,
            # 本轮真正下发给引擎的模型：核查阶段沿用它，不按偏好重新解析。
            review_model=ctx.actor.model,
        )
    )
    _emit_job(ctx, job, page="records", note="巡检作业已启动")
    return {
        "job": job,
        "created": created,
        "note": (
            "这是服务端的确定性巡检流程（发现→检测→按需核查→生成人工队列），不是另起一个规划器。"
            "用 hx_job_status 查询进度；不要在对话里重复发起。"
        ),
    }


def _job_status(ctx: ToolContext) -> dict:
    import time as _time

    from backend.services import jobs as job_service

    job_id = str(ctx.args.get("job_id") or "").strip()
    if not job_id:
        raise ToolError("JOB_ID_REQUIRED", "需要 job_id")
    wait_seconds = min(float(ctx.args.get("wait_seconds") or 0), 10.0)
    deadline = _time.monotonic() + wait_seconds
    while True:
        try:
            row = job_service.get_owned(ctx.conn, int(ctx.actor.user_id), ctx.actor.project_id, job_id)
        except KeyError:
            return {
                "job_id": job_id,
                "status": "unknown",
                "note": "没有这个作业。不要凭猜测重复提交，先向用户确认。",
            }
        payload = job_service.public(row)
        if payload["status"] in job_service.TERMINAL or _time.monotonic() >= deadline:
            return {
                "job": payload,
                "note": (
                    "作业仍在进行；稍后再查同一个 job_id。"
                    if payload["status"] not in job_service.TERMINAL
                    else "作业已结束，结果在 job.result 里。"
                ),
            }
        _time.sleep(0.5)


def _list_records(ctx: ToolContext) -> dict:
    from backend.services import detection as detection_service

    result = _service(
        lambda: detection_service.list_for_project(
            ctx.conn,
            int(ctx.actor.user_id),
            ctx.actor.project_id,
            status=ctx.args.get("status"),
            model_id=ctx.args.get("model_id"),
            has_hits=ctx.args.get("has_hits"),
            date_from=ctx.args.get("date_from"),
            date_to=ctx.args.get("date_to"),
            limit=int(ctx.args.get("limit") or 20),
        )
    )
    result["note"] = (
        "日期按本地日期解释；filters.applied 给出换算后的 UTC 边界。"
        "记录 ID 是真实主键，页面与工具返回的是同一批数据。"
    )
    return result


def _review_records(ctx: ToolContext) -> dict:
    from backend.services import aireview

    record_ids = [str(item) for item in (ctx.args.get("record_ids") or [])]
    if not record_ids:
        raise ToolError("NO_RECORDS", "需要 record_ids；只选一条就只传一条，不要默认核查全部")
    job, created = _service(
        lambda: aireview.start_batch(
            ctx.conn,
            int(ctx.actor.user_id),
            ctx.actor.project_id,
            record_ids=record_ids,
            request_key=str(ctx.args.get("request_key") or ""),
            session_id=ctx.actor.session_id,
            turn_id=ctx.actor.turn_id,
            model=ctx.actor.model,
        )
    )
    _emit_job(ctx, job, page="records", note="AI 核查已启动")
    return {
        "job": job,
        "created": created,
        "note": "核查会真实调用本人当前连接（会计费）；不要重复提交同一请求键。结论仅供参考，不等于人工复核。",
    }


# ---- 业务工具清单 ----------------------------------------------------------

_ID = {"type": "string", "pattern": "^[a-z]{3}_[0-9a-f]{16}$"}
_REQUEST_KEY = {"type": "string", "pattern": "^[A-Za-z0-9:_-]{6,80}$"}
_REL = {"type": "string", "minLength": 1, "maxLength": 400}

register(
    ToolSpec(
        name="hx_list_datasets",
        title="列出项目数据集",
        description="返回本项目已登记的数据集（模态、类别表、样本数、划分统计）。只看得到当前项目。",
        risk=policy.RISK_READ,
        schema={"type": "object", "properties": {}, "required": []},
        handler=_list_datasets,
    )
)
register(
    ToolSpec(
        name="hx_import_dataset",
        title="导入数据集",
        description=(
            "从项目目录或已上传的 ZIP 导入 YOLO 数据集（RGB 或 RGB+X）。会逐样本校验图片可解码、"
            "标签为 5 列且类别索引与归一化框都在范围内；缺 data.yaml 时可以给 classes 生成，"
            "但不会猜真实类别名。同一服务网页导入也在用。"
        ),
        risk=policy.RISK_WRITE,
        requires_root=True,
        schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 60},
                "rel_path": _REL,
                "asset_id": _ID,
                "modalities": {"type": "array", "maxItems": 2},
                "secondary_modality": {"type": "string", "pattern": "^(ir|edge|depth)$"},
                "classes": {"type": "array", "maxItems": 200},
            },
            "required": ["name"],
        },
        handler=_import_dataset,
        summary=lambda args: f"导入数据集 {args.get('name')}（{args.get('rel_path') or args.get('asset_id')}）",
    )
)
register(
    ToolSpec(
        name="hx_list_models",
        title="列出项目模型",
        description="返回本项目的模型（家族、单/双模态、绑定数据集、发布状态、类别映射摘要）。",
        risk=policy.RISK_READ,
        schema={"type": "object", "properties": {}, "required": []},
        handler=_list_models,
    )
)
register(
    ToolSpec(
        name="hx_import_model",
        title="导入模型权重",
        description=(
            "登记本地或已上传的 .pt 权重并校验可用性与类别索引。family=yolo 为单模态、yolomm 为双模态。"
            "类别名对齐必须显式：名称不一致时会返回 NEED_CLASS_MAPPING 与两份类别清单，"
            "由你在对话里问用户后用 class_map 重试，不允许靠翻译猜。"
        ),
        risk=policy.RISK_WRITE,
        requires_root=True,
        schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 60},
                "family": {"type": "string", "pattern": "^(yolo|yolomm)$"},
                "model_mode": {"type": "string", "pattern": "^(single|dual)$"},
                "secondary_modality": {"type": "string", "pattern": "^(ir|edge|depth)$"},
                "dataset_id": _ID,
                "rel_path": _REL,
                "asset_id": _ID,
                "class_map": {"type": "object"},
                "notes": {"type": "string", "maxLength": 500},
            },
            "required": ["name", "family"],
        },
        handler=_import_model,
        summary=lambda args: f"导入模型 {args.get('name')}（{args.get('family')}）",
    )
)
register(
    ToolSpec(
        name="hx_publish_model",
        title="发布模型到本项目",
        description=(
            "把已导入的模型发布为**当前项目**可用。必须已完成类别映射并绑定数据集；"
            "不会替换其他项目或全局模型。发布后检测才有可用模型。"
        ),
        risk=policy.RISK_WRITE,
        schema={"type": "object", "properties": {"model_id": _ID}, "required": ["model_id"]},
        handler=_publish_model,
        summary=lambda args: f"发布模型 {args.get('model_id')}",
    )
)
register(
    ToolSpec(
        name="hx_register_asset",
        title="登记项目内文件为资产",
        description=(
            "把项目目录里已有的图片/ZIP/PT 登记为资产（不搬动、不改字节），返回真实类型与 SHA。"
            "路径由服务端按项目根解析，绝对路径与越界路径会被拒绝。"
        ),
        risk=policy.RISK_WRITE,
        requires_root=True,
        schema={
            "type": "object",
            "properties": {
                "rel_path": _REL,
                "role": {"type": "string", "pattern": "^(rgb|ir|edge|depth|weight|dataset)$"},
            },
            "required": ["rel_path"],
        },
        handler=_register_asset,
        summary=lambda args: f"登记资产 {args.get('rel_path')}",
    )
)
register(
    ToolSpec(
        name="hx_detect_images",
        title="批量检测图片",
        description=(
            "用已发布模型对若干已登记图片做检测（后台作业，逐张独立，单张失败不丢其他结果）。"
            "双模态模型必须给出配套的 x_asset_ids；缺图会明确报错，不会用 RGB 冒充红外。"
            "request_key 保证重复提交不产生重复记录。"
        ),
        risk=policy.RISK_WRITE,
        schema={
            "type": "object",
            "properties": {
                "model_id": _ID,
                "rgb_asset_ids": {"type": "array", "maxItems": 50},
                "x_asset_ids": {"type": "array", "maxItems": 50},
                "confidence": {"type": "number", "minimum": 0.01, "maximum": 0.99},
                "request_key": _REQUEST_KEY,
            },
            "required": ["model_id", "rgb_asset_ids", "request_key"],
        },
        handler=_detect_images,
        summary=lambda args: f"检测 {len(args.get('rgb_asset_ids') or [])} 张图",
    )
)
register(
    ToolSpec(
        name="hx_inspect_directory",
        title="按目录巡检",
        description=(
            "在项目目录里发现图片（含 images/ + images_ir/ 配对），按用户给定的置信度、数量上限与"
            "已消歧的参数做：准备资产 → 检测 → 按需 AI 核查 → 生成本轮人工队列，并给出页面位置。"
            "这是同一个后台作业流程，不另起规划器；参数齐了就**直接提交**，不要再用一轮对话"
            "确认计划或征求“是否继续”。"
        ),
        risk=policy.RISK_WRITE,
        requires_root=True,
        schema={
            "type": "object",
            "properties": {
                "model_id": _ID,
                "directory": _REL,
                "x_directory": _REL,
                "confidence": {"type": "number", "minimum": 0.01, "maximum": 0.99},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "review": {"type": "boolean"},
                "request_key": _REQUEST_KEY,
            },
            "required": ["model_id", "directory", "request_key"],
        },
        handler=_inspect_directory,
        summary=lambda args: f"巡检目录 {args.get('directory')}（上限 {args.get('limit') or 20} 张）",
    )
)
register(
    ToolSpec(
        name="hx_job_status",
        title="查询业务作业状态",
        description=(
            "按 job_id 查询检测/巡检/核查作业的真实状态与结果，最多等待 10 秒（短于 MCP 超时）。"
            "返回 unknown 表示没有该作业，不要重复提交；running 只说明还没结束，不代表失败。"
            "原目标还有已授权的后续步骤时，可以连续调用做有界等待，拿到 true 结果后继续做，"
            "不要把这一轮结束在“跑完再告诉我”。"
        ),
        risk=policy.RISK_READ,
        schema={
            "type": "object",
            "properties": {
                "job_id": _ID,
                "wait_seconds": {"type": "number", "minimum": 0, "maximum": 10},
            },
            "required": ["job_id"],
        },
        handler=_job_status,
    )
)
register(
    ToolSpec(
        name="hx_list_records",
        title="列出识别记录",
        description=(
            "分页列出本项目的检测记录，支持状态/模型/是否有命中/本地日期区间筛选，并返回真实 record_id、"
            "命中数、置信度与时间（UTC 与本地各一份），便于和页面上的记录对应。"
        ),
        risk=policy.RISK_READ,
        schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "pattern": "^(running|done|failed|paused)$"},
                "model_id": _ID,
                "has_hits": {"type": "boolean"},
                "date_from": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                "date_to": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": [],
        },
        handler=_list_records,
    )
)
register(
    ToolSpec(
        name="hx_review_records",
        title="让 AI 核查指定记录",
        description=(
            "对用户**明确选中**的 1-20 条记录调用本人当前连接做看图核查。必须传 record_ids；"
            "不会因为点开某条记录就核查全部。核查真实发图（RGB + 配套图），会计费，不要重复提交。"
            "结论只写入 AI 建议，不等于人工复核。"
        ),
        risk=policy.RISK_WRITE,
        schema={
            "type": "object",
            "properties": {
                "record_ids": {"type": "array", "maxItems": 20},
                "request_key": _REQUEST_KEY,
            },
            "required": ["record_ids", "request_key"],
        },
        handler=_review_records,
        summary=lambda args: f"AI 核查 {len(args.get('record_ids') or [])} 条记录",
    )
)

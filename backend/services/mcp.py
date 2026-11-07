"""外部 MCP：手工配置、工具发现、逐项授权、真实调用与审计。

这个模块只做**网关**，不开第二套 Agent：

* 配置由用户手工填写（本机 stdio 命令，或一个网络地址 + header），
  模型没有任何工具能写配置、改凭据或启动新服务；
* **stdio 服务会在评委机器上以当前系统用户身份运行宿主程序**，
  所以注册、更新、发现、调用一律要求管理员角色（``MCP_ADMIN_REQUIRED``）：
  普通账号（member）只能使用管理员配置并审核过的服务，自己在页面上
  填一条任意命令也注册不进来——发现阶段会运行程序，授权不能等到调用才检查；
* 发现到的工具**默认 ``pending``**——不授权就调不动。``full`` 权限只自动执行
  **内置业务工具**，这条路径完全不看权限模式，因此不存在"模式一开就静默放行外部工具"；
  远端声明的 ``readOnlyHint`` **只作为展示标记**，不构成任何授权依据：
  "只读自动放行"已删除，授权必须是用户核对每个工具后的显式决定；
* 凭据用 AES-GCM 单独加密（``secrets/mcp.key``），接口只回掩码，错误信息里也做替换。

三条**说清楚的边界**（不是"已经沙箱化了"）：

1. stdio 服务是**当前操作系统用户**的子进程，本平台没有 OS 级沙箱；
   只配置你信任的服务器，这是权限边界而不是隔离边界。
2. http 服务只允许 https；明文 http 仅限 ``127.0.0.1`` / ``localhost`` / ``::1``，
   避免把凭据明文发到网络上。
3. 一次调用有总时长与响应大小上限；超时/超限就失败，不排队重试、不截断后假装成功。
"""

from __future__ import annotations

import json
import os
import shutil
import time
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Any

from flask import current_app

from backend import crypto, mcp_client, security
from backend.errors import ApiError
from backend.services import storage

TRANSPORTS = ("stdio", "http")
DISCOVERY_TIMEOUT = 20.0
CALL_TIMEOUT = 30.0
MAX_ARGS_BYTES = 64 * 1024
MAX_RESULT_CHARS = 20_000
MAX_TOOLS = 200
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_SECRET_KEY_NAME = "mcp.key"
_COMMAND_META = set("|&;<>`$\n\r")


# --------------------------------------------------------------------------- 凭据


# 主密钥目录在应用工厂启动时登记一次：工具处理器可能在没有 Flask 请求上下文
# 的线程里被调用（引擎侧），不能只靠 ``current_app``。
_SECRET_DIR: Path | None = None


def configure(secret_dir: str | Path) -> None:
    global _SECRET_DIR
    _SECRET_DIR = Path(secret_dir)


def _key() -> bytes:
    directory = _SECRET_DIR
    if directory is None:
        try:
            directory = Path(current_app.config["HX17"].secret_dir)
        except Exception:
            raise ApiError(
                503, "MCP_NOT_READY", "服务还没完成初始化，稍后再试"
            ) from None
    return security.load_or_create_key(Path(directory) / _SECRET_KEY_NAME)


def _aad(server_id: str) -> bytes:
    return f"mcp:{server_id}".encode("utf-8")


def _encrypt_secrets(server_id: str, payload: dict[str, str]) -> str:
    if not payload:
        return ""
    return crypto.encrypt_text(json.dumps(payload, ensure_ascii=False), _key(), aad=_aad(server_id))


def _decrypt_secrets(server_id: str, cipher: str) -> dict[str, str]:
    if not cipher:
        return {}
    try:
        raw = crypto.decrypt_text(cipher, _key(), aad=_aad(server_id))
    except Exception:
        raise ApiError(
            409, "MCP_SECRET_UNREADABLE", "这台机器的凭据密钥对不上，请重新填写凭据"
        ) from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ApiError(409, "MCP_SECRET_UNREADABLE", "凭据内容损坏，请重新填写") from None
    return {str(key): str(value) for key, value in (data or {}).items() if str(value)}


def _hints(values: dict[str, str]) -> dict[str, str]:
    return {key: security.mask_secret(value) for key, value in values.items()}


def _redact(text: Any, secrets: dict[str, str]) -> str:
    out = str(text or "")
    for value in secrets.values():
        if value and value in out:
            out = out.replace(value, "***")
    return out


# --------------------------------------------------------------------------- 校验


def _require_admin(conn, user_id: int) -> None:
    """stdio 外部 MCP 会在评委机器上运行宿主程序：只有管理员能配置和触碰。

    member 在页面上填一条任意命令也注册不进来（403），自然谈不上
    "发现/调用时再拦"——发现阶段就会启动进程。
    """
    row = conn.execute("SELECT role FROM users WHERE id = ?", (int(user_id),)).fetchone()
    if row is None or str(row["role"]) != "admin":
        raise ApiError(
            403,
            "MCP_ADMIN_REQUIRED",
            "本机 stdio 外部 MCP 只能由管理员配置：普通账号只能使用管理员"
            "配置并审核过的服务，不能自行注册宿主程序",
        )


def _check_name(name: str) -> str:
    label = str(name or "").strip()
    if not label:
        raise ApiError(400, "VALIDATION", "服务名称不能为空")
    if len(label) > 60:
        raise ApiError(400, "VALIDATION", "服务名称太长（最多 60 字）")
    return label


def _check_command(command: str, args: list[str]) -> tuple[str, list[str]]:
    """命令必须是可解析的可执行文件；参数是列表，不经过 shell。"""
    raw = str(command or "").strip()
    if not raw:
        raise ApiError(400, "VALIDATION", "stdio 服务必须填写可执行文件")
    if any(char in _COMMAND_META for char in raw):
        raise ApiError(
            400,
            "MCP_BAD_COMMAND",
            "可执行文件路径里不能带 shell 元字符（| & ; < > ` $ 换行）——参数请填在参数列表里",
        )
    resolved = raw if os.path.isabs(raw) else shutil.which(raw)
    if not resolved or not Path(resolved).is_file():
        raise ApiError(
            404,
            "MCP_COMMAND_NOT_FOUND",
            f"找不到这个可执行文件：{raw}。请填绝对路径，或确保它在 PATH 里（本平台不下载、不安装）",
        )
    clean: list[str] = []
    if not isinstance(args, list) or len(args) > 40:
        raise ApiError(400, "VALIDATION", "参数必须是列表，且不超过 40 项")
    for item in args:
        text = str(item)
        if "\x00" in text or "\n" in text:
            raise ApiError(400, "VALIDATION", "参数里不能有换行或空字符")
        if len(text) > 300:
            raise ApiError(400, "VALIDATION", "单个参数太长（最多 300 字符）")
        clean.append(text)
    return resolved, clean


def _check_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        raise ApiError(400, "VALIDATION", "http 服务必须填写地址")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ApiError(400, "MCP_BAD_URL", "地址必须是 http(s)://主机/路径 的完整 URL")
    if parsed.username or parsed.password:
        raise ApiError(
            400,
            "MCP_BAD_URL",
            "不要把账号密码写在 URL 里；需要认证请填到 header 中（会加密保存）",
        )
    if parsed.scheme == "http" and parsed.hostname not in _LOCAL_HOSTS:
        raise ApiError(
            400,
            "MCP_INSECURE_URL",
            "明文 http 只允许本机（127.0.0.1 / localhost / ::1）；其它主机请用 https",
        )
    return raw


def _check_secret_map(payload: Any, *, kind: str) -> dict[str, str]:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ApiError(400, "VALIDATION", f"{kind} 必须是键值对象")
    if len(payload) > 40:
        raise ApiError(400, "VALIDATION", f"{kind} 条目太多（最多 40 项）")
    clean: dict[str, str] = {}
    for key, value in payload.items():
        name = str(key).strip()
        if kind == "环境变量":
            if not name or not (name[0].isalpha() or name[0] == "_") or not all(
                char.isalnum() or char == "_" for char in name
            ):
                raise ApiError(400, "VALIDATION", f"环境变量名不合法：{name}")
        else:
            if not name or not all(char.isalnum() or char in "-_" for char in name):
                raise ApiError(400, "VALIDATION", f"header 名不合法：{name}")
        text = "" if value is None else str(value)
        if len(text) > 2000:
            raise ApiError(400, "VALIDATION", f"{kind}「{name}」的值太长（最多 2000 字符）")
        if text:
            clean[name] = text
    return clean


def _cwd(root: Path, rel: str) -> str:
    text = str(rel or "").strip().strip("/\\")
    if not text:
        return str(root)
    target = storage.resolve_rel(root, text, must_exist=True)
    if not target.is_dir():
        raise ApiError(400, "VALIDATION", "工作目录必须是项目内的目录")
    return str(target)


# --------------------------------------------------------------------------- 读出


def public(row: sqlite3.Row | dict, *, tools: list[dict] | None = None) -> dict:
    data = dict(row)
    hints = json.loads(data.get("secret_hints") or "{}")
    item = {
        "id": data["id"],
        "name": data["name"],
        "transport": data["transport"],
        "command": data["command"] or "",
        "args": json.loads(data.get("args") or "[]"),
        "cwd_rel": data.get("cwd_rel") or "",
        "url": data["url"] or "",
        "enabled": bool(data["enabled"]),
        "status": data["status"],
        "last_error": data.get("last_error"),
        "last_seen_at": data.get("last_seen_at"),
        "created_at": data["created_at"],
        "admin_only": data["transport"] == "stdio",
        "secret_keys": sorted(hints.keys()),
        "secret_hints": hints,
        "note": (
            "stdio 服务以当前系统用户身份运行，本平台没有 OS 级沙箱；只配置信任的服务器，"
            "且只有管理员能配置。"
            if data["transport"] == "stdio"
            else "只发一次 POST，不跟随重定向；明文 http 仅限本机。"
        ),
    }
    if tools is not None:
        item["tools"] = tools
    return item


def _tool_public(row: sqlite3.Row) -> dict:
    return {
        "name": row["name"],
        "title": row["title"],
        "description": row["description"],
        "schema": json.loads(row["schema"] or "{}"),
        "read_only": bool(row["read_only"]),
        "authorization": row["authorization"],
        "discovered_at": row["discovered_at"],
        "callable": row["authorization"] == "allowed",
    }


def get_owned(conn, user_id: int, project_id: str, server_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM mcp_servers WHERE id = ? AND project_id = ? AND owner_id = ?"
        " AND deleted_at IS NULL",
        (str(server_id), str(project_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise ApiError(404, "MCP_SERVER_NOT_FOUND", "MCP 服务不存在或不属于当前项目")
    return row


def list_servers(conn, user_id: int, project_id: str) -> dict:
    rows = conn.execute(
        "SELECT * FROM mcp_servers WHERE project_id = ? AND owner_id = ? AND deleted_at IS NULL"
        " ORDER BY created_at",
        (str(project_id), int(user_id)),
    ).fetchall()
    servers = []
    for row in rows:
        tools = conn.execute(
            "SELECT * FROM mcp_tools WHERE server_id = ? ORDER BY name", (row["id"],)
        ).fetchall()
        servers.append(public(row, tools=[_tool_public(tool) for tool in tools]))
    return {
        "servers": servers,
        "note": (
            "发现到的工具默认需要逐项授权；远端声明只读只是提示，不构成授权；"
            "full 权限只自动执行内置业务工具，不会因此放宽这里的任何工具。"
            "stdio 服务只有管理员能配置。"
        ),
    }


# --------------------------------------------------------------------------- 写入


def create(
    conn,
    user_id: int,
    project_id: str,
    *,
    name: str,
    transport: str,
    command: str = "",
    args: list[str] | None = None,
    cwd_rel: str = "",
    url: str = "",
    env: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    enabled: bool = False,
) -> dict:
    label = _check_name(name)
    kind = str(transport or "").strip()
    if kind not in TRANSPORTS:
        raise ApiError(400, "VALIDATION", "transport 只能是 stdio 或 http")
    if kind == "stdio":
        # 角色门禁在解析命令之前：member 不允许注册任何宿主程序。
        _require_admin(conn, user_id)
    root = storage.project_root(conn, user_id, project_id)
    server_id = security.new_id("mcp")
    if kind == "stdio":
        resolved, clean_args = _check_command(command, list(args or []))
        work_dir = _cwd(root, cwd_rel)
        clean_url = ""
        secrets = _check_secret_map(env, kind="环境变量")
    else:
        clean_url = _check_url(url)
        resolved, clean_args = "", []
        work_dir = str(root)
        secrets = _check_secret_map(headers, kind="header")
    now = security.now_iso()
    try:
        conn.execute(
            "INSERT INTO mcp_servers (id, project_id, owner_id, name, transport, command, args,"
            " cwd_rel, url, secrets_cipher, secret_hints, enabled, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                server_id,
                str(project_id),
                int(user_id),
                label,
                kind,
                resolved,
                json.dumps(clean_args, ensure_ascii=False),
                "" if kind == "stdio" and work_dir == str(root) else (
                    storage.relative(root, Path(work_dir)) if kind == "stdio" else ""
                ),
                clean_url,
                _encrypt_secrets(server_id, secrets),
                json.dumps(_hints(secrets), ensure_ascii=False),
                1 if enabled else 0,
                "unknown" if enabled else "disabled",
                now,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ApiError(409, "MCP_NAME_TAKEN", "这个项目里已经有同名 MCP 服务，请换个名称") from None
    return public(get_owned(conn, user_id, project_id, server_id))


def update(
    conn,
    user_id: int,
    project_id: str,
    server_id: str,
    *,
    name: str | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    cwd_rel: str | None = None,
    url: str | None = None,
    env: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    enabled: bool | None = None,
) -> dict:
    row = get_owned(conn, user_id, project_id, server_id)
    kind = str(row["transport"])
    # stdio 的任何变更（含启停）都等于改变"会在本机运行的程序"，管理员才动得了。
    if kind == "stdio":
        _require_admin(conn, user_id)
    root = storage.project_root(conn, user_id, project_id)
    updates: dict[str, Any] = {}

    if name is not None:
        updates["name"] = _check_name(name)
    if kind == "stdio":
        if command is not None or args is not None:
            resolved, clean_args = _check_command(
                row["command"] if command is None else command,
                json.loads(row["args"] or "[]") if args is None else args,
            )
            updates["command"] = resolved
            updates["args"] = json.dumps(clean_args, ensure_ascii=False)
        if cwd_rel is not None:
            work_dir = _cwd(root, cwd_rel)
            updates["cwd_rel"] = "" if work_dir == str(root) else storage.relative(root, Path(work_dir))
    else:
        if url is not None:
            updates["url"] = _check_url(url)

    # 凭据：值给空串表示删除该键，不给表示"保持原样"。
    incoming = env if kind == "stdio" else headers
    if incoming is not None:
        label = "环境变量" if kind == "stdio" else "header"
        cleaned = _check_secret_map(incoming, kind=label)
        current = _decrypt_secrets(server_id, row["secrets_cipher"]) if row["secrets_cipher"] else {}
        merged = dict(current)
        for key, value in cleaned.items():
            if value:
                merged[key] = value
        for key in list(incoming.keys()):
            if not str(incoming.get(key) or ""):
                merged.pop(str(key).strip(), None)
        updates["secrets_cipher"] = _encrypt_secrets(server_id, merged)
        updates["secret_hints"] = json.dumps(_hints(merged), ensure_ascii=False)

    if enabled is not None:
        updates["enabled"] = 1 if enabled else 0
        if not enabled:
            updates["status"] = "disabled"
            updates["last_error"] = None
        elif row["status"] == "disabled":
            updates["status"] = "unknown"
    if updates:
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE mcp_servers SET {assignments} WHERE id = ? AND owner_id = ?",
            (*updates.values(), str(server_id), int(user_id)),
        )
    conn.commit()
    return _detail(conn, user_id, project_id, server_id)


def delete(conn, user_id: int, project_id: str, server_id: str) -> dict:
    get_owned(conn, user_id, project_id, server_id)
    conn.execute(
        "UPDATE mcp_servers SET deleted_at = ?, enabled = 0, status = 'disabled'"
        " WHERE id = ? AND owner_id = ?",
        (security.now_iso(), str(server_id), int(user_id)),
    )
    conn.execute("DELETE FROM mcp_tools WHERE server_id = ?", (str(server_id),))
    conn.commit()
    return {"status": "deleted", "id": str(server_id)}


def _detail(conn, user_id: int, project_id: str, server_id: str) -> dict:
    row = get_owned(conn, user_id, project_id, server_id)
    tools = conn.execute(
        "SELECT * FROM mcp_tools WHERE server_id = ? ORDER BY name", (str(server_id),)
    ).fetchall()
    return public(row, tools=[_tool_public(tool) for tool in tools])


# --------------------------------------------------------------------------- 发现


def _session(conn, row: sqlite3.Row, *, timeout: float):
    """按配置构造传输会话；凭据解密只在这里发生，用完即丢。"""
    server_id = str(row["id"])
    secrets = _decrypt_secrets(server_id, row["secrets_cipher"])
    if row["transport"] == "stdio":
        session = mcp_client.StdioSession(
            command=str(row["command"]),
            args=json.loads(row["args"] or "[]"),
            env=secrets,
            cwd=_resolve_cwd(conn, row, str(row["cwd_rel"] or "")),
            timeout=timeout,
        )
        return session, secrets
    return mcp_client.HttpSession(url=str(row["url"]), headers=secrets, timeout=timeout), secrets


def _resolve_cwd(conn, row: sqlite3.Row, cwd_rel: str) -> str | None:
    """把保存时校验过的相对工作目录复原成绝对路径（仍走一次 guard）。"""
    if not cwd_rel:
        return None
    root = storage.project_root(conn, int(row["owner_id"]), str(row["project_id"]))
    return str(storage.resolve_rel(root, cwd_rel, must_exist=True))


def _handshake(session, *, timeout: float) -> dict:
    deadline = mcp_client._deadline(timeout)
    result = session.request(
        "initialize",
        {
            "protocolVersion": mcp_client.PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "hx17", "version": "1.7"},
        },
        deadline=deadline,
    )
    session.notify("notifications/initialized", {})
    return result


def _list_tools(session, *, timeout: float) -> list[dict]:
    deadline = mcp_client._deadline(timeout)
    collected: list[dict] = []
    cursor = None
    for _page in range(5):
        params = {"cursor": cursor} if cursor else {}
        result = session.request("tools/list", params, deadline=deadline)
        items = result.get("tools")
        if not isinstance(items, list):
            raise mcp_client.McpTransportError(
                "MCP_BAD_RESPONSE", "tools/list 没有返回工具列表", step="tools/list"
            )
        collected.extend([item for item in items if isinstance(item, dict)])
        cursor = result.get("nextCursor")
        if not cursor or len(collected) >= MAX_TOOLS:
            break
    return collected[:MAX_TOOLS]


def _is_read_only(tool: dict) -> bool:
    annotations = tool.get("annotations")
    if not isinstance(annotations, dict):
        return False
    # 服务端自己声明只读才认；声明了破坏性的一律不认。
    return annotations.get("readOnlyHint") is True and annotations.get("destructiveHint") is not True


def discover(conn, user_id: int, project_id: str, server_id: str) -> dict:
    """连一次外部服务，把工具清单同步进库；新工具一律 ``pending``。

    发现阶段就会**运行**stdio 宿主程序，所以角色门禁在这里同样生效，
    不能等到工具调用时才检查。
    """
    row = get_owned(conn, user_id, project_id, server_id)
    if row["deleted_at"]:
        raise ApiError(409, "MCP_SERVER_DELETED", "这个服务在回收站里")
    if row["transport"] == "stdio":
        _require_admin(conn, user_id)
    session, secrets = _session(conn, row, timeout=DISCOVERY_TIMEOUT)
    try:
        session.open()
        _handshake(session, timeout=DISCOVERY_TIMEOUT)
        tools = _list_tools(session, timeout=DISCOVERY_TIMEOUT)
    except mcp_client.McpTransportError as error:
        message = _redact(f"{error.message}", secrets)[:500]
        conn.execute(
            "UPDATE mcp_servers SET status = 'error', last_error = ? WHERE id = ?",
            (message, str(server_id)),
        )
        conn.commit()
        return {
            "ok": False,
            "server": public(get_owned(conn, user_id, project_id, server_id)),
            "error": {"code": error.code, "message": message},
            "note": "发现失败不会自动重试；请修配置后再点一次。",
        }
    finally:
        session.close()

    now = security.now_iso()
    existing = {
        str(item["name"]): item
        for item in conn.execute("SELECT * FROM mcp_tools WHERE server_id = ?", (server_id,))
    }
    seen: set[str] = set()
    for tool in tools:
        name = str(tool.get("name") or "").strip()
        if not name or len(name) > 120:
            continue
        seen.add(name)
        read_only = _is_read_only(tool)
        previous = existing.get(name)
        if previous is None:
            # 新工具一律 pending：远端 readOnlyHint 不构成授权依据。
            authorization = "pending"
        else:
            authorization = str(previous["authorization"])
        conn.execute(
            "INSERT INTO mcp_tools (server_id, name, title, description, schema, read_only,"
            " authorization, discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(server_id, name) DO UPDATE SET title = excluded.title,"
            " description = excluded.description, schema = excluded.schema,"
            " read_only = excluded.read_only, authorization = excluded.authorization",
            (
                str(server_id),
                name,
                str(tool.get("title") or "")[:200],
                str(tool.get("description") or "")[:1000],
                json.dumps(tool.get("inputSchema") or {}, ensure_ascii=False)[:20_000],
                1 if read_only else 0,
                authorization,
                now,
            ),
        )
    for name in set(existing) - seen:
        conn.execute("DELETE FROM mcp_tools WHERE server_id = ? AND name = ?", (str(server_id), name))
    conn.execute(
        "UPDATE mcp_servers SET status = 'connected', last_error = NULL, last_seen_at = ?"
        " WHERE id = ?",
        (now, str(server_id)),
    )
    conn.commit()
    return {
        "ok": True,
        "server": _detail(conn, user_id, project_id, server_id),
        "new_tools": sorted(seen - set(existing)),
        "dropped_tools": sorted(set(existing) - seen),
        "note": (
            "新发现的工具一律待授权（pending）：远端声明只读只是提示，"
            "必须逐个核对后显式允许，没有任何自动放行。"
        ),
    }


def authorize(
    conn, user_id: int, project_id: str, server_id: str, tool_name: str, decision: str
) -> dict:
    get_owned(conn, user_id, project_id, server_id)
    action = str(decision or "").strip()
    if action not in {"allow", "deny", "revoke"}:
        raise ApiError(400, "VALIDATION", "decision 只能是 allow / deny / revoke")
    row = conn.execute(
        "SELECT * FROM mcp_tools WHERE server_id = ? AND name = ?", (str(server_id), str(tool_name))
    ).fetchone()
    if row is None:
        raise ApiError(404, "MCP_TOOL_NOT_FOUND", "没有这个工具，请先发现一次")
    target = "allowed" if action == "allow" else "denied"
    if action == "revoke":
        target = "pending"
    conn.execute(
        "UPDATE mcp_tools SET authorization = ? WHERE server_id = ? AND name = ?",
        (target, str(server_id), str(tool_name)),
    )
    conn.commit()
    return {"tool": _tool_public(conn.execute(
        "SELECT * FROM mcp_tools WHERE server_id = ? AND name = ?", (str(server_id), str(tool_name))
    ).fetchone())}


# --------------------------------------------------------------------------- 调用


def _validate_args(schema: dict, args: dict) -> dict:
    if not isinstance(args, dict):
        raise ApiError(400, "MCP_BAD_ARGUMENTS", "arguments 必须是对象")
    encoded = json.dumps(args, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_ARGS_BYTES:
        raise ApiError(413, "MCP_ARGS_TOO_LARGE", "参数过大（上限 64KB）")
    if schema.get("type") == "object" or "properties" in schema:
        required = schema.get("required") or []
        missing = [str(key) for key in required if str(key) not in args]
        if missing:
            raise ApiError(
                400, "MCP_ARGS_MISSING", "缺少必填参数：" + "、".join(missing), details={"missing": missing}
            )
        properties = schema.get("properties") or {}
        expected = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        wrong: list[str] = []
        for key, value in args.items():
            rule = properties.get(str(key))
            if not isinstance(rule, dict):
                continue
            kind = rule.get("type")
            if kind in expected and not isinstance(value, expected[kind]):
                wrong.append(f"{key} 需要 {kind}")
            if isinstance(value, bool) and kind in {"integer", "number"}:
                wrong.append(f"{key} 需要 {kind}")
        if wrong:
            raise ApiError(400, "MCP_ARGS_TYPE", "参数类型不对：" + "；".join(wrong[:5]))
    return args


def _text_of(result: dict) -> str:
    content = result.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif item.get("type") == "image":
                parts.append("[图片内容：本阶段不把外部图片传给模型]")
            elif item.get("type") == "resource":
                parts.append(f"[资源：{str(item.get('uri') or '')[:200]}]")
    if not parts:
        parts.append(json.dumps(result, ensure_ascii=False))
    text = "\n".join(parts)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n…（已截断，原长 {len(text)} 字符）"
    return text


def call(
    conn,
    user_id: int,
    project_id: str,
    server_id: str,
    *,
    tool_name: str,
    arguments: dict | None = None,
    request_key: str | None = None,
    turn_id: str | None = None,
) -> dict:
    """真实调用一个**已授权**的外部工具。

    授权检查在这里、且**只看逐项授权**：不看 ``full`` 之类的权限模式，
    所以"把权限调到最高"不会让外部工具变得可调。
    """
    row = get_owned(conn, user_id, project_id, server_id)
    name = str(tool_name or "").strip()
    if not row["enabled"]:
        raise ApiError(409, "MCP_DISABLED", "这个 MCP 服务没有启用，不能调用")
    if row["transport"] == "stdio":
        # 调用也会在本机运行宿主程序：member 只能用管理员审核过的服务。
        _require_admin(conn, user_id)
    tool = conn.execute(
        "SELECT * FROM mcp_tools WHERE server_id = ? AND name = ?", (str(server_id), name)
    ).fetchone()
    if tool is None:
        raise ApiError(404, "MCP_TOOL_NOT_FOUND", "没有这个工具，请先发现一次")
    if str(tool["authorization"]) != "allowed":
        raise ApiError(
            403,
            "MCP_NOT_AUTHORIZED",
            "这个工具还没有被授权：外部工具默认逐项授权，"
            "提高整体权限或打开 full 都不会自动放行，请在“设置 → 外部 MCP”里单独允许",
        )
    schema = json.loads(tool["schema"] or "{}")
    payload = _validate_args(schema, arguments or {})

    if request_key:
        previous = conn.execute(
            "SELECT * FROM mcp_calls WHERE server_id = ? AND request_key = ?",
            (str(server_id), str(request_key)),
        ).fetchone()
        if previous is not None:
            return {
                "ok": bool(previous["ok"]),
                "replayed": True,
                "tool": name,
                "text": previous["result"] or "",
                "error": ({"code": "MCP_PREVIOUS_FAILED", "message": previous["error"]}
                          if previous["error"] else None),
                "duration_ms": int(previous["duration_ms"]),
                "note": "这个 request_key 已经调用过，返回的是当时的记录，没有再次请求外部服务。",
            }

    session, secrets = _session(conn, row, timeout=CALL_TIMEOUT)
    started = time.monotonic()
    call_id = security.new_id("mcp")
    error_code = ""
    error_text = ""
    text = ""
    ok = False
    try:
        session.open()
        _handshake(session, timeout=CALL_TIMEOUT)
        result = session.request("tools/call", {"name": name, "arguments": payload}, deadline=mcp_client._deadline(CALL_TIMEOUT))
        text = _redact(_text_of(result), secrets)
        ok = not bool(result.get("isError"))
        if not ok:
            error_code, error_text = "MCP_TOOL_ERROR", text[:500]
    except mcp_client.McpTransportError as error:
        error_code = error.code
        error_text = _redact(error.message, secrets)[:500]
    finally:
        session.close()
    duration = int(max(0.0, time.monotonic() - started) * 1000)
    conn.execute(
        "INSERT INTO mcp_calls (id, server_id, tool_name, actor_id, turn_id, request_key, ok,"
        " error, result, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            call_id,
            str(server_id),
            name,
            int(user_id),
            turn_id,
            request_key,
            1 if ok else 0,
            error_text or None,
            text[:MAX_RESULT_CHARS],
            duration,
            security.now_iso(),
        ),
    )
    conn.commit()
    if not ok:
        return {
            "ok": False,
            "tool": name,
            "text": text[:MAX_RESULT_CHARS],
            "error": {"code": error_code or "MCP_CALL_FAILED", "message": error_text},
            "duration_ms": duration,
        }
    return {"ok": True, "tool": name, "text": text, "duration_ms": duration, "call_id": call_id}


def recent_calls(conn, user_id: int, project_id: str, server_id: str, *, limit: int = 20) -> dict:
    get_owned(conn, user_id, project_id, server_id)
    rows = conn.execute(
        "SELECT * FROM mcp_calls WHERE server_id = ? ORDER BY created_at DESC LIMIT ?",
        (str(server_id), max(1, min(int(limit), 100))),
    ).fetchall()
    return {
        "calls": [
            {
                "id": item["id"],
                "tool": item["tool_name"],
                "ok": bool(item["ok"]),
                "error": item["error"],
                "duration_ms": int(item["duration_ms"]),
                "created_at": item["created_at"],
            }
            for item in rows
        ]
    }


__all__ = [
    "CALL_TIMEOUT",
    "configure",
    "DISCOVERY_TIMEOUT",
    "TRANSPORTS",
    "authorize",
    "call",
    "create",
    "delete",
    "discover",
    "get_owned",
    "list_servers",
    "public",
    "recent_calls",
    "update",
]

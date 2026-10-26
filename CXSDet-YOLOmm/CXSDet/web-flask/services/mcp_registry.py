"""MCP connection registry and execution adapter (C-10).

Separation of concerns, deliberately:

* **Configuring** a connection stores only what is needed to reach it. Tokens
  and keys go to the workspace secret store (``secret_store``), and this table
  keeps a reference, never the secret.
* **Connecting** (``test``) reports transport health. It never implies that any
  tool may be called: every call still needs the workspace policy to grant
  ``mcp:execute`` on the connection.
* **Discovering** caches the advertised tool list with a content hash so a
  changed external service is visible and the workspace can re-check its
  authorisation instead of silently trusting yesterday's list.
* **Executing** refuses a tool that is not in the cached discovery, records the
  declared effects (write / billable / idempotent / cancellable) and treats an
  unknown outcome as unknown. A read-only tool failure is safe to report as
  ``failed``; a write tool failure leaves the outcome to the external service,
  so it is raised as an unexpected error and the adapter marks it ``unknown``.

The site never installs a service. It starts only the stdio program explicitly
saved by the user, and does not claim to sandbox that external process: the
external service's own access rules apply.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


class McpUnavailable(ValueError):
    code = "MCP_TRANSPORT_UNAVAILABLE"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(database_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(database_path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS mcp_connections (
            id TEXT PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            transport TEXT NOT NULL,
            endpoint TEXT,
            command TEXT,
            args_json TEXT NOT NULL DEFAULT '[]',
            credential_ref TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'configured',
            tools_version TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS mcp_tools (
            connection_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            schema_json TEXT NOT NULL DEFAULT '{}',
            writes INTEGER NOT NULL DEFAULT 1,
            billable INTEGER NOT NULL DEFAULT 0,
            idempotent INTEGER NOT NULL DEFAULT 0,
            cancellable INTEGER NOT NULL DEFAULT 0,
            effects_declared INTEGER NOT NULL DEFAULT 0,
            version TEXT,
            PRIMARY KEY (connection_id, name)
        )"""
    )
    if "revision" not in {row[1] for row in conn.execute("PRAGMA table_info(mcp_connections)")}:
        conn.execute("ALTER TABLE mcp_connections ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")


class McpStore:
    """Connection records plus an injected transport factory.

    ``transport_factory(config)`` must return an object exposing
    ``list_tools() -> list[dict]`` and ``call_tool(name, arguments) -> dict``.
    Without it every connection reports ``unavailable`` instead of pretending
    to have talked to a server.
    """

    def __init__(self, database_path: str | Path, *, transport_factory=None,
                 secret_store=None) -> None:
        self.database_path = str(database_path)
        self.transport_factory = transport_factory
        self.secret_store = secret_store
        with _connect(self.database_path) as conn:
            _ensure_tables(conn)

    # ---------------------------------------------------------------- records

    def save(self, owner_id: int, payload: dict) -> dict:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("连接名称不能为空")
        if len(name) > 120 or ('enabled' in payload and not isinstance(payload['enabled'], bool)):
            raise ValueError('连接名称过长或启用状态不是布尔值')
        if payload.get("transport") not in ("stdio", "http"):
            raise ValueError("transport 只能为 stdio 或 http（Streamable HTTP）")
        if not isinstance(payload.get("arguments", []), list) or any(not isinstance(a, str) for a in payload.get("arguments", [])):
            raise ValueError("arguments 必须是字符串数组")
        if not payload.get("command" if payload["transport"] == "stdio" else "endpoint"):
            raise ValueError("请填写本机程序或网络地址")
        if payload['transport'] == 'http':
            parts = urlsplit(payload['endpoint'])
            if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
                raise ValueError('地址须为不含凭据、查询参数和片段的 HTTP(S) MCP 地址；凭据请单独填写')
        connection_id = payload.get("id") or str(uuid.uuid4())
        args = [str(value) for value in (payload.get("arguments") or [])]
        with _connect(self.database_path) as conn:
            _ensure_tables(conn)
            row = conn.execute(
                "SELECT owner_id FROM mcp_connections WHERE id=?", (connection_id,)
            ).fetchone()
            if row is not None and row["owner_id"] != owner_id:
                raise PermissionError("该连接不属于当前用户")
            timestamp = _now()
            if row is None:
                conn.execute(
                    "INSERT INTO mcp_connections (id, owner_id, name, transport, endpoint, "
                    "command, args_json, credential_ref, enabled, status, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'configured', ?, ?)",
                    (
                        connection_id, owner_id, name, payload["transport"],
                        payload.get("endpoint"), payload.get("command"), json.dumps(args),
                        payload.get("credential_ref"),
                        1 if payload.get("enabled", True) else 0, timestamp, timestamp,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE mcp_connections SET name=?, transport=?, endpoint=?, command=?, "
                    "args_json=?, credential_ref=?, enabled=?, updated_at=?, revision=revision+1, tools_version=NULL, status='configured' WHERE id=?",
                    (
                        name, payload["transport"], payload.get("endpoint"),
                        payload.get("command"), json.dumps(args),
                        payload.get("credential_ref"),
                        1 if payload.get("enabled", True) else 0, timestamp,
                        connection_id,
                    ),
                )
                conn.execute("DELETE FROM mcp_tools WHERE connection_id=?", (connection_id,))
        return self.get(connection_id, owner_id)

    def get(self, connection_id: str, owner_id: int) -> dict:
        with _connect(self.database_path) as conn:
            _ensure_tables(conn)
            row = conn.execute(
                "SELECT * FROM mcp_connections WHERE id=?", (connection_id,)
            ).fetchone()
        if row is None:
            raise FileNotFoundError("MCP 连接未登记")
        if row["owner_id"] != owner_id:
            raise PermissionError("该连接不属于当前用户")
        record = dict(row)
        record["args"] = json.loads(record.pop("args_json") or "[]")
        record["enabled"] = bool(record["enabled"])
        return record

    def list(self, owner_id: int) -> list[dict]:
        with _connect(self.database_path) as conn:
            _ensure_tables(conn)
            rows = conn.execute(
                "SELECT id FROM mcp_connections WHERE owner_id=? ORDER BY created_at",
                (owner_id,),
            ).fetchall()
        return [self.get(row["id"], owner_id) for row in rows]

    def _config(self, record: dict) -> dict:
        config = {
            "id": record["id"],
            "transport": record["transport"],
            "endpoint": record.get("endpoint"),
            "command": record.get("command"),
            "args": record.get("args") or [],
            "enabled": record["enabled"],
            "credential": None,
        }
        reference = record.get("credential_ref")
        if reference:
            if self.secret_store is None:
                raise McpUnavailable("认证信息需要工作区凭据存储，而当前尚未配置")
            config["credential"] = self.secret_store.get(record["owner_id"], reference)
        return config

    def _transport(self, record: dict):
        if not record["enabled"]:
            raise McpUnavailable("该连接已停用")
        if self.transport_factory is None:
            raise McpUnavailable(
                "MCP 传输尚未配置：本机服务需由用户自行准备，网站不代为安装或启动"
            )
        transport = self.transport_factory(self._config(record))
        if transport is None:
            raise McpUnavailable(
                "MCP 传输尚未配置：本机服务需由用户自行准备，网站不代为安装或启动"
            )
        return transport

    def _mark(self, connection_id: str, status: str, error: str | None = None,
              tools_version: str | None = None) -> None:
        with _connect(self.database_path) as conn:
            _ensure_tables(conn)
            conn.execute(
                "UPDATE mcp_connections SET status=?, last_error=?, "
                "tools_version=COALESCE(?, tools_version), updated_at=? WHERE id=?",
                (status, error, tools_version, _now(), connection_id),
            )

    # ------------------------------------------------------------- operations

    def test(self, owner_id: int, connection_id: str) -> dict:
        record = self.get(connection_id, owner_id)
        try:
            transport = self._transport(record)
            server = transport.describe() if hasattr(transport, "describe") else {}
            transport.list_tools()
        except McpUnavailable as exc:
            self._mark(connection_id, "unavailable", str(exc))
            return {"connection_id": connection_id, "connected": False,
                    "server": {}, "error": str(exc)}
        except Exception as exc:
            self._mark(connection_id, "error", type(exc).__name__)
            return {"connection_id": connection_id, "connected": False,
                    "server": {}, "error": type(exc).__name__}
        self._mark(connection_id, "connected", None)
        return {"connection_id": connection_id, "connected": True,
                "server": server or {}, "error": None}

    def discover(self, owner_id: int, connection_id: str) -> dict:
        record = self.get(connection_id, owner_id)
        transport = self._transport(record)
        try:
            advertised = transport.list_tools()
        except Exception as exc:
            self._mark(connection_id, "error", type(exc).__name__)
            raise McpUnavailable(f"工具发现失败: {type(exc).__name__}") from exc

        tools = []
        for item in advertised:
            annotations = item.get("annotations") or {}
            declared = bool(annotations)
            tools.append({
                "name": item["name"],
                "description": item.get("description") or "",
                "schema": item.get("inputSchema") or {},
                "writes": not bool(annotations.get("readOnlyHint", False)),
                "billable": bool(annotations.get("billableHint", False)),
                "idempotent": bool(annotations.get("idempotentHint", False)),
                "cancellable": bool(annotations.get("cancellableHint", False)),
                "effects_declared": declared,
            })
        fingerprint = hashlib.sha256(
            json.dumps([record["revision"], sorted(tools, key=lambda t: t["name"])],
                       sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        previous = record.get("tools_version")
        with _connect(self.database_path) as conn:
            _ensure_tables(conn)
            conn.execute("DELETE FROM mcp_tools WHERE connection_id=?", (connection_id,))
            conn.executemany(
                "INSERT INTO mcp_tools (connection_id, name, description, schema_json, "
                "writes, billable, idempotent, cancellable, effects_declared, version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (connection_id, tool["name"], tool["description"],
                     json.dumps(tool["schema"], ensure_ascii=False),
                     int(tool["writes"]), int(tool["billable"]),
                     int(tool["idempotent"]), int(tool["cancellable"]),
                     int(tool["effects_declared"]), fingerprint)
                    for tool in tools
                ],
            )
        self._mark(connection_id, "connected", None, tools_version=fingerprint)
        return {
            "connection_id": connection_id,
            "tools": tools,
            "tools_version": fingerprint,
            "changed": previous is not None and previous != fingerprint,
        }

    def get_tool(self, connection_id: str, owner_id: int, tool_name: str) -> dict | None:
        self.get(connection_id, owner_id)
        with _connect(self.database_path) as conn:
            _ensure_tables(conn)
            row = conn.execute(
                "SELECT * FROM mcp_tools WHERE connection_id=? AND name=?",
                (connection_id, tool_name),
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["schema"] = json.loads(record.pop("schema_json") or "{}")
        for key in ("writes", "billable", "idempotent", "cancellable", "effects_declared"):
            record[key] = bool(record[key])
        return record

    def invoke(self, owner_id: int, connection_id: str, tool_name: str,
               arguments: dict, expected_version: str | None = None) -> dict:
        record = self.get(connection_id, owner_id)
        tool = self.get_tool(connection_id, owner_id, tool_name)
        if tool is None:
            raise McpUnavailable("该工具不在最近一次工具发现结果中")
        if expected_version is not None and expected_version != tool['version']:
            raise McpUnavailable('MCP 工具版本已变化，请重新确认')
        transport = self._transport(record)
        try:
            result = transport.call_tool(tool_name, arguments)
        except Exception as exc:
            self._mark(connection_id, "error", type(exc).__name__)
            if tool["writes"] or tool["billable"]:
                # The external service may already have applied the change.
                raise RuntimeError(
                    f"外部工具执行结果未知，未自动重发: {type(exc).__name__}"
                ) from exc
            raise McpUnavailable(f"外部工具执行失败: {type(exc).__name__}") from exc
        self._mark(connection_id, "connected", None)
        return {
            "result": result if isinstance(result, dict) else {"value": result},
            "effect": "write" if tool["writes"] else "read",
        }

    def list_tools(self, owner_id: int, connection_id: str) -> list[dict]:
        self.get(connection_id, owner_id)
        with _connect(self.database_path) as conn:
            names = [r[0] for r in conn.execute("SELECT name FROM mcp_tools WHERE connection_id=? ORDER BY name", (connection_id,))]
        return [self.get_tool(connection_id, owner_id, name) for name in names]

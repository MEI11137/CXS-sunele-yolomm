"""Agent 侧 SQLite 存储：连接、表结构、事务与通用工具函数。

约定：

* 复用原站点的同一个 SQLite 文件（``yyxz_sqlite.db``），所有 Agent 表统一加
  ``agent_`` 前缀，不改动任何原有表结构；
* 表结构由本模块幂等创建，可重复执行，不覆盖已有数据；
* 连接路径优先取当前 Flask 应用的 ``DATABASE`` 配置，便于测试指向临时库。
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterable, Iterator, List, Optional

SCHEMA_VERSION = 1

_PREPARED_PATHS: set = set()


def now() -> str:
    """统一时间格式（本地时间，秒级），与原表 ``create_time`` 风格一致。"""
    return datetime.now().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def loads(raw: Optional[str], default: Any = None) -> Any:
    if raw is None or raw == "":
        return {} if default is None else default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {} if default is None else default


def current_db_path() -> str:
    """当前应使用的数据库文件。"""
    try:
        from flask import current_app

        configured = current_app.config.get("DATABASE")
        if configured:
            return str(configured)
    except Exception:  # noqa: BLE001 - 无应用上下文时退回全局配置
        pass
    from config import DB_PATH

    return str(DB_PATH)


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    """打开连接并确保 Agent 表已存在。"""
    target = path or current_db_path()
    conn = sqlite3.connect(target, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    ensure_schema(conn, target)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """显式事务；``immediate`` 用于需要串行的写入（如事件序号）。"""
    if immediate:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


@contextmanager
def session_scope(immediate: bool = False) -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute("BEGIN")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
    finally:
        conn.close()


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(row) for row in rows]


def query_all(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    return rows_to_dicts(conn.execute(sql, tuple(params)).fetchall())


def query_one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
    return row_to_dict(conn.execute(sql, tuple(params)).fetchone())


_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS agent_project (
        id TEXT PRIMARY KEY,
        owner_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        requirements_json TEXT NOT NULL DEFAULT '{}',
        origin TEXT NOT NULL DEFAULT 'user',
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_project_owner_name "
    "ON agent_project(owner_id, name)",
    """
    CREATE TABLE IF NOT EXISTS agent_resource (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        project_id TEXT NOT NULL,
        owner_id INTEGER NOT NULL,
        display_name TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1,
        access_policy TEXT NOT NULL DEFAULT 'private',
        modality TEXT,
        source_table TEXT,
        source_id TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_agent_resource_owner "
    "ON agent_resource(owner_id, project_id, kind)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_resource_source "
    "ON agent_resource(source_table, source_id) WHERE source_id IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS agent_model_connection (
        user_id INTEGER PRIMARY KEY,
        provider TEXT NOT NULL,
        api_url TEXT NOT NULL,
        model TEXT NOT NULL,
        key_cipher TEXT NOT NULL,
        key_hint TEXT NOT NULL,
        key_fingerprint TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'unchecked',
        capabilities_json TEXT NOT NULL DEFAULT '{}',
        failure_reason TEXT,
        last_checked_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_credential (
        id TEXT PRIMARY KEY,
        owner_id INTEGER NOT NULL,
        namespace TEXT NOT NULL,
        name TEXT NOT NULL,
        value_cipher TEXT NOT NULL,
        hint TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_credential "
    "ON agent_credential(owner_id, namespace, name)",
    """
    CREATE TABLE IF NOT EXISTS agent_session (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        owner_id INTEGER NOT NULL,
        title TEXT,
        manual_id TEXT,
        manual_version INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_agent_session_owner "
    "ON agent_session(owner_id, project_id)",
    """
    CREATE TABLE IF NOT EXISTS agent_message (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        task_id TEXT,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        refs_json TEXT NOT NULL DEFAULT '[]',
        tool_name TEXT,
        tool_call_id TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_agent_message_session "
    "ON agent_message(session_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS agent_task (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        owner_id INTEGER NOT NULL,
        goal TEXT NOT NULL,
        status TEXT NOT NULL,
        plan_json TEXT,
        plan_version INTEGER NOT NULL DEFAULT 0,
        plan_confirmed_at TEXT,
        steps_json TEXT NOT NULL DEFAULT '[]',
        waiting_for_json TEXT,
        business_refs_json TEXT NOT NULL DEFAULT '[]',
        manual_id TEXT,
        manual_version INTEGER,
        summary TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_agent_task_session ON agent_task(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_agent_task_status ON agent_task(status)",
    """
    CREATE TABLE IF NOT EXISTS agent_event (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        task_id TEXT,
        sequence INTEGER NOT NULL,
        type TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_event_sequence "
    "ON agent_event(session_id, sequence)",
    """
    CREATE TABLE IF NOT EXISTS agent_action (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        owner_id INTEGER NOT NULL,
        tool_name TEXT NOT NULL,
        tool_version TEXT NOT NULL,
        parameters_json TEXT NOT NULL,
        parameters_summary TEXT NOT NULL,
        preview_json TEXT NOT NULL DEFAULT '{}',
        target_projects_json TEXT NOT NULL DEFAULT '[]',
        resource_revisions_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1,
        request_key TEXT NOT NULL UNIQUE,
        claimed_at TEXT,
        decided_at TEXT,
        decided_by INTEGER,
        result_json TEXT,
        error_code TEXT,
        execution_state TEXT NOT NULL DEFAULT 'not_executed',
        operation_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_agent_action_task ON agent_action(task_id, status)",
    """
    CREATE TABLE IF NOT EXISTS agent_operation (
        id TEXT PRIMARY KEY,
        task_id TEXT,
        action_id TEXT,
        tool_name TEXT NOT NULL,
        request_key TEXT NOT NULL,
        operation_id TEXT,
        status TEXT NOT NULL,
        business_refs_json TEXT NOT NULL DEFAULT '[]',
        outputs_json TEXT NOT NULL DEFAULT '{}',
        item_results_json TEXT NOT NULL DEFAULT '[]',
        error_json TEXT,
        is_stub INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_operation_action "
    "ON agent_operation(action_id) WHERE action_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_operation_request "
    "ON agent_operation(request_key)",
    """
    CREATE TABLE IF NOT EXISTS agent_handoff_note (
        task_id TEXT PRIMARY KEY,
        note_json TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        needs_recheck INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_manual (
        id TEXT PRIMARY KEY,
        owner_id INTEGER,
        name TEXT NOT NULL,
        version INTEGER NOT NULL,
        source TEXT NOT NULL,
        summary TEXT,
        keywords TEXT NOT NULL DEFAULT '',
        content_md TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_tool_grant (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        project_id TEXT,
        server_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        tool_version TEXT NOT NULL,
        mode TEXT NOT NULL,
        created_at TEXT NOT NULL,
        revoked_at TEXT
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_tool_grant "
    "ON agent_tool_grant(user_id, server_id, tool_name) "
    "WHERE revoked_at IS NULL",
]


def ensure_schema(conn: sqlite3.Connection, path: Optional[str] = None) -> None:
    """幂等创建 Agent 表。同一路径只执行一次，避免每次连接都跑 DDL。"""
    key = os.path.abspath(path or current_db_path())
    if key in _PREPARED_PATHS:
        return
    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.commit()
    _PREPARED_PATHS.add(key)


def reset_schema_cache() -> None:
    """测试切换数据库时清掉"已建表"缓存。"""
    _PREPARED_PATHS.clear()

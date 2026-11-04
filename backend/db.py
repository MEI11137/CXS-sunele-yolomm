"""SQLite 连接生命周期与建表迁移。

* 每个应用实例在工厂阶段把数据库路径固定进 ``Settings``，请求期间不再读环境变量；
* 每个请求一个连接，存在 ``g`` 上，请求结束关闭；
* 用 ``PRAGMA user_version`` 做迁移版本，只允许向前追加脚本。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import Flask, current_app, g

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    display_name    TEXT    NOT NULL,
    password_hash   TEXT    NOT NULL,
    role            TEXT    NOT NULL CHECK (role IN ('admin', 'member')),
    status          TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    token_version   INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id          TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT    NOT NULL,
    expires_at  INTEGER NOT NULL,
    revoked_at  TEXT,
    client_hint TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id, revoked_at);

CREATE TABLE IF NOT EXISTS allowed_roots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT    NOT NULL UNIQUE,
    label       TEXT    NOT NULL DEFAULT '',
    created_by  INTEGER REFERENCES users(id),
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id            TEXT    PRIMARY KEY,
    owner_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    description   TEXT    NOT NULL DEFAULT '',
    root_path     TEXT,
    root_bound_at TEXT,
    root_bound_by INTEGER REFERENCES users(id),
    status        TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    deleted_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner_id, status, created_at);

CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT    PRIMARY KEY,
    project_id TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id, status, updated_at);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content    TEXT    NOT NULL,
    meta       TEXT    NOT NULL DEFAULT '{}',
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS connections (
    user_id             INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    provider            TEXT    NOT NULL,
    base_url            TEXT    NOT NULL,
    model               TEXT    NOT NULL,
    display_name        TEXT    NOT NULL DEFAULT '',
    key_cipher          TEXT    NOT NULL,
    key_hint            TEXT    NOT NULL,
    key_fingerprint     TEXT    NOT NULL,
    capabilities        TEXT    NOT NULL DEFAULT '{}',
    checked_at          TEXT,
    checked_base_url    TEXT,
    checked_model       TEXT,
    checked_provider    TEXT,
    checked_fingerprint TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT    NOT NULL,
    actor_id  INTEGER,
    action    TEXT    NOT NULL,
    target    TEXT    NOT NULL DEFAULT '',
    detail    TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);
"""

# v2：根目录改为“按账号授予”。迁移只建关系表，**不把已有全局授权自动发给任何账号**，
# 避免升级后所有人默认看到彼此的数据目录；授予必须由管理员或本机 CLI 明确执行。
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS root_grants (
    root_id    INTEGER NOT NULL REFERENCES allowed_roots(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    granted_by INTEGER REFERENCES users(id),
    granted_at TEXT    NOT NULL,
    PRIMARY KEY (root_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_root_grants_user ON root_grants(user_id);
"""

# 只允许向后追加；已发布的脚本不得修改。
MIGRATIONS: tuple[tuple[int, str], ...] = ((1, SCHEMA_V1), (2, SCHEMA_V2))


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, script in MIGRATIONS:
        if version > current:
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()


def get_db() -> sqlite3.Connection:
    """当前请求的连接。路径来自工厂阶段的配置快照。"""
    if "db_conn" not in g:
        settings = current_app.config["HX17"]
        g.db_conn = connect(settings.db_path)
    return g.db_conn


def close_db(_error: BaseException | None = None) -> None:
    conn = g.pop("db_conn", None)
    if conn is not None:
        conn.close()


def init_app(app: Flask) -> None:
    settings = app.config["HX17"]
    conn = connect(settings.db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    app.teardown_appcontext(close_db)

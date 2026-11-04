"""SQLite 连接生命周期与建表迁移。

* 每个应用实例在工厂阶段把数据库路径固定进 ``Settings``，请求期间不再读环境变量；
* 每个请求一个连接，存在 ``g`` 上，请求结束关闭；
* 用 ``PRAGMA user_version`` 做迁移版本，只允许向前追加脚本。
"""

from __future__ import annotations

import logging
import sqlite3
import time
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

# v3：Agent 轮次、事件、动作确认、工具任务、引擎会话映射与用户偏好。
#
# * ``turns`` 上的**部分唯一索引**是“每会话同时只有一个活动轮次”的最终保证：
#   应用层再检查一次只是为了给出可读错误，数据库层不允许并发插入第二轮。
# * ``(session_id, request_id)`` 唯一，浏览器刷新/重复点击不会新建第二轮。
# * ``turn_events`` 只追加，前端用 ``after=seq`` 拉增量；断开后重连不丢状态。
SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    mode       TEXT    NOT NULL DEFAULT 'request'
               CHECK (mode IN ('request', 'assist', 'full')),
    model      TEXT,
    updated_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
    id                     TEXT    PRIMARY KEY,
    session_id             TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    project_id             TEXT    NOT NULL,
    owner_id               INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    request_id             TEXT    NOT NULL,
    kind                   TEXT    NOT NULL DEFAULT 'user'
                           CHECK (kind IN ('user', 'continuation')),
    mode                   TEXT    NOT NULL
                           CHECK (mode IN ('request', 'assist', 'full')),
    provider               TEXT    NOT NULL DEFAULT '',
    model                  TEXT    NOT NULL DEFAULT '',
    connection_fingerprint TEXT    NOT NULL DEFAULT '',
    status                 TEXT    NOT NULL
                           CHECK (status IN ('queued', 'running', 'waiting_action',
                                             'completed', 'failed', 'stopped')),
    stop_requested_at      TEXT,
    error_code             TEXT,
    error_message          TEXT,
    started_at             TEXT,
    finished_at            TEXT,
    created_at             TEXT    NOT NULL,
    updated_at             TEXT    NOT NULL,
    UNIQUE (session_id, request_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_one_active
    ON turns(session_id)
    WHERE status IN ('queued', 'running', 'waiting_action');
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, created_at);

CREATE TABLE IF NOT EXISTS turn_events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id    TEXT    NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    session_id TEXT    NOT NULL,
    type       TEXT    NOT NULL,
    payload    TEXT    NOT NULL DEFAULT '{}',
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turn_events_turn ON turn_events(turn_id, seq);

CREATE TABLE IF NOT EXISTS actions (
    id            TEXT    PRIMARY KEY,
    turn_id       TEXT    NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    session_id    TEXT    NOT NULL,
    project_id    TEXT    NOT NULL,
    owner_id      INTEGER NOT NULL,
    tool          TEXT    NOT NULL,
    risk          TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    args          TEXT    NOT NULL DEFAULT '{}',
    preview       TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL
                  CHECK (status IN ('proposed', 'executing', 'done', 'failed',
                                    'rejected', 'expired')),
    result        TEXT    NOT NULL DEFAULT '{}',
    error_code    TEXT,
    error_message TEXT,
    decided_at    TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_session ON actions(session_id, status);
CREATE INDEX IF NOT EXISTS idx_actions_turn ON actions(turn_id, status);

CREATE TABLE IF NOT EXISTS tool_jobs (
    id            TEXT    PRIMARY KEY,
    turn_id       TEXT,
    session_id    TEXT,
    owner_id      INTEGER NOT NULL,
    tool          TEXT    NOT NULL,
    status        TEXT    NOT NULL
                  CHECK (status IN ('queued', 'running', 'done', 'failed')),
    result        TEXT    NOT NULL DEFAULT '{}',
    error_code    TEXT,
    error_message TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_jobs_turn ON tool_jobs(turn_id);

CREATE TABLE IF NOT EXISTS engine_sessions (
    session_id        TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    engine_session_id TEXT NOT NULL,
    fingerprint       TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
"""

# v4：工具任务记录参数指纹。
#
# 只用 (id, owner, tool) 判断重放不够：同一个 call_id 换了参数也必须冲突，
# 否则一次“重试”可能把另一个操作当成上一次的结果返回。参数以排序后的 JSON 落库，
# 只用于比对，不参与任何执行。
SCHEMA_V4 = """
ALTER TABLE tool_jobs ADD COLUMN args TEXT NOT NULL DEFAULT '{}';
"""

# v5：Native 会话映射必须记住“它建在哪一代引擎存储上”。
#
# ``engine_session_id`` 只在**创建它的那份私有存储目录**里有意义。视觉能力结论变化
# （vision false→true）会让引擎换一个隔离目录，此时旧 ID 在新引擎里并不存在；
# 只比对连接指纹会把旧 ID 直接返回，之后每个请求都打在空气上。
# 因此把目录代次（``storage_key``）一起落库，复用时同时校验“同一代存储”+“实际存在”。
SCHEMA_V5 = """
ALTER TABLE engine_sessions ADD COLUMN storage_key TEXT NOT NULL DEFAULT '';
"""

# 只允许向后追加；已发布的脚本不得修改。
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, SCHEMA_V1),
    (2, SCHEMA_V2),
    (3, SCHEMA_V3),
    (4, SCHEMA_V4),
    (5, SCHEMA_V5),
)


CONNECT_ATTEMPTS = 5
_TRANSIENT = ("readonly", "locked", "unable to open", "disk i/o")


def _is_transient(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(token in text for token in _TRANSIENT)


def _probe_writable(conn: sqlite3.Connection) -> None:
    """确认这条连接真的能写。

    Windows 上有个不常见的坑：数据库文件刚建好、或被安全软件短暂独占时，
    ``sqlite3.connect`` 的可写打开会失败，SQLite 的 Win32 VFS 会**静默回退成只读打开**
    （用户态看不到任何异常，``PRAGMA journal_mode`` 仍返回 ``wal``）。
    这样的连接之后每次写入都报 ``attempt to write a readonly database``，
    且永远不会自己恢复。所以必须在建连时验证，而不是等到业务写入时才炸。

    但探测本身**不能变成新的锁竞争来源**：这里只等 300ms，
    并且把“别人正在写（locked/busy）”与“这条连接只读（readonly）”严格区分——
    前者说明连接可写，直接放行；后者才需要重建连接。
    """
    conn.execute("PRAGMA busy_timeout=300")
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        if "readonly" in text:
            raise
        if "locked" in text or "busy" in text:
            # 只是暂时拿不到写锁：连接本身是可写的，不能因此重连。
            return
        raise
    else:
        conn.execute("ROLLBACK")
    finally:
        conn.execute("PRAGMA busy_timeout=8000")


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接。

    先设 busy_timeout 再切 WAL，最后验证可写；任一步遇到只读/锁冲突就重建连接。
    重试只针对“打开阶段的偶发竞争”，不会把真正的权限问题伪装成成功。
    """
    last: sqlite3.OperationalError | None = None
    for attempt in range(CONNECT_ATTEMPTS):
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(path), timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=8000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            _probe_writable(conn)
            return conn
        except sqlite3.OperationalError as exc:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover - 关闭失败不影响重连
                    pass
            last = exc
            if attempt >= CONNECT_ATTEMPTS - 1 or not _is_transient(exc):
                raise
            logging.getLogger(__name__).warning(
                "数据库连接打开后不可写，重连第 %d 次：%s", attempt + 1, exc
            )
            time.sleep(0.05 * (attempt + 1))
    raise last  # pragma: no cover - 循环内已抛错


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

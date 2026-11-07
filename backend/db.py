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

# v6：业务资产、数据集、模型、识别记录、AI 核查与后台作业。
#
# 设计要点：
# * 所有业务行都带 ``project_id`` + ``owner_id``，查询一律双条件，别人的数据按“不存在”处理；
# * 资产保存**原始字节**的 SHA 与真实类型；图片不是 project_file，单独一张表；
# * ``detections`` / ``jobs`` 上的 ``(project_id, request_key)`` 唯一索引是幂等的最终保证：
#   重试拿到的是同一条记录/同一个作业，而不是第二份计费与第二份产物；
# * ``job_events`` 是**会话级**的：聊天轮次结束后，业务作业仍能通过它把结果推回界面。
SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS assets (
    id          TEXT    PRIMARY KEY,
    project_id  TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id  TEXT,
    kind        TEXT    NOT NULL CHECK (kind IN ('image', 'archive', 'weight', 'document')),
    role        TEXT    NOT NULL DEFAULT 'image'
                CHECK (role IN ('rgb', 'ir', 'edge', 'x', 'image', 'archive', 'weight')),
    origin      TEXT    NOT NULL DEFAULT 'upload'
                CHECK (origin IN ('upload', 'local', 'derived')),
    filename    TEXT    NOT NULL,
    rel_path    TEXT    NOT NULL,
    mime        TEXT    NOT NULL,
    size        INTEGER NOT NULL,
    sha256      TEXT    NOT NULL,
    width       INTEGER,
    height      INTEGER,
    source_path TEXT,
    parent_id   TEXT,
    meta        TEXT    NOT NULL DEFAULT '{}',
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_project ON assets(project_id, owner_id, created_at);
CREATE INDEX IF NOT EXISTS idx_assets_sha ON assets(sha256);

CREATE TABLE IF NOT EXISTS datasets (
    id                 TEXT    PRIMARY KEY,
    project_id         TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name               TEXT    NOT NULL,
    modalities         TEXT    NOT NULL DEFAULT '["rgb"]',
    secondary_modality TEXT,
    classes            TEXT    NOT NULL DEFAULT '[]',
    root_rel           TEXT    NOT NULL,
    source             TEXT    NOT NULL CHECK (source IN ('upload', 'local')),
    source_asset_id    TEXT,
    sample_count       INTEGER NOT NULL DEFAULT 0,
    split_counts       TEXT    NOT NULL DEFAULT '{}',
    status             TEXT    NOT NULL CHECK (status IN ('ready', 'failed')),
    notes              TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL,
    UNIQUE (project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_datasets_project ON datasets(project_id, owner_id, created_at);

CREATE TABLE IF NOT EXISTS dataset_samples (
    id         TEXT    PRIMARY KEY,
    dataset_id TEXT    NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    split      TEXT    NOT NULL,
    stem       TEXT    NOT NULL,
    rgb_rel    TEXT    NOT NULL,
    x_rel      TEXT,
    label_rel  TEXT,
    width      INTEGER,
    height     INTEGER,
    boxes      TEXT    NOT NULL DEFAULT '[]',
    class_ids  TEXT    NOT NULL DEFAULT '[]',
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_dataset ON dataset_samples(dataset_id, split);

CREATE TABLE IF NOT EXISTS models (
    id                 TEXT    PRIMARY KEY,
    project_id         TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name               TEXT    NOT NULL,
    family             TEXT    NOT NULL CHECK (family IN ('yolo', 'yolomm')),
    model_mode         TEXT    NOT NULL CHECK (model_mode IN ('single', 'multimodal')),
    secondary_modality TEXT,
    dataset_id         TEXT,
    weight_rel         TEXT    NOT NULL,
    weight_sha256      TEXT    NOT NULL,
    weight_size        INTEGER NOT NULL DEFAULT 0,
    classes            TEXT    NOT NULL DEFAULT '[]',
    class_map          TEXT    NOT NULL DEFAULT '{}',
    status             TEXT    NOT NULL CHECK (status IN ('imported', 'published', 'failed', 'archived')),
    published_at       TEXT,
    notes              TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL,
    UNIQUE (project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_models_project ON models(project_id, owner_id, status);

CREATE TABLE IF NOT EXISTS detections (
    id             TEXT    PRIMARY KEY,
    project_id     TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id     TEXT,
    turn_id        TEXT,
    job_id         TEXT,
    request_key    TEXT    NOT NULL,
    model_id       TEXT    NOT NULL,
    dataset_id     TEXT,
    rgb_asset_id   TEXT    NOT NULL,
    x_asset_id     TEXT,
    x_origin       TEXT    NOT NULL DEFAULT 'none',
    confidence     REAL    NOT NULL DEFAULT 0.25,
    status         TEXT    NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'paused')),
    boxes          TEXT    NOT NULL DEFAULT '[]',
    class_summary  TEXT    NOT NULL DEFAULT '{}',
    hit_count      INTEGER NOT NULL DEFAULT 0,
    image_rel      TEXT,
    result_rel     TEXT,
    error_code     TEXT,
    error_message  TEXT,
    created_at     TEXT    NOT NULL,
    finished_at    TEXT,
    UNIQUE (project_id, request_key)
);
CREATE INDEX IF NOT EXISTS idx_detections_project ON detections(project_id, owner_id, created_at);
CREATE INDEX IF NOT EXISTS idx_detections_status ON detections(project_id, status, created_at);

CREATE TABLE IF NOT EXISTS ai_reviews (
    id                     TEXT    PRIMARY KEY,
    project_id             TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id               INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    detection_id           TEXT    NOT NULL,
    job_id                 TEXT,
    provider               TEXT    NOT NULL DEFAULT '',
    model                  TEXT    NOT NULL DEFAULT '',
    connection_fingerprint TEXT    NOT NULL DEFAULT '',
    status                 TEXT    NOT NULL CHECK (status IN ('running', 'done', 'failed')),
    verdict                TEXT
                           CHECK (verdict IS NULL OR verdict IN
                                  ('consistent', 'possible_false_positive',
                                   'possible_missed_detection', 'needs_human_review')),
    confidence             REAL,
    summary                TEXT    NOT NULL DEFAULT '',
    findings               TEXT    NOT NULL DEFAULT '{}',
    error_code             TEXT,
    error_message          TEXT,
    created_at             TEXT    NOT NULL,
    finished_at            TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_detection ON ai_reviews(detection_id, created_at);
CREATE INDEX IF NOT EXISTS idx_reviews_project ON ai_reviews(project_id, created_at);

CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT    PRIMARY KEY,
    project_id    TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id    TEXT,
    turn_id       TEXT,
    kind          TEXT    NOT NULL,
    request_key   TEXT    NOT NULL,
    status        TEXT    NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'partial')),
    payload       TEXT    NOT NULL DEFAULT '{}',
    progress      TEXT    NOT NULL DEFAULT '{}',
    result        TEXT    NOT NULL DEFAULT '{}',
    error_code    TEXT,
    error_message TEXT,
    created_at    TEXT    NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    UNIQUE (project_id, request_key)
);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id, owner_id, created_at);

CREATE TABLE IF NOT EXISTS job_events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT    NOT NULL,
    project_id TEXT    NOT NULL,
    owner_id   INTEGER NOT NULL,
    session_id TEXT,
    type       TEXT    NOT NULL,
    payload    TEXT    NOT NULL DEFAULT '{}',
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_events_project ON job_events(project_id, seq);
CREATE INDEX IF NOT EXISTS idx_job_events_session ON job_events(session_id, seq);
"""

# 只允许向后追加；已发布的脚本不得修改。
# v7：轮次记录本轮带的附件资产（图片）。放在 turns 上而不是消息 meta 里，
# 是因为运行器发起 prompt 时必须先拿到“这一轮到底带了哪几张图”，
# 并且附件归属/项目边界要跟着轮次一起可审计。
SCHEMA_V7 = """
ALTER TABLE turns ADD COLUMN attachment_ids TEXT NOT NULL DEFAULT '[]';
"""

# v8：作业与核查的“中断”状态 + 在途核查的原子认领。
#
# 为什么需要重建表：SQLite 不能修改已有 CHECK 约束，而 queued/running 在服务重启后
# 既不是成功也不是业务失败，必须能表达成“结果未知”。用重建而不是把中断塞进 failed，
# 是因为“失败”会被误读成“模型给出了否定结论”。
#
# 认领键选 (detection_id, model, connection_fingerprint)：同一张图、同一个模型、
# 同一份连接配置，在途只允许一条核查。换模型或换连接属于另一次核查，不共享认领。
SCHEMA_V8 = """
ALTER TABLE ai_reviews RENAME TO ai_reviews_v7;
CREATE TABLE ai_reviews (
    id                     TEXT    PRIMARY KEY,
    project_id             TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id               INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    detection_id           TEXT    NOT NULL,
    job_id                 TEXT,
    provider               TEXT    NOT NULL DEFAULT '',
    model                  TEXT    NOT NULL DEFAULT '',
    connection_fingerprint TEXT    NOT NULL DEFAULT '',
    status                 TEXT    NOT NULL CHECK (status IN ('running', 'done', 'failed', 'interrupted')),
    verdict                TEXT
                           CHECK (verdict IS NULL OR verdict IN
                                  ('consistent', 'possible_false_positive',
                                   'possible_missed_detection', 'needs_human_review')),
    confidence             REAL,
    summary                TEXT    NOT NULL DEFAULT '',
    findings               TEXT    NOT NULL DEFAULT '{}',
    error_code             TEXT,
    error_message          TEXT,
    created_at             TEXT    NOT NULL,
    finished_at            TEXT
);
INSERT INTO ai_reviews
    (id, project_id, owner_id, detection_id, job_id, provider, model, connection_fingerprint,
     status, verdict, confidence, summary, findings, error_code, error_message, created_at, finished_at)
SELECT
    id, project_id, owner_id, detection_id, job_id, provider, model, connection_fingerprint,
    status, verdict, confidence, summary, findings, error_code, error_message, created_at, finished_at
FROM ai_reviews_v7;
DROP TABLE ai_reviews_v7;
CREATE INDEX idx_reviews_detection ON ai_reviews(detection_id, created_at);
CREATE INDEX idx_reviews_project ON ai_reviews(project_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reviews_active
    ON ai_reviews(detection_id, model, connection_fingerprint) WHERE status = 'running';

ALTER TABLE jobs RENAME TO jobs_v7;
CREATE TABLE jobs (
    id            TEXT    PRIMARY KEY,
    project_id    TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id    TEXT,
    turn_id       TEXT,
    kind          TEXT    NOT NULL,
    request_key   TEXT    NOT NULL,
    status        TEXT    NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'partial', 'interrupted')),
    payload       TEXT    NOT NULL DEFAULT '{}',
    progress      TEXT    NOT NULL DEFAULT '{}',
    result        TEXT    NOT NULL DEFAULT '{}',
    error_code    TEXT,
    error_message TEXT,
    created_at    TEXT    NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    UNIQUE (project_id, request_key)
);
INSERT INTO jobs
    (id, project_id, owner_id, session_id, turn_id, kind, request_key, status, payload, progress,
     result, error_code, error_message, created_at, started_at, finished_at)
SELECT
    id, project_id, owner_id, session_id, turn_id, kind, request_key, status, payload, progress,
    result, error_code, error_message, created_at, started_at, finished_at
FROM jobs_v7;
DROP TABLE jobs_v7;
CREATE INDEX idx_jobs_project ON jobs(project_id, owner_id, created_at);
"""


# v9：人工复核版本、反馈池与数据集版本列。
#
# * 复核用**版本**而不是一行状态：草稿可反复保存，提交后产生不可变版本；
#   同一记录可以有多次确认/驳回，历史与冲突检查都靠 (detection_id, version)。
# * 驳回不是删除：它是一条 status='rejected' 的版本，记录因此离开待复核队列，
#   需要显式 reopen 才会重新出现。
# * 反馈池只认“已确认且尚未回流的版本”（UNIQUE(detection_id, review_version_id)），
#   版本变了就是另一条反馈，不会把旧结论复用到新图上。
SCHEMA_V9 = """
ALTER TABLE detections ADD COLUMN deleted_at TEXT;
ALTER TABLE detections ADD COLUMN deleted_by INTEGER;
ALTER TABLE detections ADD COLUMN flow_id TEXT;

ALTER TABLE datasets ADD COLUMN parent_dataset_id TEXT;
ALTER TABLE datasets ADD COLUMN kind TEXT NOT NULL DEFAULT 'import';
ALTER TABLE datasets ADD COLUMN seed INTEGER;
ALTER TABLE datasets ADD COLUMN split_strategy TEXT NOT NULL DEFAULT 'source';
ALTER TABLE datasets ADD COLUMN request_key TEXT;
ALTER TABLE datasets ADD COLUMN manifest_rel TEXT;
ALTER TABLE datasets ADD COLUMN export_rel TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_datasets_request
    ON datasets(project_id, request_key) WHERE request_key IS NOT NULL;

ALTER TABLE dataset_samples ADD COLUMN origin_record_id TEXT;
ALTER TABLE dataset_samples ADD COLUMN origin_sample_key TEXT;
ALTER TABLE dataset_samples ADD COLUMN review_version_id TEXT;
ALTER TABLE dataset_samples ADD COLUMN sha256 TEXT;
ALTER TABLE dataset_samples ADD COLUMN pixel_sha256 TEXT;

CREATE TABLE IF NOT EXISTS review_versions (
    id              TEXT    PRIMARY KEY,
    project_id      TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    detection_id    TEXT    NOT NULL,
    version         INTEGER NOT NULL,
    status          TEXT    NOT NULL CHECK (status IN ('draft', 'confirmed', 'rejected')),
    outcome         TEXT    CHECK (outcome IS NULL OR outcome IN ('defect', 'no_defect')),
    boxes           TEXT    NOT NULL DEFAULT '[]',
    class_table     TEXT    NOT NULL DEFAULT '[]',
    image_width     INTEGER NOT NULL DEFAULT 0,
    image_height    INTEGER NOT NULL DEFAULT 0,
    model_id        TEXT,
    dataset_id      TEXT,
    note            TEXT    NOT NULL DEFAULT '',
    reject_reason   TEXT,
    request_key     TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    submitted_at    TEXT,
    UNIQUE (project_id, detection_id, version)
);
CREATE INDEX IF NOT EXISTS idx_review_detection ON review_versions(detection_id, version);
CREATE UNIQUE INDEX IF NOT EXISTS uq_review_draft
    ON review_versions(detection_id, owner_id) WHERE status = 'draft';
CREATE UNIQUE INDEX IF NOT EXISTS uq_review_request
    ON review_versions(project_id, request_key) WHERE request_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS record_feedback (
    id                TEXT    PRIMARY KEY,
    project_id        TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    detection_id      TEXT    NOT NULL,
    review_version_id TEXT    NOT NULL,
    dataset_id        TEXT,
    status            TEXT    NOT NULL CHECK (status IN ('pending', 'reflowed', 'void')),
    note              TEXT    NOT NULL DEFAULT '',
    created_at        TEXT    NOT NULL,
    reflowed_at       TEXT,
    UNIQUE (detection_id, review_version_id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_project ON record_feedback(project_id, status, created_at);
"""

# v10：回流草稿与数据集回收站。草稿在定稿之前不生成 images/labels/ZIP，
# 定稿后才是可训练的数据集版本；删除只打时间戳，不动原目录。
SCHEMA_V10 = """
ALTER TABLE datasets ADD COLUMN deleted_at TEXT;

CREATE TABLE IF NOT EXISTS reflow_drafts (
    id              TEXT    PRIMARY KEY,
    project_id      TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id      TEXT,
    name            TEXT    NOT NULL,
    mode            TEXT    NOT NULL CHECK (mode IN ('revision', 'new')),
    parent_dataset_id TEXT,
    strategy        TEXT    NOT NULL DEFAULT 'even',
    ratios          TEXT    NOT NULL DEFAULT '{}',
    seed            INTEGER NOT NULL DEFAULT 0,
    selections      TEXT    NOT NULL DEFAULT '[]',
    plan            TEXT    NOT NULL DEFAULT '{}',
    status          TEXT    NOT NULL CHECK (status IN ('editing', 'ready', 'failed', 'finalized', 'deleted')),
    error_code      TEXT,
    error_message   TEXT,
    dataset_id      TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    finalized_at    TEXT,
    deleted_at      TEXT,
    UNIQUE (project_id, owner_id, name)
);
CREATE INDEX IF NOT EXISTS idx_reflow_drafts_scope
    ON reflow_drafts(project_id, owner_id, status, created_at);

CREATE TABLE IF NOT EXISTS review_followups (
    id            TEXT    PRIMARY KEY,
    project_id    TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id    TEXT    NOT NULL,
    record_id     TEXT    NOT NULL,
    intent        TEXT    NOT NULL CHECK (intent IN ('notify', 'open_reflow', 'reflow_preview', 'reflow')),
    instruction   TEXT    NOT NULL DEFAULT '',
    payload       TEXT    NOT NULL DEFAULT '{}',
    mode          TEXT    NOT NULL,
    provider      TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    fingerprint   TEXT    NOT NULL,
    status        TEXT    NOT NULL CHECK (status IN ('waiting', 'ready', 'running', 'done', 'cancelled', 'failed')),
    result        TEXT    NOT NULL DEFAULT '{}',
    request_id    TEXT,
    cancel_reason TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    completed_at  TEXT,
    UNIQUE (project_id, owner_id, session_id, record_id, intent)
);
CREATE INDEX IF NOT EXISTS idx_review_followups_scope
    ON review_followups(session_id, status, created_at);
"""

# v11：样本身份的**文件戳**。03 导入的样本在 04 才有 sha256/pixel_sha256 列，
# 老行全为 NULL，匹配器只比这两列就把它当“新图”。这里不预填指纹（那等于编造），
# 只加两个可空列记录“算指纹时文件的 size/mtime”：它们只用于诊断与“要不要顺手刷新”，
# **不是**内容未变的证据——读字节算 sha256 才是（改写文件再 os.utime 还原 mtime 的
# 反例已证明戳可以被伪造）。
SCHEMA_V11 = """
ALTER TABLE dataset_samples ADD COLUMN identity_size INTEGER;
ALTER TABLE dataset_samples ADD COLUMN identity_mtime_ns INTEGER;
"""


# v12：训练运行、单机训练锁，以及模型表补齐“来源/父模型/指标/软删/被取代”。
#
# 为什么重建 models：SQLite 改不了 CHECK 约束，而发布新模型必须能把同项目里
# 被取代的旧模型标成 superseded（而不是 archived——归档是用户主动收起，
# 被取代是“有更新的版本上线了”，两者在界面上要分开说）。
# 其他表没有指向 models 的外键，所以重建只影响本表。
#
# training_runs 与 jobs 的分工：*run* 存训练域字段（参数、日志、PID、父子模型），
# *job* 只做既有的事件流与轮次查询（沿用 WB17-03 的后台任务骨架，不另建一套）。
# 抢锁先于建 run：没有锁就不产生“训练中”的记录，避免重启后看似有训练在跑。
SCHEMA_V12 = """
ALTER TABLE models RENAME TO models_v11;
CREATE TABLE models (
    id                 TEXT    PRIMARY KEY,
    project_id         TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name               TEXT    NOT NULL,
    family             TEXT    NOT NULL CHECK (family IN ('yolo', 'yolomm')),
    model_mode         TEXT    NOT NULL CHECK (model_mode IN ('single', 'multimodal')),
    secondary_modality TEXT,
    dataset_id         TEXT,
    weight_rel         TEXT    NOT NULL,
    weight_sha256      TEXT    NOT NULL,
    weight_size        INTEGER NOT NULL DEFAULT 0,
    classes            TEXT    NOT NULL DEFAULT '[]',
    class_map          TEXT    NOT NULL DEFAULT '{}',
    status             TEXT    NOT NULL CHECK (status IN
                           ('imported', 'published', 'superseded', 'failed', 'archived')),
    published_at       TEXT,
    notes              TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL,
    source             TEXT    NOT NULL DEFAULT 'imported'
                       CHECK (source IN ('imported', 'trained')),
    parent_model_id    TEXT,
    training_run_id    TEXT,
    metrics            TEXT    NOT NULL DEFAULT '{}',
    deleted_at         TEXT,
    UNIQUE (project_id, name)
);
INSERT INTO models
    (id, project_id, owner_id, name, family, model_mode, secondary_modality, dataset_id,
     weight_rel, weight_sha256, weight_size, classes, class_map, status, published_at,
     notes, created_at, source)
SELECT
    id, project_id, owner_id, name, family, model_mode, secondary_modality, dataset_id,
    weight_rel, weight_sha256, weight_size, classes, class_map, status, published_at,
    notes, created_at, 'imported'
FROM models_v11;
DROP TABLE models_v11;
CREATE INDEX idx_models_project ON models(project_id, owner_id, status);

CREATE TABLE IF NOT EXISTS training_runs (
    id             TEXT    PRIMARY KEY,
    project_id     TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id         TEXT,
    dataset_id     TEXT    NOT NULL,
    parent_model_id TEXT,
    base_weight_rel TEXT   NOT NULL DEFAULT '',
    name           TEXT    NOT NULL,
    status         TEXT    NOT NULL CHECK (status IN
                       ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted')),
    params         TEXT    NOT NULL DEFAULT '{}',
    metrics        TEXT    NOT NULL DEFAULT '{}',
    conditions     TEXT    NOT NULL DEFAULT '{}',
    run_dir_rel    TEXT    NOT NULL DEFAULT '',
    log_rel        TEXT    NOT NULL DEFAULT '',
    pid            INTEGER,
    pid_started_at REAL,
    output_model_id TEXT,
    error_code     TEXT,
    error_message  TEXT,
    request_key    TEXT    NOT NULL,
    session_id     TEXT,
    turn_id        TEXT,
    created_at     TEXT    NOT NULL,
    started_at     TEXT,
    finished_at    TEXT,
    UNIQUE (project_id, request_key)
);
CREATE INDEX IF NOT EXISTS idx_training_project
    ON training_runs(project_id, owner_id, created_at);

CREATE TABLE IF NOT EXISTS training_locks (
    id           TEXT    PRIMARY KEY,
    project_id   TEXT    NOT NULL,
    owner_id     INTEGER NOT NULL,
    run_id       TEXT    NOT NULL,
    pid          INTEGER,
    acquired_at  TEXT    NOT NULL,
    heartbeat_at TEXT
);
"""


# v13：资料库（项目内文档 + 分页原文）与手册（内置 + 个人版本）。
#
# * 文档属于**项目 + 所有者**：跨项目读不到，与管理员的全局知识库不是一回事。
# * 页面文本单独一张表：检索按页命中，读原文也按页取，避免一次把整本书塞进上下文。
# * 手册有版本：编辑产生新版本，可显式选择启用哪一版；内置手册（project_id/owner_id 为
#   NULL 或 builtin=1）只读，个人手册可编辑。手册内容只是**说明文字**，永远不会
#   被当成权限来源——工具与目录边界只由服务端策略决定。
SCHEMA_V13 = """
CREATE TABLE IF NOT EXISTS documents (
    id            TEXT    PRIMARY KEY,
    project_id    TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    filename      TEXT    NOT NULL,
    rel_path      TEXT    NOT NULL,
    kind          TEXT    NOT NULL CHECK (kind IN ('pdf', 'docx', 'txt', 'md')),
    size          INTEGER NOT NULL DEFAULT 0,
    sha256        TEXT    NOT NULL DEFAULT '',
    page_count    INTEGER NOT NULL DEFAULT 0,
    char_count    INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL CHECK (status IN ('ready', 'failed')),
    error_code    TEXT,
    error_message TEXT,
    created_at    TEXT    NOT NULL,
    deleted_at    TEXT,
    UNIQUE (project_id, owner_id, name)
);
CREATE INDEX IF NOT EXISTS idx_documents_project
    ON documents(project_id, owner_id, created_at);

CREATE TABLE IF NOT EXISTS document_pages (
    document_id TEXT    NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no     INTEGER NOT NULL,
    text        TEXT    NOT NULL,
    PRIMARY KEY (document_id, page_no)
);

CREATE TABLE IF NOT EXISTS manuals (
    id                TEXT    PRIMARY KEY,
    project_id        TEXT,
    owner_id          INTEGER,
    name              TEXT    NOT NULL,
    description       TEXT    NOT NULL DEFAULT '',
    builtin           INTEGER NOT NULL DEFAULT 0,
    active_version_id TEXT,
    created_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_manuals_scope ON manuals(project_id, owner_id);

CREATE TABLE IF NOT EXISTS manual_versions (
    id         TEXT    PRIMARY KEY,
    manual_id  TEXT    NOT NULL REFERENCES manuals(id) ON DELETE CASCADE,
    version    INTEGER NOT NULL,
    content    TEXT    NOT NULL,
    note       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL,
    created_by INTEGER,
    UNIQUE (manual_id, version)
);
"""


# v14：外部 MCP。三层分离是有意的——
#   mcp_servers 存配置（凭据只存密文与掩码），
#   mcp_tools   存发现到的工具与**逐项授权**（默认 pending，不默认放行），
#   mcp_calls   存每次真实调用的审计（谁、什么时候、成功失败）。
SCHEMA_V14 = """
CREATE TABLE IF NOT EXISTS mcp_servers (
    id                  TEXT    PRIMARY KEY,
    project_id          TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name                TEXT    NOT NULL,
    transport           TEXT    NOT NULL CHECK (transport IN ('stdio', 'http')),
    command             TEXT    NOT NULL DEFAULT '',
    args                TEXT    NOT NULL DEFAULT '[]',
    cwd_rel             TEXT    NOT NULL DEFAULT '',
    url                 TEXT    NOT NULL DEFAULT '',
    secrets_cipher      TEXT    NOT NULL DEFAULT '',
    secret_hints        TEXT    NOT NULL DEFAULT '{}',
    enabled             INTEGER NOT NULL DEFAULT 0,
    auto_allow_read_only INTEGER NOT NULL DEFAULT 0,
    status              TEXT    NOT NULL DEFAULT 'unknown'
                                CHECK (status IN ('unknown', 'connected', 'error', 'disabled')),
    last_error          TEXT,
    last_seen_at        TEXT,
    created_at          TEXT    NOT NULL,
    deleted_at          TEXT,
    UNIQUE (project_id, owner_id, name)
);

CREATE TABLE IF NOT EXISTS mcp_tools (
    server_id     TEXT    NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    title         TEXT    NOT NULL DEFAULT '',
    description   TEXT    NOT NULL DEFAULT '',
    schema        TEXT    NOT NULL DEFAULT '{}',
    read_only     INTEGER NOT NULL DEFAULT 0,
    authorization TEXT    NOT NULL DEFAULT 'pending'
                          CHECK (authorization IN ('pending', 'allowed', 'denied')),
    discovered_at TEXT    NOT NULL,
    PRIMARY KEY (server_id, name)
);

CREATE TABLE IF NOT EXISTS mcp_calls (
    id          TEXT    PRIMARY KEY,
    server_id   TEXT    NOT NULL,
    tool_name   TEXT    NOT NULL,
    actor_id    INTEGER NOT NULL,
    turn_id     TEXT,
    request_key TEXT,
    ok          INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    result      TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcp_calls_server ON mcp_calls(server_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_calls_key
    ON mcp_calls(server_id, request_key) WHERE request_key IS NOT NULL;
"""


SCHEMA_V15 = """
ALTER TABLE projects ADD COLUMN requirements TEXT NOT NULL DEFAULT '';
"""

SCHEMA_V16 = """
-- 训练分派：YOLO / YOLOMM 按“真实数据模态 + 父模型”决定，记录进 run 行，
-- 供产物登记（create_trained）写 models.family / model_mode / secondary_modality。
ALTER TABLE training_runs ADD COLUMN family TEXT NOT NULL DEFAULT 'yolo';
ALTER TABLE training_runs ADD COLUMN secondary_modality TEXT;
"""


SCHEMA_V17 = """
-- 训练启动交接：'preparing'（准备/校验中，可取消）→ 'launching'（已认领启动权，
-- 进程即将或已经起来，取消不能提前宣称成功）。NULL 表示没有在途的启动交接。
ALTER TABLE training_runs ADD COLUMN launch_state TEXT;
"""


SCHEMA_V18 = """
-- 数据集内容指纹：同一项目/所有者内，内容+模态+类别+标注+划分相同就不再建第二份。
-- 只按内容算（sha/pixel_sha/boxes/class_ids/划分），不含名称、ZIP 时间戳、绝对路径。
ALTER TABLE datasets ADD COLUMN content_fingerprint TEXT;
DROP INDEX IF EXISTS idx_datasets_fingerprint;
CREATE UNIQUE INDEX IF NOT EXISTS idx_datasets_fingerprint
    ON datasets(project_id, owner_id, content_fingerprint)
    WHERE content_fingerprint IS NOT NULL AND deleted_at IS NULL;
"""


SCHEMA_V20 = """
-- 个人手册的删除改成软删：删除只是从列表/读取里隐藏，内容仍在库里，
-- 可以恢复；内置手册永远只读，不走这条路。
ALTER TABLE manuals ADD COLUMN deleted_at TEXT;
"""


SCHEMA_V21 = """
-- 双模态复核：主模态与辅模态各自一套标注，各用各的原图像素坐标。
-- boxes / image_width / image_height 继续表示**参考模态**（记录的主图）的标注与尺寸；
-- x_boxes / x_image_width / x_image_height 表示**辅模态**（如 IR）的标注与尺寸。
-- 旧记录这三列为 NULL，读取时按“没有辅模态标注”处理，不改变原行为。
ALTER TABLE review_versions ADD COLUMN x_boxes TEXT;
ALTER TABLE review_versions ADD COLUMN x_image_width INTEGER;
ALTER TABLE review_versions ADD COLUMN x_image_height INTEGER;
-- 记录这两套坐标分别属于哪个模态，导出/训练据此判断怎么消费，禁止隐式互换。
ALTER TABLE review_versions ADD COLUMN reference_modality TEXT;
ALTER TABLE review_versions ADD COLUMN secondary_modality TEXT;
-- 数据集显式记录“标注参考模态”：训练读取 images/ 与 labels/ 时以它为准，
-- 不再靠目录名猜。
ALTER TABLE datasets ADD COLUMN reference_modality TEXT;
-- 辅模态标注文件（如 labels_ir/<split>/<stem>.txt）：与 labels/ 分开存，
-- 和 datasets/<id>/manifest.json 里的 reference/secondary 信息一起可追溯。
ALTER TABLE dataset_samples ADD COLUMN labels_ir_rel TEXT;
"""


MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, SCHEMA_V1),
    (2, SCHEMA_V2),
    (3, SCHEMA_V3),
    (4, SCHEMA_V4),
    (5, SCHEMA_V5),
    (6, SCHEMA_V6),
    (7, SCHEMA_V7),
    (8, SCHEMA_V8),
    (9, SCHEMA_V9),
    (10, SCHEMA_V10),
    (11, SCHEMA_V11),
    (12, SCHEMA_V12),
    (13, SCHEMA_V13),
    (14, SCHEMA_V14),
    (15, SCHEMA_V15),
    (16, SCHEMA_V16),
    (17, SCHEMA_V17),
    (18, SCHEMA_V18),
    # v19：同一份索引加上“回收站不参与”，让已迁移过的库也换掉旧谓词。
    (
        19,
        "DROP INDEX IF EXISTS idx_datasets_fingerprint;"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_datasets_fingerprint"
        " ON datasets(project_id, owner_id, content_fingerprint)"
        " WHERE content_fingerprint IS NOT NULL AND deleted_at IS NULL;",
    ),
    (20, SCHEMA_V20),
    (21, SCHEMA_V21),
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

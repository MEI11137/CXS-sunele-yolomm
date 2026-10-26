"""原页面资源授权的测试夹具（INT-03 / INT-04）。

数据集、模型这些页面路由不再用"管理员 / 普通用户"二元判断，而是按**真实归属**
授权。归属来自服务端库：``agent_resource`` 索引 + 业务行推推导，所以这些用例
必须给 app 一个临时库，里面既要有账号（角色决定管理员），也要有资源归属。

以前这些用例挂在 ``admin_required`` 上，只要 monkeypatch ``is_admin`` 就行；
现在授权要读归属，光换角色开关无法表达"这个资源是谁的"，于是统一在这里准备，
同时避免用例读到仓库里真实的开发库。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 用例里常用的三个身份：1 = 管理员，7 / 8 = 普通用户
DEFAULT_ACCOUNTS: Dict[int, int] = {1: 1, 7: 0, 8: 0}


def resource(
    source_table: str,
    source_id: str,
    owner_id: int,
    *,
    kind: Optional[str] = None,
    access_policy: str = "private",
    project_id: str = "proj-guard",
    revision: int = 1,
) -> Tuple[str, Dict[str, Any]]:
    """声明一条"业务行 → 归属"的登记（kind 默认由 source_table 反推）。"""
    from agent.resources import KIND_BY_SOURCE_TABLE

    return (
        source_table,
        {
            "kind": kind or KIND_BY_SOURCE_TABLE.get(source_table, source_table),
            "source_id": source_id,
            "owner_id": owner_id,
            "access_policy": access_policy,
            "project_id": project_id,
            "revision": revision,
        },
    )


def install(
    app,
    db_path,
    *,
    accounts: Optional[Dict[int, int]] = None,
    resources: Iterable[Tuple[str, Dict[str, Any]]] = (),
) -> str:
    """把 app 指向临时库，写入账号与资源归属，返回库路径。"""
    path = str(db_path)
    app.config["DATABASE"] = path

    from agent.store import connect  # 打开时会顺带建好 agent_* 表

    conn = connect(path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " username VARCHAR(50) NOT NULL UNIQUE,"
            " password VARCHAR(100) NOT NULL,"
            " real_name VARCHAR(50), phone VARCHAR(20), email VARCHAR(100),"
            " avatar_bucket VARCHAR(50), avatar_object_key VARCHAR(255),"
            " role INTEGER NOT NULL DEFAULT 0,"
            " status INTEGER NOT NULL DEFAULT 1,"
            " create_time TIMESTAMP, update_time TIMESTAMP)"
        )
        for user_id, role in (accounts or DEFAULT_ACCOUNTS).items():
            conn.execute(
                "INSERT OR REPLACE INTO user "
                "(id, username, password, role, status) VALUES (?, ?, ?, ?, 1)",
                (int(user_id), f"user{user_id}", "x", int(role)),
            )
        for index, (source_table, payload) in enumerate(_iter(resources)):
            conn.execute(
                "INSERT OR REPLACE INTO agent_resource "
                "(id, kind, project_id, owner_id, display_name, revision, "
                " access_policy, source_table, source_id, metadata_json, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)",
                (
                    f"res-{index}",
                    payload["kind"],
                    payload["project_id"],
                    int(payload["owner_id"]),
                    payload["source_id"],
                    int(payload["revision"]),
                    payload["access_policy"],
                    source_table,
                    payload["source_id"],
                    "2026-01-01T00:00:00",
                    "2026-01-01T00:00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def seed_rows(db_path: str, statements: Iterable[Tuple[str, tuple]]) -> None:
    """写业务行（可选）：某些用例需要业务表里真有这一行才能推导归属。"""
    conn = sqlite3.connect(str(db_path))
    try:
        for sql, params in statements:
            conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _iter(items: Iterable[Any]) -> List[Any]:
    return list(items)

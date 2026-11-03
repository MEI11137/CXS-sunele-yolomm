"""隐藏旧版本误入库的海巡引擎上下文压缩摘要（B）。

旧版本的引擎监听把引擎私有的上下文压缩消息当成了助手回答，写进了
``agent_message``。原文保留用于审计，只给这些消息追加结构化标记
``{"type": "internal_context"}``，再在用户消息 API 与前端映射中排除。

判断依据是引擎私有库 ``opencode.db`` 里 message 的结构化 compaction 元数据，
消息 ID 用与保存时完全相同的 ``oc_ + sha256(site_session + remote_message_id)[:28]``
推导；不扫描用户文件，也不按正文关键词猜测。

查询刻意不缓存：引擎使用 WAL，只按主库 mtime/size 缓存会漏掉尚在
``-wal`` 中的新消息。每次读取都直接查私有库（只读），并按 role=assistant
过滤，避免把非助手记录误标记。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from agent import engine_messages
from agent.store import connect, current_db_path, dumps, loads

MARKER = "internal_context"


def _app_config(key: str) -> Any:
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            return current_app.config.get(key)
    except Exception:  # noqa: BLE001 - 无应用上下文时退回环境变量/全局配置
        pass
    return None


def _absolute(path: Any) -> Path:
    """统一转成绝对路径；引擎状态目录不能依赖进程当前目录。"""
    return Path(str(path)).expanduser().resolve()


def engine_state_root() -> Optional[Path]:
    """引擎私有状态根目录；与 HxEngineRuntime._engine 使用同一份配置。"""
    root = _app_config("HX_ENGINE_STATE_DIR") or os.environ.get("HX_ENGINE_STATE_DIR")
    if root:
        return _absolute(root)
    temp = _app_config("TEMP_DIR")
    if not temp:
        try:
            from config import TEMP_DIR as configured_temp

            temp = configured_temp
        except Exception:  # noqa: BLE001
            temp = None
    return _absolute(Path(str(temp)) / "hxengine") if temp else None


def _database_path() -> str:
    """与 HxEngineRuntime._engine 完全相同的原字符串。

    不能先 resolve 再转 posix：Windows 下 ``E:\\a\\b`` 与 ``E:/a/b`` 的
    sha256 不同，会导致按会话找不到引擎私有库、旧压缩摘要漏标。
    """
    configured = _app_config("DATABASE")
    if configured is not None:
        return str(configured)
    return str(current_db_path())


def database_key() -> str:
    """引擎状态目录的库指纹；与 _engine 共用同一算法，避免路径写法分歧。"""
    return hashlib.sha256(_database_path().encode()).hexdigest()[:12]


def engine_db_path(owner_id: Any, session_id: str) -> Optional[Path]:
    """站点库 + 用户 + 会话对应的引擎私有库文件（绝对路径）。"""
    root = engine_state_root()
    if root is None:
        return None
    try:
        db_key = database_key()
        session_key = hashlib.sha256(str(session_id).encode()).hexdigest()[:24]
    except (OSError, ValueError):
        return None
    return (root / db_key / str(owner_id) / session_key / "data" / "opencode" / "opencode.db").resolve()


def _read_engine_message_ids(path: Path) -> Set[str]:
    """只读引擎库，找出 role=assistant 且带结构化 compaction 元数据的消息 id。"""
    ids: Set[str] = set()
    conn = None
    try:
        conn = sqlite3.connect(f"{_absolute(path).as_uri()}?mode=ro", uri=True, timeout=2.0)
        for row in conn.execute("SELECT id, data FROM message"):
            message_id = str(row[0] or "")
            if not message_id:
                continue
            try:
                info = json.loads(row[1] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(info, dict) or info.get("role") != "assistant":
                continue
            if engine_messages.is_internal_info(info):
                ids.add(message_id)
    except (OSError, sqlite3.Error, ValueError):
        ids = set()
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    return ids


def _site_message_ids(engine_ids: Set[str], session_id: str) -> Set[str]:
    prefix = str(session_id)
    return {
        "oc_" + hashlib.sha256((prefix + message_id).encode()).hexdigest()[:28]
        for message_id in engine_ids
    }


def _owner_id(session_id: str) -> Optional[int]:
    conn = connect()
    try:
        row = conn.execute("SELECT owner_id FROM agent_session WHERE id = ?", (session_id,)).fetchone()
        return int(row["owner_id"]) if row is not None else None
    except (sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        conn.close()


def internal_message_ids(session_id: str) -> Set[str]:
    """该会话里属于引擎内部压缩的站点消息 id 集合。"""
    owner_id = _owner_id(session_id)
    if owner_id is None:
        return set()
    path = engine_db_path(owner_id, session_id)
    if path is None or not path.is_file():
        return set()
    return _site_message_ids(_read_engine_message_ids(path), session_id)


def internal_refs_sql(alias: str = "agent_message") -> str:
    """SQL 条件：refs_json 不含内部标记；NULL/字符串/非法旧 JSON 都不报错。"""
    refs = f"{alias}.refs_json"
    return (
        "NOT EXISTS (SELECT 1 FROM json_each("
        f"CASE WHEN json_valid({refs}) THEN {refs} ELSE '[]' END) AS internal_ref "
        # json_each 的 type 列直接给出元素类型；对 ["string"] 不能用 json_type(value)，
        # 那会对字符串值本身再解析一次 JSON 并报 malformed JSON。
        "WHERE internal_ref.type = 'object' "
        "AND json_extract(internal_ref.value, '$.type') = ?)"
    )


def internal_id_clause(
    internal_ids: Optional[Set[str]], alias: str = "agent_message", chunk: int = 400
) -> Tuple[str, List[Any]]:
    """SQL 条件 + 参数：排除精确的 oc_ 内部消息 id；按 SQLite 变量上限分块。"""
    ids = sorted(str(item) for item in (internal_ids or set()) if item)
    if not ids:
        return "", []
    parts: List[str] = []
    params: List[Any] = []
    for start in range(0, len(ids), max(1, int(chunk))):
        batch = ids[start:start + chunk]
        parts.append(f"{alias}.id NOT IN (" + ",".join("?" * len(batch)) + ")")
        params.extend(batch)
    return " AND " + " AND ".join(parts), params


def refs_are_internal(refs: Any) -> bool:
    return any(isinstance(ref, dict) and ref.get("type") == MARKER for ref in refs or [])


def row_is_internal(row: Dict[str, Any], internal_ids: Optional[Set[str]] = None) -> bool:
    """结构标记与引擎库元数据都只用来隐藏展示，不改写正文。"""
    row = row or {}
    refs = row.get("refs")
    if refs is None:
        refs = loads(row.get("refs_json"), [])
    if refs_are_internal(refs):
        return True
    return bool(internal_ids and row.get("id") in internal_ids)


def filter_messages(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """过滤内部压缩消息；不改变其余消息的顺序。"""
    rows = list(rows or [])
    if not rows:
        return []
    session_id = rows[0].get("session_id")
    internal_ids = internal_message_ids(session_id) if session_id else set()
    return [row for row in rows if not row_is_internal(row, internal_ids)]


def backfill_session(session_id: str) -> Dict[str, Any]:
    """给一个会话里已存在的内部摘要补标记，幂等、只改 refs_json。

    供宿主在备份真实数据库后按会话调用；不会自动修改运行中的数据库。
    """
    result = {"session_id": session_id, "engine_internal": 0, "matched": 0, "marked": 0}
    owner_id = _owner_id(session_id)
    if owner_id is None:
        return result
    path = engine_db_path(owner_id, session_id)
    if path is None or not path.is_file():
        return result
    engine_ids = _read_engine_message_ids(path)
    site_ids = _site_message_ids(engine_ids, session_id)
    if not site_ids:
        return result
    conn = connect()
    try:
        for message_id in site_ids:
            row = conn.execute(
                "SELECT refs_json FROM agent_message WHERE id = ? AND session_id = ?",
                (message_id, session_id),
            ).fetchone()
            if row is None:
                continue
            result["matched"] += 1
            refs = loads(row["refs_json"], []) or []
            if not isinstance(refs, list):
                refs = []
            if refs_are_internal(refs):
                continue
            conn.execute(
                "UPDATE agent_message SET refs_json = ? WHERE id = ? AND session_id = ?",
                (dumps([*refs, {"type": MARKER}]), message_id, session_id),
            )
            result["marked"] += 1
        conn.commit()
    finally:
        conn.close()
    # 只报“本会话站点里真实存在的内部消息数”，不把引擎库中未保存进站点的压缩消息
    # 误报成历史摘要条数。
    result["engine_internal"] = result["matched"]
    return result


def backfill_all_sessions(limit: int = 200) -> List[Dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id FROM agent_session ORDER BY updated_at DESC LIMIT ?", (int(limit),)
        ).fetchall()
    finally:
        conn.close()
    return [backfill_session(row["id"]) for row in rows]


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="给旧版本误入库的引擎上下文压缩摘要补内部标记")
    parser.add_argument("--session", action="append", default=[], help="只处理指定会话 id，可重复")
    parser.add_argument("--all", action="store_true", help="处理最近的会话")
    parser.add_argument("--limit", type=int, default=200, help="--all 时最多处理的会话数")
    args = parser.parse_args(argv)
    if args.session:
        results = [backfill_session(session_id) for session_id in args.session]
    elif args.all:
        results = backfill_all_sessions(args.limit)
    else:
        parser.print_help()
        return 2
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
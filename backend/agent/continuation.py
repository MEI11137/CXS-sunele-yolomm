"""确认动作后的续跑：approve 路由与轮次收尾共用同一套幂等逻辑。

设计来源（真实缺陷）：
* 用户在助手原轮次仍输出时很快批准动作，approve 路由看到 ``active_for_session``
  非空就放弃续跑；原轮次收尾时又只处理人工复核接力，导致“动作执行成功但助手
  没有任何回复”。
* 续跑的 prompt 不能按“会话里最后一个已完成动作”取结果，否则并发/延迟时
  A 的续跑会读到 B 的结果。会话里的 ``request_id`` 已经编码了动作身份
  (``resume_<action_id>``)，必须按它精确回读。

这里不新增队列表：``turns`` 上的 ``UNIQUE(session_id, request_id)`` 已经是幂等键；
调用方（approve 路由、runner 收尾）都调同一个 helper，谁先到谁创建，另一边拿到
已有轮次。
"""

from __future__ import annotations

import logging
from typing import Callable

from backend.agent import identity, store

RESUME_PREFIX = "resume_"
logger = logging.getLogger(__name__)


def resume_request_id(action_id: str) -> str:
    return f"{RESUME_PREFIX}{action_id}"[:64]


def action_for_resume(conn, turn):
    """按续跑轮次的 request_id 精确找它对应的动作，并校验归属/状态。

    返回 None 表示：不是动作续跑、动作不存在、动作已取消/失败、或轮次与动作
    的会话/所有者不一致。调用方必须把它当成“明确失效”，不能回退到会话最新动作。
    """
    request_id = str(turn["request_id"] or "")
    if not request_id.startswith(RESUME_PREFIX):
        return None
    action_id = request_id[len(RESUME_PREFIX) :]
    if not action_id:
        return None
    row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    if row is None:
        return None
    if str(row["status"]) != "done":
        return None
    if str(row["session_id"]) != str(turn["session_id"]):
        return None
    if int(row["owner_id"]) != int(turn["owner_id"]):
        return None
    return row


def _latest_turn(conn, session_id: str):
    return conn.execute(
        "SELECT * FROM turns WHERE session_id = ? ORDER BY rowid DESC LIMIT 1",
        (str(session_id),),
    ).fetchone()


def start_action_continuation(conn, action, start_turn: Callable[[str], None]):
    """为已完成动作创建（或复用）续跑轮次；不满足前置条件返回 None。

    前置条件与页面/运行时保持一致：会话与项目仍在、账号连接仍在、原轮次正常
    completed、同一会话没有活动轮次、也没有比原轮次更新的用户目标。幂等键固定为
    ``resume_<action_id>``，approve 路由与收尾扫描并发时只会有一轮。
    """
    try:
        # 调用方可能持有执行前的旧 row（approve 路由就是），状态以库内最新为准。
        fresh = store.get_action(conn, str(action["id"]))
        if fresh is None:
            return None
        action = fresh
        if str(action["status"]) != "done":
            return None
        user_id = int(action["owner_id"])
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None or str(user["status"]) != "active":
            return None
        session = conn.execute(
            "SELECT * FROM sessions WHERE id = ? AND owner_id = ?",
            (str(action["session_id"]), user_id),
        ).fetchone()
        if session is None or str(session["status"]) != "active":
            return None
        project = conn.execute(
            "SELECT * FROM projects WHERE id = ? AND owner_id = ?",
            (str(action["project_id"]), user_id),
        ).fetchone()
        if project is None or str(project["status"]) != "active":
            return None
        parent = store.get(conn, str(action["turn_id"]))
        if parent is None or str(parent["status"]) != "completed":
            return None
        # 停止中的原轮次不能补旧工作。
        if parent["stop_requested_at"]:
            return None
        connection = identity.connection_for(conn, user_id)
        if connection is None:
            return None
        # 不能拿用户后来换的新连接去续旧动作：必须仍是原轮次发起时的凭据/模型快照。
        if str(parent["connection_fingerprint"]) != str(identity.connection_fingerprint(connection)):
            return None
        request_id = resume_request_id(str(action["id"]))
        existing = store.by_request(conn, str(session["id"]), request_id)
        if existing is not None:
            return store.turn_public(existing)
        if store.active_for_session(conn, str(session["id"])) is not None:
            return None
        # 原轮次之后如果用户已经开了新的非续跑目标（哪怕 B 的续跑又排在最后），
        # 也不能把 A 的旧续跑插到 B 的新目标后面。
        parent_seq = conn.execute(
            "SELECT rowid AS seq FROM turns WHERE id = ?", (str(parent["id"]),)
        ).fetchone()["seq"]
        newer_goal = conn.execute(
            "SELECT 1 FROM turns WHERE session_id = ? AND rowid > ? AND kind != 'continuation' LIMIT 1",
            (str(session["id"]), int(parent_seq)),
        ).fetchone()
        if newer_goal is not None:
            return None
        turn, created = store.create(
            conn,
            session_id=str(session["id"]),
            project_id=str(project["id"]),
            owner_id=user_id,
            request_id=request_id,
            mode=identity.effective_mode(conn, user_id),
            provider=str(connection["provider"]),
            model=identity.effective_model(conn, user_id, connection),
            fingerprint=identity.connection_fingerprint(connection),
            kind="continuation",
        )
        if not created:
            return store.turn_public(turn)
        start_turn(str(turn["id"]))
        return store.turn_public(turn)
    except Exception:  # noqa: BLE001 - 收尾路径绝不能拖着主流程崩
        logger.exception("创建动作续跑失败 action=%s", action["id"] if "id" in action.keys() else "?")
        return None


def settle_action_continuations(conn, turn, start_turn: Callable[[str], None]) -> int:
    """轮次正常收尾后，把它（或它续跑的原轮次）里已执行但未续答的动作接上。

    只扫描刚结束轮次所属的那一轮动作，不扫会话历史。停止/失败轮次不补旧工作。
    """
    if turn is None or str(turn["status"]) != "completed":
        return 0
    parent_id = str(turn["id"])
    request_id = str(turn["request_id"] or "")
    if str(turn["kind"]) == "continuation" and request_id.startswith(RESUME_PREFIX):
        action_id = request_id[len(RESUME_PREFIX) :]
        if action_id:
            row = conn.execute("SELECT turn_id FROM actions WHERE id = ?", (action_id,)).fetchone()
            if row is not None:
                parent_id = str(row["turn_id"])
    rows = conn.execute(
        "SELECT * FROM actions WHERE turn_id = ? AND status = 'done' ORDER BY rowid",
        (parent_id,),
    ).fetchall()
    started = 0
    for action in rows:
        request_id = resume_request_id(str(action["id"]))
        if store.by_request(conn, str(action["session_id"]), request_id) is not None:
            continue
        result = start_action_continuation(conn, action, start_turn)
        if result is not None:
            started += 1
    return started
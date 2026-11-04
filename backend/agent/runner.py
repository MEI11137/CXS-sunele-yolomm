"""轮次运行器：在后台线程里驱动一次真实对话。

可靠性要点（这些是实测踩过的坑，不是理论）：

* 引擎的 ``/session/status`` 在会话空闲后**不再返回该 key**，所以“没有 key”= 空闲；
* 一轮里可能出现多条 assistant 消息（工具调用一轮 + 最终回答一轮），
  ``step-finish.reason == "tool-calls"`` 的不是最终回答；
* ``summary`` 消息、``synthetic`` / ``ignored`` 文本属于压缩与内部内容，不能当回答展示；
* idle 可能只是短暂空档：必须连续观察到空闲并经过稳定期，再确认最终消息；
* stop 只是“请求停止”，真正的终止以引擎 abort 结果与空闲状态为准；
* 浏览器刷新/关闭不影响本线程，重连后按事件序号续拉即可恢复。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from backend import db
from backend.agent import engine as engine_module
from backend.agent import identity, policy, prompt, store

DEFAULT_TIMEOUT = 900
POLL_INTERVAL = 0.5
IDLE_STABLE_SECONDS = 1.2
IDLE_GRACE_SECONDS = 6.0
STOP_WAIT_SECONDS = 20.0
MAX_ENGINE_ERRORS = 5
TOOL_EVENT_LIMIT = 160
MAX_ASSISTANT_CHARS = 40000
# 发起前读取“已有消息 ID 集合”的次数。这是**只读** GET，不会计费，也不发 prompt；
# 失败一次就换重试，最终拿不到确定答案时必须让本轮失败，绝不能当成空历史继续。
BASELINE_READ_ATTEMPTS = 3
BASELINE_READ_BACKOFF = 0.25

_ABORTED = "MessageAbortedError"


class TurnRunner:
    def __init__(self, settings, manager: engine_module.EngineManager, gateway, *, max_workers: int = 4):
        self._settings = settings
        self._manager = manager
        self._gateway = gateway
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._slots = threading.BoundedSemaphore(max_workers)
        self._session_locks: dict[str, threading.Lock] = {}
        self._shutdown = threading.Event()

    # ------------------------------------------------------------------ 调度

    def start(self, turn_id: str) -> None:
        if self._shutdown.is_set():
            return
        with self._lock:
            if turn_id in self._threads and self._threads[turn_id].is_alive():
                return
            thread = threading.Thread(
                target=self._run_guarded, args=(str(turn_id),), name=f"hx17-turn-{turn_id}", daemon=True
            )
            self._threads[turn_id] = thread
            thread.start()

    def shutdown(self) -> None:
        self._shutdown.set()
        with self._lock:
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout=2)

    def active_threads(self) -> int:
        with self._lock:
            return sum(1 for item in self._threads.values() if item.is_alive())

    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    def _run_guarded(self, turn_id: str) -> None:
        acquired = self._slots.acquire(timeout=max(60, self._settings.turn_timeout_seconds))
        if not acquired:
            self._mark_failed_direct(turn_id, "SERVER_BUSY", "服务端正忙，本轮没有开始，请稍后重试")
            return
        try:
            self._run(turn_id)
        except Exception as exc:  # 任何未预期异常都要落到轮次状态，不能静默消失
            logging.getLogger(__name__).exception("轮次运行器异常：%s db=%s", exc, self._settings.db_path)
            self._mark_failed_direct(
                turn_id, "RUNNER_CRASH", f"运行器内部错误（{type(exc).__name__}），本轮已终止"
            )
        finally:
            self._slots.release()

    def _mark_failed_direct(self, turn_id: str, code: str, message: str) -> None:
        """兜底：运行器自身崩了也要把轮次落成失败，不能让界面一直转圈。"""
        try:
            conn = self._connect()
        except Exception:
            return
        try:
            row = store.get(conn, turn_id)
            store.finish(conn, turn_id, "failed", error_code=code, error_message=message)
            self._event(conn, row, "error", {"code": code, "message": message})
            self._event(conn, row, "turn.status", {"status": "failed"})
        except Exception:
            return
        finally:
            conn.close()

    # ------------------------------------------------------------------ 主流程

    def _connect(self):
        return db.connect(self._settings.db_path)

    def _run(self, turn_id: str) -> None:
        conn = self._connect()
        try:
            turn = store.get(conn, turn_id)
            started_ok = store.mark_running(conn, turn["id"])
            if not started_ok:
                return
            self._event(conn, turn, "turn.status", {"status": "running"})

            owner = self._validate_context(conn, turn)
            if owner is None:
                return
            project, session, connection, fingerprint, model = owner

            root = Path(str(project["root_path"]))
            spec = engine_module.EngineSpec(
                user_id=int(turn["owner_id"]),
                project_id=str(turn["project_id"]),
                session_id=str(turn["session_id"]),
                root=root,
                fingerprint=fingerprint,
                provider=str(connection["provider"]),
                model=model,
                models=identity.available_models(conn, int(turn["owner_id"])),
                base_url=str(connection["base_url"]),
                api_key=self._api_key(conn, int(turn["owner_id"])),
                gateway_url=self._gateway.base_url,
                tool_token="",
                python=self._settings.engine_python or "",
                # 视觉证据必须对得上**这一轮实际要请求的模型**，否则给另一个模型
                # 声明 image 输入会把图片发给不支持图像的模型。
                vision=identity.verified_vision(conn, int(turn["owner_id"]), model),
            )
            try:
                instance = self._manager.get(spec)
            except engine_module.EngineUnavailable as exc:
                self._fail(conn, turn, exc.code, self._engine_message(exc))
                return

            actor = self._actor(conn, turn, project, connection, model, root, fingerprint)
            try:
                engine_session, recreated = self._prepare_engine_session(
                    conn, instance, session, fingerprint
                )
                if recreated:
                    # 新会话在引擎里是空的：把站点已经公开过的历史带过去，
                    # 否则用户会看到助手突然“失忆”，或者更糟——把已经做完的事再做一遍。
                    self._event(
                        conn,
                        turn,
                        "notice",
                        {
                            "level": "info",
                            "message": "引擎会话已重建，已把最近的对话上下文恢复给助手。",
                        },
                    )
                # 边界必须是**消息 ID 集合**，不能用数组长度：本轮的消息可能在
                # prompt 返回前就已经写入，长度基线会把它们全部当成历史截掉。
                baseline = self._message_ids(instance, engine_session)
                prompt_text = self._prompt_text(conn, turn)
                if recreated:
                    prompt_text = self._restore_context(conn, turn, prompt_text) + prompt_text
                self._manager.prompt_async(
                    instance,
                    engine_session,
                    text=prompt_text,
                    system=prompt.build(actor),
                    model=model,
                )
            except engine_module.EngineUnavailable as exc:
                self._fail(conn, turn, exc.code, self._engine_message(exc))
                return

            self._poll(conn, turn, instance, engine_session, baseline)
        except KeyError:
            return
        finally:
            conn.close()

    def _message_ids(self, instance, engine_session: str) -> set[str]:
        """当前引擎会话里已经存在的消息 ID 集合（本轮的判定边界）。

        发 prompt **之前**取，之后只认“不在集合里”的消息。

        这里最关键的一条：**读不到不等于空历史**。曾经这里是
        ``except EngineUnavailable: return set()``，于是一次瞬时读失败就让旧消息
        全部变成“本轮新增”；新请求短暂 idle 时，上一轮的终答满足完成条件，
        本轮被标成 completed 却把旧回答又落了一遍，真正的新答反而丢了。

        所以现在：读失败就**有界只读重试**（只读请求，不会计费，也不会重发 prompt）；
        仍然拿不到确定答案就抛出 ``BASELINE_UNAVAILABLE``，让本轮在**发出任何模型
        请求之前**如实失败。宁可让用户重发一次，也不能把上一轮的回答冒充成本轮结果。
        """
        last: engine_module.EngineUnavailable | None = None
        for attempt in range(BASELINE_READ_ATTEMPTS):
            try:
                messages = self._manager.messages(instance, engine_session, limit=500)
            except engine_module.EngineUnavailable as exc:
                last = exc
                if attempt + 1 < BASELINE_READ_ATTEMPTS:
                    time.sleep(BASELINE_READ_BACKOFF * (attempt + 1))
                continue
            ids: set[str] = set()
            for item in messages:
                info = item.get("info") or {}
                message_id = str(info.get("id") or "")
                if message_id:
                    ids.add(message_id)
            if messages and not ids:
                # 读到了内容却一个 ID 都没有：形状不对，同样不能当可靠边界。
                raise engine_module.EngineUnavailable(
                    "BASELINE_UNAVAILABLE",
                    "引擎返回了消息但缺少消息 ID，无法确定本轮的新消息边界",
                )
            return ids
        raise engine_module.EngineUnavailable(
            "BASELINE_UNAVAILABLE",
            f"无法读取引擎会话里已有的消息（{last.message if last else '未知原因'}），"
            "不能确定本轮的新消息边界",
        ) from last

    # ------------------------------------------------------------------ 上下文校验

    def _validate_context(self, conn, turn):
        user = conn.execute(
            "SELECT id, status FROM users WHERE id = ?", (int(turn["owner_id"]),)
        ).fetchone()
        if user is None or user["status"] != "active":
            self._fail(conn, turn, "USER_DISABLED", "账号已被停用，本轮没有开始")
            return None
        project = conn.execute(
            "SELECT * FROM projects WHERE id = ? AND owner_id = ?",
            (turn["project_id"], int(turn["owner_id"])),
        ).fetchone()
        if project is None or project["status"] != "active":
            self._fail(conn, turn, "PROJECT_CLOSED", "项目已删除或不可用，本轮没有开始")
            return None
        session = conn.execute(
            "SELECT * FROM sessions WHERE id = ? AND owner_id = ? AND project_id = ?",
            (turn["session_id"], int(turn["owner_id"]), turn["project_id"]),
        ).fetchone()
        if session is None or session["status"] != "active":
            self._fail(conn, turn, "SESSION_CLOSED", "会话已删除，本轮没有开始")
            return None
        if not project["root_path"]:
            self._fail(
                conn, turn, "NO_PROJECT_ROOT", "该项目还没有绑定服务器目录，先让用户在项目页完成绑定"
            )
            return None
        from backend import paths
        from backend.services import roots as root_service

        granted = [item["path"] for item in root_service.list_for_user(conn, int(turn["owner_id"]))]
        if not any(paths.is_within(project["root_path"], item) for item in granted):
            self._fail(
                conn,
                turn,
                "GRANT_REVOKED",
                "项目目录的授权已被收回；请联系管理员重新授予后再发送",
            )
            return None

        connection = identity.connection_for(conn, int(turn["owner_id"]))
        if connection is None:
            self._fail(conn, turn, "CONNECTION_MISSING", "还没有保存个人模型连接，先在设置页配置")
            return None
        fingerprint = identity.connection_fingerprint(connection)
        if fingerprint != turn["connection_fingerprint"]:
            self._fail(
                conn, turn, "CONNECTION_CHANGED", "个人模型连接已变更，本轮已失效；请重新发送消息"
            )
            return None
        model = str(turn["model"] or identity.effective_model(conn, int(turn["owner_id"]), connection))
        return project, session, connection, fingerprint, model

    def _api_key(self, conn, user_id: int) -> str:
        from backend.crypto import decrypt_text
        from backend.services import connections as connection_service

        try:
            return connection_service.server_credentials(
                conn, int(user_id), key=self._connection_key()
            )["api_key"]
        except Exception:
            raise engine_module.EngineUnavailable(
                "CONNECTION_UNREADABLE",
                "个人连接的密钥无法解密；请在设置页重新保存 Key",
            ) from None

    def _connection_key(self) -> bytes:
        from backend import security

        path = Path(self._settings.secret_dir) / "connection.key"
        return security.load_or_create_key(path)

    def _actor(self, conn, turn, project, connection, model, root: Path, fingerprint: str):
        from backend.agent.context import Actor

        return Actor(
            user_id=int(turn["owner_id"]),
            username="",
            project_id=str(turn["project_id"]),
            project_name=str(project["name"]),
            session_id=str(turn["session_id"]),
            turn_id=str(turn["id"]),
            mode=policy.normalize_mode(turn["mode"]),
            root_path=root,
            connection_fingerprint=fingerprint,
            provider=str(connection["provider"]),
            model=model,
        )

    def _ensure_engine_session(self, conn, instance, session, fingerprint: str) -> str:
        """只要 Native 会话 ID（运维脚本与测试用的稳定入口）。"""
        return self._prepare_engine_session(conn, instance, session, fingerprint)[0]

    def _prepare_engine_session(self, conn, instance, session, fingerprint: str):
        """返回 ``(native_session_id, recreated)``。

        复用条件有三条，缺一不可：

        1. 连接指纹一致（换了连接不能接着用旧会话的上下文）；
        2. **存储代次一致**——Native 会话 ID 只在创建它的那份私有存储目录里有意义，
           视觉能力结论变化会让引擎换目录；
        3. 这个 ID 在当前引擎里**确实存在**（只有明确 404 才判不存在）。

        第 3 条拿不到确定答案（超时/连接失败/5xx）时直接抛出，让本轮失败；
        绝不当成 404 去新建会话再把用户请求重发一遍——那会丢历史、也会重复已经做过的动作。
        """
        session_id = str(session["id"])
        storage_key = self._manager.storage_key(instance.spec)
        row = store.engine_session(conn, session_id)
        if self._mapping_usable(instance, row, fingerprint, storage_key):
            return str(row["engine_session_id"]), False
        with self._session_lock(session_id):
            row = store.engine_session(conn, session_id)
            if self._mapping_usable(instance, row, fingerprint, storage_key):
                return str(row["engine_session_id"]), False
            engine_session = self._manager.create_session(instance, str(session["title"]))
            store.set_engine_session(
                conn, session_id, engine_session, fingerprint, storage_key
            )
            return engine_session, True

    def _mapping_usable(self, instance, row, fingerprint: str, storage_key: str) -> bool:
        if row is None:
            return False
        if str(row["fingerprint"]) != str(fingerprint):
            return False
        if str(row["storage_key"] or "") != str(storage_key):
            return False
        return bool(self._manager.session_exists(instance, str(row["engine_session_id"])))

    def _restore_context(self, conn, turn, prompt_text: str) -> str:
        """把站点里已公开的最近对话整理成一段上下文，供重建后的新会话接续。

        只取用户消息与**最终回答**：工具中间产物、压缩摘要、内部推理都不在
        ``messages`` 表里，因此不可能被这里带出去。本轮的用户消息会作为正式
        prompt 再发一次，所以从上下文里排除，避免同一条请求出现两遍。
        """
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 21",
            (str(turn["session_id"]),),
        ).fetchall()
        rows = list(reversed(rows))
        if rows:
            newest = rows[-1]
            if str(newest["role"]) == "user" and str(newest["content"]) == str(prompt_text):
                rows = rows[:-1]
        lines: list[str] = []
        for row in rows[-20:]:
            text = " ".join(str(row["content"] or "").split())
            if not text:
                continue
            label = "用户" if str(row["role"]) == "user" else "助手"
            lines.append(f"{label}：{text[:1200]}")
        if not lines:
            return ""
        body = "\n".join(lines)
        if len(body) > 6000:
            body = body[-6000:]
        return (
            "【会话恢复】本机引擎的会话存储被重建过（能力结论变化、进程重启或存储回收），"
            "新引擎里没有此前的对话记录。下面是从站点记录里取到的最近对话：\n"
            f"{body}\n\n"
            "请据此保持连续：不要重复已经完成的动作，也不要声称重新执行过任何操作；"
            "缺少的信息可以重新只读确认一次。\n\n"
        )

    def _prompt_text(self, conn, turn) -> str:
        if turn["kind"] == "continuation":
            row = conn.execute(
                "SELECT * FROM actions WHERE session_id = ? AND status = 'done' "
                "ORDER BY decided_at DESC, created_at DESC LIMIT 1",
                (turn["session_id"],),
            ).fetchone()
            if row is not None:
                try:
                    result = json.loads(row["result"] or "{}")
                except ValueError:
                    result = {}
                return (
                    "【系统事件】用户已确认你上一步请求的动作并已执行完成。\n"
                    f"动作：{row['title']}\n"
                    f"执行结果：{json.dumps(result, ensure_ascii=False)[:1500]}\n\n"
                    "请基于这个真实结果继续：如果用户的原始目标已完成，给出简洁的最终答复；"
                    "还需要别的操作就继续调用工具。不要重复已经成功的动作。"
                )
        row = conn.execute(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'user' "
            "ORDER BY id DESC LIMIT 1",
            (turn["session_id"],),
        ).fetchone()
        return str(row["content"]) if row is not None else "请继续。"

    # ------------------------------------------------------------------ 轮询

    def _poll(
        self, conn, turn, instance, engine_session: str, baseline: set[str]
    ) -> None:
        """轮询直到本轮结束。

        ``baseline`` 是发起前取得的**确定**边界，这里不再提供“没有就按空集算”的兜底：
        空集会把整段历史当成本轮新增，正是旧回答被重新落库的那条路径。
        """
        started = time.monotonic()
        timeout = float(self._settings.turn_timeout_seconds or DEFAULT_TIMEOUT)
        deadline = started + timeout

        seen_text: dict[str, str] = {}
        seen_tool: dict[str, str] = {}
        engine_errors = 0
        idle_since: float | None = None
        stopped = False
        last_context_check = 0.0

        while True:
            if self._shutdown.is_set():
                self._finish_stopped(conn, turn, "SERVER_SHUTDOWN", "服务端正在关闭，本轮已中断")
                return
            if time.monotonic() > deadline:
                self._abort_and_settle(instance, engine_session)
                self._fail(conn, turn, "TURN_TIMEOUT", "本轮超过最长运行时间，已被中断")
                return

            # 运行期间也要复查上下文：换连接、撤权、停用账号、删会话/项目都必须当场生效。
            now = time.monotonic()
            if now - last_context_check >= 2.0:
                last_context_check = now
                reason = self._context_invalid(conn, turn)
                if reason is not None:
                    code, message = reason
                    self._abort_and_settle(instance, engine_session)
                    self._fail(conn, turn, code, message)
                    return

            stop_requested = bool(
                conn.execute(
                    "SELECT stop_requested_at FROM turns WHERE id = ?", (turn["id"],)
                ).fetchone()["stop_requested_at"]
            )
            if stop_requested and not stopped:
                stopped = True
                self._event(conn, turn, "turn.status", {"status": "stopping"})
                state = self._abort_and_settle(instance, engine_session)
                # 停止后不再继续轮询这台引擎（可能已经被隔离）：把已经产生的内容同步一次，
                # 然后如实收尾。
                read_failed = False
                try:
                    messages = self._manager.messages(instance, engine_session, limit=500)
                except engine_module.EngineUnavailable:
                    # 这里是**收尾读取**失败，不是本轮判定边界：不能因此回头把历史当新增，
                    # 但也别把“读不到”说成“没有内容”，要在收尾说明里讲清楚。
                    read_failed = True
                    messages = []
                self._sync(conn, turn, self._new_messages(messages, baseline), seen_text, seen_tool)
                detail = "已停止本轮：后续步骤不再执行；已完成的工具调用结果保留。"
                if read_failed:
                    detail += "（停止时无法读取引擎消息，本轮尚未保存的中间输出可能已丢失。）"
                self._finish_stopped(conn, turn, "STOPPED", detail, state=state)
                return

            try:
                state = self._manager.session_state(instance, engine_session)
                messages = self._manager.messages(instance, engine_session, limit=500)
                engine_errors = 0
            except engine_module.EngineUnavailable:
                engine_errors += 1
                if engine_errors >= MAX_ENGINE_ERRORS:
                    self._manager.stop_project(int(turn["owner_id"]), str(turn["project_id"]))
                    self._fail(
                        conn,
                        turn,
                        "ENGINE_LOST",
                        "引擎进程失去响应，本轮已终止；已完成的工具结果保留，可重新发送",
                    )
                    return
                time.sleep(1.0)
                continue

            new_messages = self._new_messages(messages, baseline)
            self._sync(conn, turn, new_messages, seen_text, seen_tool)

            if state == "idle":
                if idle_since is None:
                    idle_since = time.monotonic()
                final = self._pick_final(new_messages)
                quiet = time.monotonic() - idle_since
                if final is not None and quiet >= IDLE_STABLE_SECONDS:
                    self._finish_success(conn, turn, final, new_messages)
                    return
                if quiet >= IDLE_GRACE_SECONDS:
                    self._finish_without_text(conn, turn, new_messages)
                    return
            else:
                idle_since = None

            time.sleep(POLL_INTERVAL)

    @staticmethod
    def _new_messages(messages: list[dict], baseline: set[str]) -> list[dict]:
        """只保留本轮新增的消息。

        引擎的分页参数不保证“从头到尾”，但新消息一定在末尾且 ID 不在基线里，
        因此按 ID 过滤比按数组长度切片可靠：快速完成、超过分页上限、旧消息
        parts 后补更新都不会把本轮回答误判成历史。
        """
        fresh: list[dict] = []
        for item in messages:
            info = item.get("info") or {}
            message_id = str(info.get("id") or "")
            if message_id and message_id in baseline:
                continue
            fresh.append(item)
        return fresh

    def _context_invalid(self, conn, turn) -> tuple[str, str] | None:
        """运行中的轮次上下文复核；返回 (code, message) 表示必须立刻终止。"""
        user = conn.execute(
            "SELECT status FROM users WHERE id = ?", (int(turn["owner_id"]),)
        ).fetchone()
        if user is None or user["status"] != "active":
            return "USER_DISABLED", "账号已被停用，本轮已终止"
        project = conn.execute(
            "SELECT status, root_path FROM projects WHERE id = ? AND owner_id = ?",
            (turn["project_id"], int(turn["owner_id"])),
        ).fetchone()
        if project is None or project["status"] != "active":
            return "PROJECT_CLOSED", "项目已删除，本轮已终止"
        session = conn.execute(
            "SELECT status FROM sessions WHERE id = ? AND owner_id = ?",
            (turn["session_id"], int(turn["owner_id"])),
        ).fetchone()
        if session is None or session["status"] != "active":
            return "SESSION_CLOSED", "会话已删除，本轮已终止"
        connection = identity.connection_for(conn, int(turn["owner_id"]))
        if connection is None or identity.connection_fingerprint(connection) != turn[
            "connection_fingerprint"
        ]:
            return "CONNECTION_CHANGED", "个人模型连接已变更或删除，本轮已终止；请重新发送消息"
        if not project["root_path"]:
            return "NO_PROJECT_ROOT", "项目目录绑定已被解除，本轮已终止"
        from backend import paths
        from backend.services import roots as root_service

        granted = [
            item["path"] for item in root_service.list_for_user(conn, int(turn["owner_id"]))
        ]
        if not any(paths.is_within(str(project["root_path"]), item) for item in granted):
            return "GRANT_REVOKED", "项目目录授权已被收回，本轮已终止"
        return None

    # ------------------------------------------------------------------ 事件同步

    def _sync(self, conn, turn, messages: list[dict], seen_text: dict, seen_tool: dict) -> None:
        for item in messages:
            info = item.get("info") or {}
            if info.get("role") != "assistant":
                continue
            if info.get("summary"):
                # 压缩摘要不是回答，不展示也不作为完成依据。
                continue
            message_id = str(info.get("id") or "")
            for part in item.get("parts") or []:
                kind = part.get("type")
                if kind == "text":
                    if part.get("synthetic") or part.get("ignored"):
                        continue
                    text = str(part.get("text") or "")
                    part_id = str(part.get("id") or message_id)
                    if not text.strip() and not text:
                        continue
                    if seen_text.get(part_id) == text:
                        continue
                    seen_text[part_id] = text
                    self._event(
                        conn,
                        turn,
                        "text",
                        {"message_id": message_id, "part_id": part_id, "text": text},
                    )
                elif kind == "tool":
                    state = part.get("state") or {}
                    status = str(state.get("status") or "pending")
                    token = f"{status}:{state.get('title') or ''}:{bool(state.get('input'))}"
                    part_id = str(part.get("id") or part.get("callID") or "")
                    if seen_tool.get(part_id) == token:
                        continue
                    seen_tool[part_id] = token
                    timing = state.get("time") or {}
                    started = timing.get("start")
                    ended = timing.get("end")
                    duration = int(ended - started) if started and ended else None
                    output = state.get("output")
                    self._event(
                        conn,
                        turn,
                        "tool",
                        {
                            "call_id": str(part.get("callID") or ""),
                            "tool": str(part.get("tool") or ""),
                            "state": status,
                            "title": str(state.get("title") or "")[:120],
                            "duration_ms": duration,
                            "preview": self._tool_preview(output),
                        },
                    )

    @staticmethod
    def _tool_preview(output: Any) -> str:
        if output is None:
            return ""
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        text = " ".join(text.split())
        return text[:TOOL_EVENT_LIMIT]

    # ------------------------------------------------------------------ 结果判定

    @staticmethod
    def _public_text(item: dict) -> str:
        chunks: list[str] = []
        for part in item.get("parts") or []:
            if part.get("type") != "text":
                continue
            if part.get("synthetic") or part.get("ignored"):
                continue
            text = str(part.get("text") or "")
            if text.strip():
                chunks.append(text)
        return "\n\n".join(chunks).strip()

    def _pick_final(self, messages: list[dict]) -> dict | None:
        """挑出可展示的最终回答。

        规则（逐条对应踩过的坑）：

        * 只看 assistant，且跳过压缩摘要；
        * 最新一条 assistant 只要还没 ``time.completed``，就说明本轮还在写——
          此时**不能**退回更早的中间文本冒充最终答复；
        * ``finish``/``step-finish.reason`` 为 ``tool-calls`` 表示还在工具轮，
          继续等引擎发下一轮，不把它当回答；
        * ``length`` / ``content-filter`` / ``error`` 不是可用回答，交给失败路径；
        * 必须同时有已完成的 info 与真实 text part，仅 idle 不足以宣称完成。
        """
        for item in reversed(messages):
            info = item.get("info") or {}
            if info.get("role") != "assistant" or info.get("summary"):
                continue
            if info.get("error"):
                return None
            completed = (info.get("time") or {}).get("completed")
            parts = item.get("parts") or []
            reasons = [
                str(part.get("reason"))
                for part in parts
                if part.get("type") == "step-finish" and part.get("reason")
            ]
            finish = str(info.get("finish") or (reasons[-1] if reasons else ""))
            if not completed:
                return None
            if finish == "tool-calls":
                continue
            if finish in {"length", "content-filter", "error"}:
                return None
            text = self._public_text(item)
            if not text:
                continue
            return {
                "message_id": str(info.get("id") or ""),
                "text": text,
                "reason": finish or "stop",
                "completed": completed,
            }
        return None

    def _last_error(self, messages: list[dict]) -> dict | None:
        for item in reversed(messages):
            info = item.get("info") or {}
            if info.get("role") == "assistant" and info.get("error"):
                error = info["error"]
                if isinstance(error, dict):
                    name = str(error.get("name") or "")
                    message = str((error.get("data") or {}).get("message") or "")
                    return {"name": name, "message": message}
        return None

    def _finish_success(self, conn, turn, final: dict, messages: list[dict]) -> None:
        text = str(final["text"])[:MAX_ASSISTANT_CHARS]
        tool_calls = sum(
            1
            for item in messages
            for part in item.get("parts") or []
            if part.get("type") == "tool"
        )
        message = self._append_assistant(
            conn,
            turn,
            text,
            {
                "turn_id": turn["id"],
                "mode": turn["mode"],
                "model": turn["model"],
                "engine_message_id": final["message_id"],
                "tool_calls": tool_calls,
                "finish_reason": final["reason"],
            },
        )
        if message is None:
            # 正文没落库就不能把轮次标成功：否则刷新后回答凭空消失、状态却显示完成。
            self._fail(
                conn,
                turn,
                "FINAL_NOT_SAVED",
                "已经生成了回答，但保存到会话记录时失败；本轮按失败处理，没有写入占位回复。"
                "请稍后重试或联系管理员检查数据库。",
            )
            return
        store.finish(conn, turn["id"], "completed")
        # 不清空 proposed 动作：用户确认卡可能属于这一轮，“完成”并不代表它已失效。
        self._event(
            conn,
            turn,
            "final",
            {
                "status": "completed",
                "text": text,
                "message_id": message["id"] if message else None,
                "tool_calls": tool_calls,
            },
        )
        self._event(conn, turn, "turn.status", {"status": "completed"})

    def _finish_without_text(self, conn, turn, messages: list[dict]) -> None:
        error = self._last_error(messages)
        if error is not None:
            code, human = self._map_error(error)
            self._fail(conn, turn, code, human)
            return
        self._fail(
            conn,
            turn,
            "NO_FINAL_TEXT",
            "引擎结束了本轮但没有给出可展示的最终回答（可能被压缩、被上游截断或中途退出）。"
            "本轮不填占位回复，请重新发送或换个问法。",
        )

    def _abort_and_settle(self, instance, engine_session: str) -> str:
        """请求 abort，等到**真实**空闲；等不到就隔离这台引擎。

        abort 只是一个请求。引擎没有确认空闲，说明旧进程里可能还有工具调用在跑；
        如果把同一台引擎（和同一张令牌）直接交给下一轮，旧调用会被记到新任务上。
        所以这里宁可杀掉进程：进程没了，旧调用就不可能再产生副作用。
        私有存储保留，下一轮重建后历史还能接着用。
        """
        self._manager.abort(instance, engine_session)
        deadline = time.monotonic() + STOP_WAIT_SECONDS
        state = "unknown"
        while time.monotonic() < deadline:
            try:
                state = self._manager.session_state(instance, engine_session)
            except engine_module.EngineUnavailable:
                state = "unreachable"
                break
            if state == "idle":
                return state
            time.sleep(0.4)
        self._manager.quarantine(instance)
        return state

    def _finish_stopped(self, conn, turn, code: str, message: str, *, state: str | None = None) -> None:
        detail = message
        isolated = state is not None and state != "idle"
        if isolated:
            detail = message + "（注意：引擎未确认空闲，已隔离该引擎进程；这可能中断正在收尾的步骤，"
            detail += "不要把未返回结果的动作当作已完成。）"
            code = "STOPPED_UNCONFIRMED"
        running = store.running_jobs(conn, turn["id"])
        if running:
            names = "、".join(sorted({str(item["tool"]) for item in running}))
            detail += (
                f"另有 {len(running)} 个工具任务（{names}）在停止时仍标记为执行中，"
                "结果未知，不能假设它们已经完成或已经回滚。"
            )
        if isolated:
            # 进程已经被杀掉，这些任务永远不会有真实返回值，继续标 running 只会误导。
            store.fail_running_jobs(
                conn,
                turn["id"],
                code="STOPPED_UNCONFIRMED",
                message="引擎在停止时未确认空闲并已被隔离，该任务的执行结果无法确认",
            )
        store.finish(conn, turn["id"], "stopped", error_code=code, error_message=detail)
        store.expire_actions(conn, turn["id"])
        self._event(conn, turn, "final", {"status": "stopped", "error_code": code, "text": ""})
        self._event(conn, turn, "turn.status", {"status": "stopped"})

    def _fail(self, conn, turn, code: str, message: str) -> None:
        # 轮次已经结束，不会再有工具返回结果：把仍标着“执行中”的任务收成失败，
        # 否则同一个 call_id 之后查询永远停在“请稍后再查”。
        store.fail_running_jobs(
            conn, turn["id"], code=code, message="本轮已结束，该任务的执行结果无法确认"
        )
        store.finish(conn, turn["id"], "failed", error_code=code, error_message=message)
        store.expire_actions(conn, turn["id"])
        self._event(conn, turn, "error", {"code": code, "message": message})
        self._event(conn, turn, "turn.status", {"status": "failed"})

    def _map_error(self, error: dict) -> tuple[str, str]:
        name = error.get("name") or ""
        if name == _ABORTED:
            return "ABORTED", "本轮被中断（引擎报告 Aborted），没有生成最终回答。"
        if name == "ProviderAuthError":
            return "PROVIDER_AUTH", "模型服务拒绝了该凭据；请到设置页检查个人连接与 Key。"
        if name == "APIError":
            return "PROVIDER_API", "模型服务返回错误；请查看设置页的连接检查结果后重试。"
        if name == "ContextOverflowError":
            return "CONTEXT_OVERFLOW", "上下文超出模型窗口，请新建会话或缩短输入。"
        if name == "MessageOutputLengthError":
            return "OUTPUT_LENGTH", "模型输出被长度限制截断，没有可用回答。"
        if name == "ContentFilterError":
            return "CONTENT_FILTER", "模型侧内容过滤拦截了本次输出。"
        return "ENGINE_ERROR", "引擎报告本轮失败，没有最终回答。"

    @staticmethod
    def _engine_message(exc: engine_module.EngineUnavailable) -> str:
        mapping = {
            "ENGINE_MISSING": "服务端没有找到 OpenCode 可执行文件，请联系管理员检查部署。",
            "ENGINE_VERSION_MISMATCH": "引擎版本与本工程验证版本不一致，已拒绝启动。",
            "ENGINE_VERSION_UNKNOWN": "无法确认引擎版本，已拒绝启动。",
            "ENGINE_START_FAILED": "引擎进程启动失败，请稍后重试或联系管理员。",
            "ENGINE_NOT_READY": "引擎启动后健康检查未通过，请稍后重试。",
            "ENGINE_EXIT": "引擎进程启动后立即退出，请查看服务端日志。",
            "ENGINE_TIMEOUT": "引擎响应超时，本轮已终止。",
            "ENGINE_UNREACHABLE": "无法连接引擎进程，本轮已终止。",
            "BASELINE_UNAVAILABLE": (
                "无法确认引擎里已有的对话边界，本轮**没有**向模型发送任何请求，因此不会重复执行。"
                "稍等片刻重新发送即可。"
            ),
            "CONNECTION_UNREADABLE": "个人连接的密钥无法解密，请在设置页重新保存 Key。",
        }
        return mapping.get(exc.code, "引擎不可用，本轮没有开始。")

    # ------------------------------------------------------------------ 工具

    def _append_assistant(self, conn, turn, text: str, meta: dict) -> dict | None:
        if not text.strip():
            return None
        from backend.services import sessions as session_service

        try:
            return session_service.append_message(
                conn,
                session_id=str(turn["session_id"]),
                role="assistant",
                content=text,
                meta=meta,
                limit=MAX_ASSISTANT_CHARS,
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "最终回答保存失败 turn=%s", str(turn["id"])
            )
            return None

    def _event(self, conn, turn, type_: str, payload: dict) -> None:
        store.append_event(conn, str(turn["id"]), str(turn["session_id"]), type_, payload)

"""私有工具网关：模型只能通过这里触达业务能力。

为什么不是“让模型直接调 /api”：

* 网关是**独立回环监听器**（127.0.0.1 + 随机端口），不属于对外站点端口，
  因此不会被公网入口暴露，也不吃站点 Cookie；
* 每次调用必须带 32 字节随机令牌，令牌由服务端在轮次启动时下发，绑定
  (用户, 项目, 会话, 轮次, 权限模式, 连接指纹)。工具里没有 user_id 参数，
  模型无法冒充别人；
* 每次调用都重新从数据库校验：账号启用、项目未删、会话归属、轮次仍在进行、
  连接未被替换、目录授权未被收回。任何一项不成立就拒绝，而不是“继续用上次的身份”；
* ``call_id`` 由调用方生成并作为任务主键：网络超时后用同一个 id 查询状态，
  绝不会因为重试而把同一操作执行两遍。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from backend import db, paths
from backend.agent import fsops, identity, policy, store, tools
from backend.agent.context import Actor, ToolError

TOKEN_HEADER = "X-HX17-Tool-Token"


class GatewayIdentityError(ToolError):
    """身份/上下文失效：整条会话不能再用，必须冒到 HTTP 层（而不是当成一次工具业务失败）。"""
MAX_BODY = 4 * 1024 * 1024
TOKEN_TTL = 6 * 3600


@dataclass
class Binding:
    """会话级工具令牌。

    绑定的是**会话**而不是某一轮：同一个会话的连续多轮共用同一个引擎进程与工具桥，
    因此令牌必须跨轮有效；而“当前是哪一轮”在每次调用时从数据库实时解析，
    模型无法通过伪造 ``session_id`` 或 ``turn_id`` 把调用写到别人的轮次里。
    """

    token: str
    user_id: int
    session_id: str
    project_id: str
    fingerprint: str
    expires_at: float

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) > self.expires_at


class ToolGateway:
    def __init__(self, settings) -> None:
        self._settings = settings
        self._lock = threading.RLock()
        self._bindings: dict[str, Binding] = {}
        # 已退场的令牌要保留结论，不能直接忘掉：引擎缓存里可能还握着它，
        # 而“过期”（可以换新凭据重启）与“被明确撤销”（不许复活）是两种处置。
        self._retired: dict[str, tuple[str, float]] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0
        # 私有数据永远不可见：即使项目绑定目录恰好覆盖了 var/ 也按路径拒绝。
        fsops.configure_private([settings.data_dir, settings.secret_dir])

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        gateway = self

        class Handler(_GatewayHandler):
            pass

        Handler.gateway = gateway  # type: ignore[attr-defined]
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        self.port = int(server.server_address[1])
        self._thread = threading.Thread(
            target=server.serve_forever, name="hx17-tool-gateway", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=3)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ------------------------------------------------------------------ 令牌

    def issue_session(
        self,
        *,
        user_id: int,
        session_id: str,
        project_id: str,
        fingerprint: str,
    ) -> str:
        """签发会话级令牌：同一会话的连续多轮复用，换连接/删会话即失效。"""
        from backend import security

        token = security.new_token(32)
        binding = Binding(
            token=token,
            user_id=int(user_id),
            session_id=str(session_id),
            project_id=str(project_id),
            fingerprint=str(fingerprint),
            expires_at=time.time() + TOKEN_TTL,
        )
        with self._lock:
            self._prune()
            self._revoke_session_locked(binding.session_id)
            self._bindings[token] = binding
        return token

    def revoke(self, token: str, *, reason: str = "disposed") -> None:
        """明确撤销：之后既不能用，也不允许被“自动续期”复活。"""
        key = str(token or "")
        with self._lock:
            if self._bindings.pop(key, None) is not None:
                self._retired[key] = (reason, time.time())
            else:
                self._retired.setdefault(key, (reason, time.time()))

    def revoke_session(self, session_id: str) -> int:
        with self._lock:
            return self._revoke_session_locked(session_id)

    def _revoke_session_locked(self, session_id: str, *, reason: str = "session_revoked") -> int:
        targets = [key for key, item in self._bindings.items() if item.session_id == str(session_id)]
        for key in targets:
            self._bindings.pop(key, None)
            self._retired[key] = (reason, time.time())
        return len(targets)

    def _prune(self) -> None:
        now = time.time()
        stale = [key for key, item in self._bindings.items() if item.expired(now)]
        for key in stale:
            self._bindings.pop(key, None)
            self._retired[key] = ("expired", now)
        # 退场记录也要有上限，但保留时长远大于 TTL：足够覆盖仍在缓存里的引擎。
        horizon = now - TOKEN_TTL * 2
        for key in [k for k, (_state, at) in self._retired.items() if at < horizon]:
            self._retired.pop(key, None)

    def binding_for(self, token: str) -> Binding | None:
        with self._lock:
            binding = self._bindings.get(str(token or ""))
            if binding is None or binding.expired():
                return None
            return binding

    def token_state(self, token: str) -> str:
        """令牌此刻的结论：``active`` / ``expired`` / ``revoked`` / ``unknown``。

        引擎缓存要靠它决定“能不能继续用”：过期可以带新凭据重启并保留会话存储，
        被明确撤销则不许复活——那是一次真实的安全结论，不是时间到了。
        """
        key = str(token or "")
        if not key:
            return "unknown"
        with self._lock:
            binding = self._bindings.get(key)
            if binding is not None:
                return "expired" if binding.expired() else "active"
            retired = self._retired.get(key)
            if retired is not None:
                return "revoked" if retired[0] != "expired" else "expired"
            return "unknown"

    # ------------------------------------------------------------------ 身份反查

    def actor_for(self, binding: Binding) -> Actor:
        """按会话令牌反查“这一刻真正在执行的那一轮”。

        身份不来自模型参数，也不来自令牌里缓存的 turn：每次调用都重新读库，
        这样停止、换连接、撤权、删项目后的下一次调用会立刻被拒绝。
        """
        conn = db.connect(self._settings.db_path)
        try:
            session = conn.execute(
                """
                SELECT s.id AS session_id, s.status AS session_status, s.owner_id AS session_owner,
                       s.project_id AS session_project,
                       p.status AS project_status, p.root_path, p.name AS project_name,
                       p.owner_id AS project_owner,
                       u.username, u.status AS user_status
                  FROM sessions s
                  JOIN projects p ON p.id = s.project_id
                  JOIN users u ON u.id = s.owner_id
                 WHERE s.id = ?
                """,
                (binding.session_id,),
            ).fetchone()
            if session is None:
                raise GatewayIdentityError(
                    "SESSION_NOT_FOUND", "会话已不存在，请重新发送消息", status=404
                )
            if session["user_status"] != "active":
                raise GatewayIdentityError(
                    "USER_DISABLED", "账号已被停用，工具调用被拒绝", status=403
                )
            if session["session_status"] != "active":
                raise GatewayIdentityError(
                    "SESSION_CLOSED", "会话已删除，工具调用被拒绝", status=409
                )
            if session["project_status"] != "active":
                raise GatewayIdentityError(
                    "PROJECT_CLOSED", "项目已删除，工具调用被拒绝", status=409
                )
            if (
                int(session["session_owner"]) != int(binding.user_id)
                or int(session["project_owner"]) != int(binding.user_id)
                or str(session["session_project"]) != str(binding.project_id)
            ):
                raise GatewayIdentityError("ACTOR_MISMATCH", "身份与会话不一致，已拒绝", status=403)

            turn = store.active_for_session(conn, str(session["session_id"]))
            if turn is None or int(turn["owner_id"]) != int(binding.user_id):
                raise GatewayIdentityError(
                    "TURN_FINISHED",
                    "本轮已结束，不能再调用工具；有新指令请重新发送消息",
                    status=409,
                )
            # 停止是“撤销这一轮继续动手的资格”，不是等进程慢慢收尾：
            # 一旦用户请求停止，之后到达的任何工具调用都必须拒绝，
            # 否则会在用户以为已经停下之后继续产生副作用。
            if turn["stop_requested_at"]:
                raise GatewayIdentityError(
                    "STOP_REQUESTED",
                    "本轮已请求停止，不再执行新的工具动作；需要继续请重新发送消息",
                    status=409,
                )

            connection = identity.connection_for(conn, int(turn["owner_id"]))
            current = identity.connection_fingerprint(connection)
            if not current or current != turn["connection_fingerprint"]:
                raise GatewayIdentityError(
                    "CONNECTION_CHANGED",
                    "个人模型连接已变更或删除，本轮失效；请重新发送消息",
                    status=409,
                )
            if current != binding.fingerprint:
                raise GatewayIdentityError(
                    "CONNECTION_CHANGED",
                    "工具令牌对应的连接已被替换，本轮失效；请重新发送消息",
                    status=409,
                )

            root_path = session["root_path"]
            if not root_path:
                raise GatewayIdentityError(
                    "NO_PROJECT_ROOT", "该项目还没有绑定目录，工具不能访问文件", status=409
                )
            self._require_grant(conn, int(turn["owner_id"]), str(root_path))

            # 模型以**本轮**下发给引擎的那个为准（turn.model），不是连接的当前值：
            # 偏好刚改过时两者可能不同，业务作业若按连接解析就会出现“聊天用 B、核查用 A”，
            # 而视觉证据只对其中一个成立。连接里的值只作旧数据的兜底。
            provider = str(turn["provider"] or (connection["provider"] if connection is not None else ""))
            model = str(turn["model"] or (connection["model"] if connection is not None else ""))

            return Actor(
                user_id=int(turn["owner_id"]),
                username=str(session["username"]),
                project_id=str(turn["project_id"]),
                project_name=str(session["project_name"]),
                session_id=str(turn["session_id"]),
                turn_id=str(turn["id"]),
                mode=policy.normalize_mode(turn["mode"]),
                root_path=Path(str(root_path)),
                connection_fingerprint=current,
                provider=provider,
                model=model,
            )
        finally:
            conn.close()

    @staticmethod
    def _require_grant(conn, user_id: int, root_path: str) -> None:
        """目录授权必须**每次**按当前授予集合复核，确认动作也不例外。"""
        from backend.services import roots as root_service

        granted = [item["path"] for item in root_service.list_for_user(conn, int(user_id))]
        if not any(paths.is_within(str(root_path), item) for item in granted):
            raise GatewayIdentityError(
                "GRANT_REVOKED",
                "该目录的授权已被收回，请让用户重新选择目录或联系管理员",
                status=403,
            )

    # ------------------------------------------------------------------ 调用

    def call(self, token: str, tool_name: str, args: Any, call_id: str) -> dict:
        binding = self.binding_for(token)
        if binding is None:
            raise ToolError("TOKEN_INVALID", "工具令牌无效或已过期", status=401)
        if not call_id or len(str(call_id)) > 120:
            raise ToolError("CALL_ID_REQUIRED", "缺少 call_id（用于幂等与状态查询）")

        conn = db.connect(self._settings.db_path)
        try:
            # 身份/上下文失效必须冒到 HTTP 层：它表示“这条会话不能再用”，
            # 与“这个工具调用参数不对/越界”是两类问题。
            actor = self.actor_for(binding)
            spec = tools.get(tool_name)
            cleaned = tools.validate_args(spec.schema, args)

            # 先抢执行权，再决定做什么：插入主键成功的那一个线程才允许往下走。
            job_row, claimed = store.claim_job(
                conn,
                job_id=str(call_id),
                turn_id=actor.turn_id,
                session_id=actor.session_id,
                owner_id=actor.user_id,
                tool=spec.name,
                args=cleaned,
            )
            if not claimed:
                job = store.job_public(job_row)
                if job["status"] == "running":
                    return {
                        "status": "running",
                        "job_id": call_id,
                        "tool": spec.name,
                        "note": (
                            "同一 call_id 仍被标记为执行中，未重复执行；"
                            "如果这一轮已经请求停止，该任务可能在进程收尾时才结束，"
                            "不要假设它一定完成了——用 hx_tool_status 查询真实状态。"
                        ),
                    }
                # 已完成/失败：原样返回上次结果，绝不重跑。
                return {
                    "status": "ok" if job["status"] == "done" else "error",
                    "job_id": call_id,
                    "tool": job["tool"],
                    "result": job["result"],
                    "error": job["error"],
                    "replayed": True,
                }

            ctx = tools.ToolContext(
                conn=conn,
                actor=actor,
                args=cleaned,
                emit=lambda type_, payload: store.append_event(
                    conn, actor.turn_id, actor.session_id, type_, payload
                ),
                jobs={},
                runtime=getattr(self, "runtime", None),
            )
            risk = spec.effective_risk(ctx)
            decision = policy.decide(actor.mode, risk)

            if decision.requires_confirmation:
                # 从身份校验到这里之间用户可能刚点了停止：那就不要再多造一张待确认卡。
                if self._stopped_during(conn, actor.turn_id):
                    raise GatewayIdentityError(
                        "STOP_REQUESTED",
                        "本轮已请求停止，不再新增待确认动作",
                        status=409,
                    )
                action = store.create_action(
                    conn,
                    turn_id=actor.turn_id,
                    session_id=actor.session_id,
                    project_id=actor.project_id,
                    owner_id=actor.user_id,
                    tool=spec.name,
                    risk=risk,
                    title=spec.title,
                    args=cleaned,
                    preview=spec.preview(cleaned),
                )
                payload = {
                    "status": "pending_confirmation",
                    "action_id": action["id"],
                    "title": spec.title,
                    "preview": action["preview"],
                    "risk": risk,
                    "note": (
                        "该动作需要用户在界面确认后才会真正执行。"
                        "请把已完成的只读结论说清楚并结束本轮，不要假设它已经成功。"
                    ),
                }
                store.job_finish(
                    conn, str(call_id), owner_id=actor.user_id, status="done", result=payload
                )
                store.append_event(
                    conn,
                    actor.turn_id,
                    actor.session_id,
                    "action",
                    {
                        "action_id": action["id"],
                        "tool": spec.name,
                        "title": spec.title,
                        "risk": risk,
                        "status": "proposed",
                        "preview": action["preview"],
                    },
                )
                self._audit(conn, actor, spec.name, risk, "proposed", cleaned)
                return {**payload, "job_id": call_id}

            result = spec.handler(ctx)
            store.job_finish(
                conn, str(call_id), owner_id=actor.user_id, status="done", result=result
            )
            if risk != policy.RISK_READ:
                self._audit(conn, actor, spec.name, risk, "executed", cleaned)
            payload = {"status": "ok", "job_id": call_id, "tool": spec.name, "result": result}
            # 执行期间用户请求了停止：这个动作已经真实发生，不能装作没发生，
            # 也不能因为“轮次标了 stopped”就对外说“所有业务都停了”。
            if self._stopped_during(conn, actor.turn_id):
                payload["stop_requested_during_execution"] = True
                payload["note"] = (
                    "用户在这条动作执行期间请求了停止。该动作已经实际执行完成，"
                    "不会回滚；本轮不会再发起新的动作。请在答复里如实说明这一点。"
                )
            return payload
        except ToolError as error:
            self._fail_job(conn, call_id, error, owner_id=binding.user_id)
            if isinstance(error, GatewayIdentityError):
                raise
            return {
                "status": "error",
                "job_id": call_id,
                "error": error.as_dict(),
                "http_status": error.status,
            }
        finally:
            conn.close()

    def execute_action(self, conn, action, *, actor_mode: str) -> dict:
        """用户确认后执行待办动作：状态先抢成 executing，重复点击不会执行两次。"""
        spec = tools.get(action["tool"])
        args = json.loads(action["args"] or "{}")
        actor_row = conn.execute(
            """
            SELECT t.owner_id, t.session_id, t.project_id, t.mode, t.connection_fingerprint,
                   t.status AS turn_status, t.stop_requested_at,
                   p.root_path, p.name AS project_name, u.username,
                   s.status AS session_status, p.status AS project_status, u.status AS user_status
              FROM turns t
              JOIN sessions s ON s.id = t.session_id
              JOIN projects p ON p.id = t.project_id
              JOIN users u ON u.id = t.owner_id
             WHERE t.id = ?
            """,
            (action["turn_id"],),
        ).fetchone()
        if actor_row is None or actor_row["user_status"] != "active":
            raise GatewayIdentityError("USER_DISABLED", "账号已停用，动作不能执行", status=403)
        # 确认必须检查**原操作**的状态：提案所属的那一轮已经被停止，
        # 就不能靠一张还开着的卡片把动作补执行掉。
        if actor_row["stop_requested_at"]:
            raise GatewayIdentityError(
                "STOP_REQUESTED", "该动作所属的轮次已被停止，不能再执行；请重新发起", status=409
            )
        if actor_row["turn_status"] in {"failed", "stopped"}:
            raise GatewayIdentityError(
                "TURN_CLOSED", "该动作所属的轮次已失败或被停止，不能再执行；请重新发起", status=409
            )
        if actor_row["session_status"] != "active" or actor_row["project_status"] != "active":
            raise GatewayIdentityError("CONTEXT_CLOSED", "会话或项目已删除，动作不能再执行", status=409)
        connection = identity.connection_for(conn, int(actor_row["owner_id"]))
        if identity.connection_fingerprint(connection) != actor_row["connection_fingerprint"]:
            raise GatewayIdentityError("CONNECTION_CHANGED", "个人连接已变更，该动作已失效，请重新发起", status=409)
        if not actor_row["root_path"]:
            raise GatewayIdentityError("NO_PROJECT_ROOT", "项目未绑定目录，动作不能执行", status=409)
        # 确认动作也必须按**当前**授权复核：提案之后被撤权的情况必须拦住。
        self._require_grant(conn, int(actor_row["owner_id"]), str(actor_row["root_path"]))

        root = Path(actor_row["root_path"]) if actor_row["root_path"] else None
        actor = Actor(
            user_id=int(actor_row["owner_id"]),
            username=str(actor_row["username"]),
            project_id=str(actor_row["project_id"]),
            project_name=str(actor_row["project_name"]),
            session_id=str(actor_row["session_id"]),
            turn_id=str(action["turn_id"]),
            mode=policy.normalize_mode(actor_mode or actor_row["mode"]),
            root_path=root,
            connection_fingerprint=str(actor_row["connection_fingerprint"]),
            provider=str(connection["provider"]) if connection is not None else "",
            model=str(connection["model"]) if connection is not None else "",
        )
        ctx = tools.ToolContext(
            conn=conn,
            actor=actor,
            args=tools.validate_args(spec.schema, args),
            emit=lambda type_, payload: store.append_event(
                conn, actor.turn_id, actor.session_id, type_, payload
            ),
            jobs={},
            runtime=getattr(self, "runtime", None),
        )
        if spec.requires_root:
            fsops.require_root(actor.root_path)
        result = spec.handler(ctx)
        self._audit(conn, actor, spec.name, action["risk"], "confirmed", ctx.args)
        return result

    def _stopped_during(self, conn, turn_id: str) -> bool:
        row = conn.execute(
            "SELECT stop_requested_at FROM turns WHERE id = ?", (str(turn_id),)
        ).fetchone()
        return bool(row is not None and row["stop_requested_at"])

    def _audit(self, conn, actor: Actor, tool: str, risk: str, stage: str, args: dict) -> None:
        from backend.services import audit

        detail = json.dumps(
            {"tool": tool, "risk": risk, "stage": stage, "mode": actor.mode, "args": args},
            ensure_ascii=False,
        )
        audit.record(
            conn,
            actor.user_id,
            f"agent.tool.{stage}",
            f"{actor.project_id}/{actor.session_id}",
            detail[:500],
        )

    def _fail_job(self, conn, call_id: str, error: ToolError, *, owner_id: int) -> None:
        try:
            row = conn.execute(
                "SELECT status, owner_id FROM tool_jobs WHERE id = ?", (str(call_id),)
            ).fetchone()
            if (
                row is not None
                and row["status"] == "running"
                and int(row["owner_id"]) == int(owner_id)
            ):
                store.job_finish(
                    conn,
                    str(call_id),
                    owner_id=int(owner_id),
                    status="failed",
                    error_code=error.code,
                    error_message=error.message,
                )
        except Exception:  # 记录失败不能影响返回给模型的结构化错误
            pass

    def job(self, token: str, job_id: str) -> dict:
        binding = self.binding_for(token)
        if binding is None:
            raise ToolError("TOKEN_INVALID", "工具令牌无效或已过期", status=401)
        conn = db.connect(self._settings.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM tool_jobs WHERE id = ? AND owner_id = ?",
                (str(job_id), binding.user_id),
            ).fetchone()
            if row is None:
                return {"status": "unknown", "job_id": job_id}
            return store.job_public(row)
        finally:
            conn.close()


class _GatewayHandler(BaseHTTPRequestHandler):
    gateway: ToolGateway
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 - 不打日志，避免把参数写进访问日志
        return

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _token(self) -> str:
        return self.headers.get(TOKEN_HEADER) or ""

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/v1/health":
            self._send(200, {"ok": True, "tools": len(tools.TOOLS)})
            return
        if path == "/v1/tools":
            if self.gateway.binding_for(self._token()) is None:
                self._send(401, {"ok": False, "error": {"code": "TOKEN_INVALID"}})
                return
            self._send(200, {"ok": True, "tools": tools.catalogue()})
            return
        if path.startswith("/v1/jobs/"):
            job_id = path[len("/v1/jobs/") :]
            try:
                self._send(200, {"ok": True, "job": self.gateway.job(self._token(), job_id)})
            except ToolError as error:
                self._send(error.status, {"ok": False, "error": error.as_dict()})
            return
        self._send(404, {"ok": False, "error": {"code": "NOT_FOUND"}})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not path.startswith("/v1/tools/"):
            self._send(404, {"ok": False, "error": {"code": "NOT_FOUND"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"ok": False, "error": {"code": "BAD_LENGTH"}})
            return
        if length > MAX_BODY:
            self._send(413, {"ok": False, "error": {"code": "BODY_TOO_LARGE"}})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"ok": False, "error": {"code": "BAD_JSON"}})
            return
        if not isinstance(body, dict):
            self._send(400, {"ok": False, "error": {"code": "BAD_JSON"}})
            return
        tool_name = path[len("/v1/tools/") :]
        try:
            result = self.gateway.call(
                self._token(), tool_name, body.get("args"), str(body.get("call_id") or "")
            )
        except ToolError as error:
            self._send(error.status, {"ok": False, "error": error.as_dict()})
            return
        self._send(200, {"ok": True, "data": result})

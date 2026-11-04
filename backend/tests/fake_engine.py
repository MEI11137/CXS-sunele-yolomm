"""测试专用假引擎：实现运行器用到的那几个 OpenCode HTTP 端点。

只在 ``allow_fake_engine=True`` 的测试配置里可用（生产默认禁止）。
它**不模拟模型行为**，只是按脚本把引擎会产生的消息形状（工具轮、最终文本、压缩摘要、
错误、abort）按顺序吐出来，用来验证运行器的判定逻辑与事件同步。
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeEngine:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.scripts: list[dict] = []
        self.prompts: list[dict] = []
        self.aborts: list[str] = []
        self.requests: list[tuple[str, str]] = []
        self._lock = threading.RLock()
        self._counter = 0
        self._server: ThreadingHTTPServer | None = None
        self.port = 0
        self.password = secrets.token_urlsafe(16)

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        engine = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # noqa: A003
                return

            def _auth_ok(self) -> bool:
                header = self.headers.get("Authorization") or ""
                if not header.startswith("Basic "):
                    return False
                try:
                    raw = base64.b64decode(header[6:]).decode()
                except Exception:
                    return False
                return raw == f"opencode:{engine.password}"

            def _send(self, status: int, payload: Any = None) -> None:
                body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                if payload is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                engine.requests.append(("GET", self.path))
                if not self._auth_ok():
                    self._send(401, {"error": "unauthorized"})
                    return
                path = self.path.split("?", 1)[0]
                if path == "/global/health":
                    self._send(200, {"healthy": True, "version": "1.19.4"})
                    return
                if path == "/session/status":
                    self._send(200, engine.status_map())
                    return
                parts = [item for item in path.split("/") if item]
                if len(parts) == 2 and parts[0] == "session":
                    # 会话存在性查询：真实引擎在会话不存在时回 404，运行器靠它
                    # 判断“映射里的 Native ID 还能不能用”。
                    session = engine.sessions.get(parts[1])
                    if session is None:
                        self._send(404, {"error": "not found"})
                        return
                    self._send(200, {"id": parts[1], "title": session.get("title", "")})
                    return
                if len(parts) == 3 and parts[0] == "session" and parts[2] == "message":
                    self._send(200, engine.messages(parts[1]))
                    return
                self._send(404, {"error": "not found"})

            def do_POST(self):  # noqa: N802
                engine.requests.append(("POST", self.path))
                if not self._auth_ok():
                    self._send(401, {"error": "unauthorized"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode() or "{}")
                except ValueError:
                    body = {}
                path = self.path.split("?", 1)[0]
                parts = [item for item in path.split("/") if item]
                if path == "/session":
                    sid = engine.create_session(body.get("title") or "")
                    self._send(200, {"id": sid, "title": body.get("title") or "", "version": "1.19.4"})
                    return
                if len(parts) == 3 and parts[0] == "session" and parts[2] == "prompt_async":
                    engine.prompt(parts[1], body)
                    self._send(204, None)
                    return
                if len(parts) == 3 and parts[0] == "session" and parts[2] == "abort":
                    engine.abort(parts[1])
                    self._send(200, True)
                    return
                self._send(404, {"error": "not found"})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        self.port = int(server.server_address[1])
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # ------------------------------------------------------------------ 存储

    def snapshot(self) -> dict:
        """把“磁盘上的会话存储”拍一份快照，用于模拟换进程后存储还在。"""
        with self._lock:
            return {
                "counter": self._counter,
                "sessions": {
                    sid: {
                        "title": session.get("title", ""),
                        "messages": [dict(item) for item in session["messages"]],
                        "state": "idle",
                    }
                    for sid, session in self.sessions.items()
                },
            }

    def restore(self, snapshot: dict) -> None:
        with self._lock:
            self._counter = int(snapshot.get("counter") or 0)
            self.sessions = {
                sid: {
                    "title": item.get("title", ""),
                    "messages": list(item["messages"]),
                    "state": "idle",
                }
                for sid, item in (snapshot.get("sessions") or {}).items()
            }

    # ------------------------------------------------------------------ 脚本

    def queue(self, *scripts: dict) -> None:
        with self._lock:
            self.scripts.extend(scripts)

    def _next_script(self) -> dict:
        with self._lock:
            return self.scripts.pop(0) if self.scripts else {"text": "默认回答"}

    def create_session(self, title: str) -> str:
        with self._lock:
            self._counter += 1
            sid = f"ses_fake{self._counter:012d}"
            self.sessions[sid] = {"title": title, "messages": [], "state": "idle", "script": None}
            return sid

    def messages(self, sid: str) -> list[dict]:
        with self._lock:
            session = self.sessions.get(sid)
            return list(session["messages"]) if session else []

    def status_map(self) -> dict:
        with self._lock:
            return {
                sid: {"type": "busy"}
                for sid, session in self.sessions.items()
                if session["state"] == "busy"
            }

    def abort(self, sid: str) -> None:
        with self._lock:
            self.aborts.append(sid)
            session = self.sessions.get(sid)
            if session is None:
                return
            session["aborted"] = True

    def prompt(self, sid: str, body: dict) -> None:
        text = ""
        for part in body.get("parts") or []:
            if part.get("type") == "text":
                text += str(part.get("text") or "")
        with self._lock:
            session = self.sessions[sid]
            session["messages"].append(
                {
                    "info": {
                        "id": f"msg_u{len(session['messages']):04d}",
                        "sessionID": sid,
                        "role": "user",
                        "time": {"created": int(time.time() * 1000)},
                    },
                    "parts": [{"type": "text", "text": text}],
                }
            )
            session["state"] = "busy"
            session["aborted"] = False
            script = self._next_script()
            session["script"] = script
        self.prompts.append({"session_id": sid, "text": text, **body})
        threading.Thread(target=self._run_script, args=(sid, script), daemon=True).start()

    def _append(self, sid: str, info: dict, parts: list[dict]) -> None:
        with self._lock:
            session = self.sessions.get(sid)
            if session is None:
                return
            session["messages"].append({"info": info, "parts": parts})

    def _run_script(self, sid: str, script: dict) -> None:
        session = self.sessions[sid]
        start = int(time.time() * 1000)
        try:
            if script.get("initial_delay"):
                time.sleep(float(script["initial_delay"]))
            if script.get("idle_blip"):
                # 模拟“短暂空闲”：状态先变 idle，再回到 busy。
                with self._lock:
                    session["state"] = "idle"
                time.sleep(float(script.get("blip_seconds", 0.6)))
                with self._lock:
                    session["state"] = "busy"

            if script.get("hang"):
                deadline = time.time() + 30
                while time.time() < deadline and not session.get("aborted"):
                    time.sleep(0.1)
                self._append(
                    sid,
                    {
                        "id": f"msg_a{int(time.time() * 1000)}",
                        "sessionID": sid,
                        "role": "assistant",
                        "time": {"created": start, "completed": int(time.time() * 1000)},
                        "error": {"name": "MessageAbortedError", "data": {"message": "Aborted"}},
                    },
                    [],
                )
                return

            if script.get("idle_before_text"):
                # 模拟“状态先变空闲、最终文本稍后才到”的引擎行为。
                with self._lock:
                    session["state"] = "idle"
                time.sleep(float(script.get("idle_gap", 2.0)))
                with self._lock:
                    session["state"] = "busy"

            if script.get("delay"):
                time.sleep(float(script["delay"]))

            if script.get("summary"):
                self._append(
                    sid,
                    {
                        "id": f"msg_s{int(time.time() * 1000)}",
                        "sessionID": sid,
                        "role": "assistant",
                        "summary": True,
                        "time": {"created": start, "completed": int(time.time() * 1000)},
                    },
                    [{"type": "text", "text": "这是内部压缩摘要，不应展示给用户。"}],
                )

            if script.get("tool_calls"):
                parts: list[dict] = []
                for index, call in enumerate(script["tool_calls"]):
                    parts.append(
                        {
                            "type": "tool",
                            "id": f"prt_t{start}{index}",
                            "callID": f"call_{index}",
                            "tool": call.get("tool", "hx17_hx_project_overview"),
                            "state": {
                                "status": call.get("state", "completed"),
                                "input": call.get("args") or {},
                                "output": call.get("output", "ok"),
                                "title": call.get("title", ""),
                                "time": {"start": start, "end": start + 12},
                            },
                        }
                    )
                parts.append({"type": "step-finish", "id": f"prt_f{start}", "reason": "tool-calls"})
                self._append(
                    sid,
                    {
                        "id": f"msg_t{start}",
                        "sessionID": sid,
                        "role": "assistant",
                        "time": {"created": start, "completed": int(time.time() * 1000)},
                    },
                    parts,
                )

            if script.get("error"):
                self._append(
                    sid,
                    {
                        "id": f"msg_e{start}",
                        "sessionID": sid,
                        "role": "assistant",
                        "time": {"created": start, "completed": int(time.time() * 1000)},
                        "error": script["error"],
                    },
                    [],
                )
                return

            if script.get("no_text"):
                self._append(
                    sid,
                    {
                        "id": f"msg_n{start}",
                        "sessionID": sid,
                        "role": "assistant",
                        "time": {"created": start, "completed": int(time.time() * 1000)},
                    },
                    [{"type": "step-finish", "id": f"prt_x{start}", "reason": "stop"}],
                )
                return

            text = str(script.get("text", ""))
            parts = [{"type": "text", "id": f"prt_r{start}", "text": text, "time": {"start": start}}]
            if script.get("synthetic_extra"):
                parts.append(
                    {"type": "text", "id": f"prt_y{start}", "text": "内部补充", "synthetic": True}
                )
            parts.append({"type": "step-finish", "id": f"prt_z{start}", "reason": script.get("reason", "stop")})
            self._append(
                sid,
                {
                    "id": f"msg_r{start}",
                    "sessionID": sid,
                    "role": "assistant",
                    "time": {"created": start, "completed": int(time.time() * 1000)},
                },
                parts,
            )
        finally:
            with self._lock:
                if session.get("state") == "busy":
                    session["state"] = "idle"


def fake_spawner(fake: FakeEngine):
    """返回一个可注入 EngineManager 的 spawner。"""

    def spawn(spec, work_dir):
        from backend.agent.engine import EngineHandle

        work_dir.mkdir(parents=True, exist_ok=True)
        return EngineHandle(
            port=fake.port, password=fake.password, process=None, work_dir=work_dir, version="1.19.4"
        )

    return spawn

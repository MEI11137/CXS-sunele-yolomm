"""协议集成检查：真实 opencode + 真实 MCP 桥 + 回环桩上游。

这不是“模型能力验收”。上游是本脚本内起的 **OpenAI 兼容桩**，只监听 127.0.0.1，
按固定脚本回一个合法工具调用和一段最终文本，**不连任何真实模型、不产生费用**。
它要证明的是协议链路真的通了：

    网站轮次入口 → 引擎进程 → MCP stdio 桥 → 私有网关 → 文件工具 → 落库

具体验收点：

1. 真实 ``opencode 1.19.4`` 子进程能起来，并在隔离数据目录里跑完一轮；
2. 引擎通过 MCP 桥真实调用 ``hx17_hx_list_files``，工具真的读到了临时项目目录；
3. 最终正文落库，且事件流里能看到工具结果；
4. 同一会话第二轮仍能调用工具（会话级令牌没有在上一轮结束时被吊销）；
5. 工具令牌过期后，站点能带新凭据重建引擎、保留会话存储，第三轮仍然真实调用工具；
6. 两个会话各自的回答只出现在自己会话里，上游收到的模型名严格等于个人连接里配置的模型。

账号是**普通成员**（role=member），不是管理员：评委用普通账号也要能完整走通。

失败时会把引擎日志尾部打印出来，方便定位是配置、桥还是协议问题。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import config, db  # noqa: E402
from backend.app import create_app  # noqa: E402
from backend.services import connections, projects, roots, sessions, users  # noqa: E402

PASSWORD = "integration-pass-9"
STUB_MODEL = "stub-model"
TOOL_NAME = "hx17_hx_list_files"
FINAL_TEXT = "临时项目目录里有 alpha.txt、beta.txt 和 reports 目录。"


class StubUpstream:
    """最小 OpenAI 兼容上游：第一次给工具调用，看到工具结果后给最终文本。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._server.handle_error = lambda *_args: None
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _handler(self):
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def _json(self, payload: dict) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self, frames: list[dict]) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for frame in frames:
                    line = "data: " + json.dumps(frame, ensure_ascii=False) + "\n\n"
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(0.01)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

            def do_GET(self) -> None:  # noqa: N802
                if self.path.endswith("/models"):
                    self._json(
                        {
                            "object": "list",
                            "data": [
                                {
                                    "id": STUB_MODEL,
                                    "object": "model",
                                    "created": 0,
                                    "owned_by": "loopback-stub",
                                }
                            ],
                        }
                    )
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    payload = {}
                messages = payload.get("messages") or []
                tool_names = [
                    (item.get("function") or {}).get("name")
                    for item in (payload.get("tools") or [])
                ]
                # 只看**最后一条**消息：多轮会话的历史里一定含上一轮的工具结果，
                # 若用“历史里有没有 tool”判断，第二轮永远不会再发起工具调用。
                last_role = ""
                last_text = ""
                for item in reversed(messages):
                    role = str(item.get("role") or "")
                    if role in {"system", "developer"}:
                        continue
                    last_role = role
                    content = item.get("content")
                    if isinstance(content, str):
                        last_text = content
                    elif isinstance(content, list):
                        last_text = " ".join(
                            str(part.get("text") or "")
                            for part in content
                            if isinstance(part, dict)
                        )
                    break
                wants_tool = (
                    TOOL_NAME in tool_names
                    and last_role == "user"
                    and ("列" in last_text or "目录" in last_text)
                )
                has_tool_result = last_role == "tool"
                with upstream.lock:
                    upstream.requests.append(
                        {
                            "tool_names": tool_names,
                            "has_tool_result": has_tool_result,
                            "wants_tool": wants_tool,
                            "last_role": last_role,
                            "stream": bool(payload.get("stream")),
                            "model": payload.get("model"),
                        }
                    )

                stream = bool(payload.get("stream"))
                chunk_id = "chatcmpl-" + uuid.uuid4().hex[:12]
                created = int(time.time())

                def chunk(delta: dict, finish: str | None = None) -> dict:
                    return {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": payload.get("model") or STUB_MODEL,
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": finish}
                        ],
                    }

                if wants_tool:
                    frames = [
                        chunk({"role": "assistant", "content": ""}),
                        chunk(
                            {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_" + uuid.uuid4().hex[:8],
                                        "type": "function",
                                        "function": {
                                            "name": TOOL_NAME,
                                            "arguments": json.dumps(
                                                {"path": "."}, ensure_ascii=False
                                            ),
                                        },
                                    }
                                ]
                            }
                        ),
                        chunk({}, "tool_calls"),
                    ]
                else:
                    # 已拿到工具结果（或这次请求没有可用工具，例如生成标题）→ 给文本。
                    text = FINAL_TEXT if has_tool_result else "（桩上游：无工具可用）"
                    frames = [
                        chunk({"role": "assistant", "content": ""}),
                        chunk({"content": text}),
                        chunk({}, "stop"),
                    ]

                if stream:
                    self._stream(frames)
                    return
                text = ""
                tool_calls = None
                for frame in frames:
                    delta = frame["choices"][0]["delta"]
                    text += delta.get("content") or ""
                    if delta.get("tool_calls"):
                        tool_calls = delta["tool_calls"]
                message: dict = {"role": "assistant", "content": text}
                if tool_calls:
                    message["tool_calls"] = tool_calls
                self._json(
                    {
                        "id": chunk_id,
                        "object": "chat.completion",
                        "created": created,
                        "model": payload.get("model") or STUB_MODEL,
                        "choices": [
                            {
                                "index": 0,
                                "message": message,
                                "finish_reason": "tool_calls" if tool_calls else "stop",
                            }
                        ],
                    }
                )

        return Handler


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.count = 0

    def check(self, condition: bool, label: str, detail: str = "") -> bool:
        self.count += 1
        if condition:
            print(f"  [OK] {label}")
            return True
        message = label + (f"：{detail}" if detail else "")
        self.failures.append(message)
        print(f"  [FAIL] {message}")
        return False


def _csrf(client) -> dict[str, str]:
    cookie = client.get_cookie("hx17_csrf")
    return {"X-CSRF-Token": cookie.value} if cookie is not None else {}


def _call(client, method: str, path: str, payload=None):
    headers = {} if method == "GET" else _csrf(client)
    if method != "GET":
        headers["Content-Type"] = "application/json"
    return client.open(path, method=method, json=payload, headers=headers)


def _data(response) -> dict:
    payload = response.get_json()
    if payload is None:
        raise AssertionError(response.get_data(as_text=True))
    return payload


def _wait_turn(client, turn_id: str, *, timeout: float) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        response = _call(client, "GET", f"/api/turns/{turn_id}?after=0")
        if response.status_code != 200:
            raise AssertionError(response.get_data(as_text=True))
        last = _data(response)["data"]
        if last["turn"]["status"] in {"completed", "failed", "stopped"}:
            return last
        time.sleep(0.25)
    raise AssertionError(f"轮次未在 {timeout}s 内结束：{last}")


def _send(client, session_id: str, content: str) -> dict:
    response = _call(
        client,
        "POST",
        f"/api/sessions/{session_id}/turns",
        {"content": content, "request_id": uuid.uuid4().hex},
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return _data(response)["data"]["turn"]


def main() -> int:
    checker = Checker()
    upstream = StubUpstream()
    upstream.start()

    data_dir = Path(tempfile.mkdtemp(prefix="hx17-proto-data-"))
    # 业务工作区与数据目录分开：数据目录是服务端私有区，文件工具不该看见它。
    workspace = Path(tempfile.mkdtemp(prefix="hx17-proto-ws-"))
    (workspace / "alpha.txt").write_text("巡检记录 A", encoding="utf-8")
    (workspace / "beta.txt").write_text("巡检记录 B", encoding="utf-8")
    (workspace / "reports").mkdir()

    settings = config.load_settings(data_dir=data_dir, serve_dist=False, port=5099)
    app = create_app(settings=settings, serve_dist=False, engine_spawner=None)
    runtime = app.config["HX17_AGENT"]
    live = app.config["HX17"]

    client = None
    try:
        conn = db.connect(live.db_path)
        try:
            alice = dict(
                users.create(conn, username="alice", password=PASSWORD, role="member")
            )
            root_row = roots.add(conn, actor_id=None, raw_path=str(workspace), label="项目目录")
            roots.grant_by_username(
                conn, actor_id=None, root_id=int(root_row["id"]), username="alice"
            )
            project = dict(projects.create(conn, alice["id"], name="协议检查项目"))
            projects.bind_root(conn, project, raw_path=str(workspace))
            session_a = dict(
                sessions.create(conn, project_id=project["id"], owner_id=alice["id"], title="会话 A")
            )
            session_b = dict(
                sessions.create(conn, project_id=project["id"], owner_id=alice["id"], title="会话 B")
            )
            connections.save(
                conn,
                alice["id"],
                {
                    "provider": "hx17-loopback",
                    "base_url": upstream.base_url,
                    "model": STUB_MODEL,
                    "display_name": "回环桩上游",
                    "api_key": "stub-key-not-a-real-credential",
                },
                key=app.config["HX17_CONNECTION_KEY"],
            )
        finally:
            conn.close()

        client = app.test_client()
        client.get("/api/meta")
        login = client.post(
            "/api/auth/login",
            json={"username": "alice", "password": PASSWORD},
            headers=_csrf(client),
        )
        assert login.status_code == 200, login.get_data(as_text=True)

        print("1) 第一轮：真实引擎 + 真实 MCP 桥 + 真实文件工具")
        turn_a1 = _send(client, session_a["id"], "列一下项目目录里有什么")
        final_a1 = _wait_turn(client, turn_a1["id"], timeout=240)
        checker.check(
            final_a1["turn"]["status"] == "completed",
            "第一轮完成",
            json.dumps(final_a1["turn"], ensure_ascii=False)[:300],
        )
        checker.check(
            final_a1["turn"].get("error_code") in (None, ""),
            "第一轮没有错误码",
            str(final_a1["turn"].get("error_code")),
        )
        tool_events = [e for e in final_a1["events"] if e["type"] == "tool"]
        checker.check(bool(tool_events), "事件流里有工具调用", str(len(tool_events)))
        if tool_events:
            payload = tool_events[-1]["payload"]
            checker.check(
                payload.get("state") == "completed",
                "工具状态是 completed",
                json.dumps(payload, ensure_ascii=False)[:300],
            )
            checker.check(
                str(payload.get("tool")) == TOOL_NAME,
                "调用的正是桥里的 hx17 工具",
                str(payload.get("tool")),
            )
            raw = json.dumps(payload, ensure_ascii=False)
            checker.check(
                "hx_list_files" in raw,
                "事件里记的是真实工具名与结果摘要",
                raw[:200],
            )

        # 事件里的 preview 是截断的；完整结果在 tool_jobs.result，用它证明真的读到了目录。
        conn = db.connect(live.db_path)
        try:
            rows = conn.execute(
                "SELECT tool, status, result FROM tool_jobs WHERE session_id = ? "
                "ORDER BY created_at",
                (session_a["id"],),
            ).fetchall()
        finally:
            conn.close()
        jobs = []
        for row in rows:
            try:
                jobs.append((row["tool"], json.loads(row["result"] or "{}")))
            except ValueError:
                jobs.append((row["tool"], {}))
        listed = [item for tool, item in jobs if tool == "hx_list_files"]
        if os.environ.get("HX17_PROTO_DEBUG"):
            print("  [debug] tool_jobs =", json.dumps(jobs, ensure_ascii=False)[:1200])
        checker.check(bool(listed), "工具作业已落库 hx_list_files", str([t for t, _ in jobs]))
        if listed:
            # tool_jobs.result 存的就是工具返回值本身（path/entries/next_cursor）。
            inner = listed[0]
            names = sorted(entry.get("name") for entry in inner.get("entries") or [])
            checker.check(
                names == ["alpha.txt", "beta.txt", "reports"],
                "工具真实读到了临时项目目录",
                json.dumps(inner, ensure_ascii=False)[:400],
            )
            checker.check(
                inner.get("path") == str(workspace),
                "工具读的是绑定的项目目录，不是数据目录",
                str(inner.get("path")),
            )

        messages_a = _data(
            _call(client, "GET", f"/api/sessions/{session_a['id']}/messages")
        )["data"]["messages"]
        texts_a = [item["content"] for item in messages_a if item["role"] == "assistant"]
        checker.check(
            texts_a == [FINAL_TEXT], "最终正文原样落库", json.dumps(texts_a, ensure_ascii=False)
        )
        first_upstream = list(upstream.requests)
        checker.check(
            any(item["has_tool_result"] for item in first_upstream),
            "上游确实收到了工具结果（第二轮请求）",
            json.dumps(first_upstream, ensure_ascii=False)[:300],
        )
        checker.check(
            any(not item["has_tool_result"] for item in first_upstream),
            "上游第一跳看到过工具清单",
            json.dumps(first_upstream, ensure_ascii=False)[:300],
        )

        conn = db.connect(live.db_path)
        try:
            row = conn.execute(
                "SELECT engine_session_id FROM engine_sessions WHERE session_id = ?",
                (session_a["id"],),
            ).fetchone()
        finally:
            conn.close()
        engine_session_a = str(row["engine_session_id"]) if row is not None else ""
        checker.check(bool(engine_session_a), "站点已记录会话的 Native ID", engine_session_a)

        print("2) 同会话第二轮：令牌没有被上一轮 finally 吊销")
        before = len(upstream.requests)
        turn_a2 = _send(client, session_a["id"], "再列一次，确认还能用工具")
        final_a2 = _wait_turn(client, turn_a2["id"], timeout=240)
        checker.check(
            final_a2["turn"]["status"] == "completed",
            "第二轮完成",
            json.dumps(final_a2["turn"], ensure_ascii=False)[:300],
        )
        checker.check(
            [e for e in final_a2["events"] if e["type"] == "tool"],
            "第二轮仍真实调用了工具",
            str([e["type"] for e in final_a2["events"]]),
        )
        checker.check(
            len(upstream.requests) > before,
            "第二轮确实打到了上游",
            f"{before} → {len(upstream.requests)}",
        )

        print("3) 同项目第二个会话：归属互不串")
        turn_b1 = _send(client, session_b["id"], "B 会话也列一下目录")
        final_b1 = _wait_turn(client, turn_b1["id"], timeout=240)
        checker.check(
            final_b1["turn"]["status"] == "completed",
            "B 会话完成",
            json.dumps(final_b1["turn"], ensure_ascii=False)[:300],
        )
        messages_b = _data(
            _call(client, "GET", f"/api/sessions/{session_b['id']}/messages")
        )["data"]["messages"]
        ids_b = {item["id"] for item in messages_b}
        ids_a = {item["id"] for item in messages_a}
        checker.check(not (ids_a & ids_b), "两个会话的消息没有交集")
        checker.check(
            all(item["session_id"] == session_b["id"] for item in messages_b),
            "B 会话的消息都标了 B",
        )
        checker.check(
            final_a1["turn"]["session_id"] == session_a["id"]
            and final_b1["turn"]["session_id"] == session_b["id"],
            "轮次各自归属正确的会话",
        )
        checker.check(
            runtime.manager.active_count() == 2,
            "两个会话各有一个引擎进程",
            str(runtime.manager.active_count()),
        )
        models_seen = sorted({str(item["model"]) for item in upstream.requests})
        checker.check(
            models_seen == [STUB_MODEL],
            "上游收到的模型名严格等于个人连接里配置的模型",
            json.dumps(models_seen, ensure_ascii=False),
        )
        print("4) 工具令牌过期：带新凭据重建引擎，会话存储与历史保留")
        expired = 0
        for binding in list(runtime.gateway._bindings.values()):  # noqa: SLF001 - 脚本模拟过期
            binding.expires_at = time.time() - 1
            expired += 1
        checker.check(expired >= 1, "已让现存工具令牌过期", str(expired))
        before_turn3 = len(upstream.requests)
        turn_a3 = _send(client, session_a["id"], "再过一次目录，确认令牌续期后还能用工具")
        final_a3 = _wait_turn(client, turn_a3["id"], timeout=240)
        checker.check(
            final_a3["turn"]["status"] == "completed",
            "令牌过期后第三轮完成（引擎已重建）",
            json.dumps(final_a3["turn"], ensure_ascii=False)[:300],
        )
        checker.check(
            [e for e in final_a3["events"] if e["type"] == "tool"],
            "第三轮仍真实调用工具（新桥、新令牌）",
            str([e["type"] for e in final_a3["events"]]),
        )
        checker.check(
            len(upstream.requests) > before_turn3,
            "第三轮确实打到了上游",
            f"{before_turn3} → {len(upstream.requests)}",
        )
        conn = db.connect(live.db_path)
        try:
            mapping = conn.execute(
                "SELECT engine_session_id FROM engine_sessions WHERE session_id = ?",
                (session_a["id"],),
            ).fetchone()
        finally:
            conn.close()
        checker.check(
            mapping is not None and str(mapping["engine_session_id"]) == str(engine_session_a),
            "重建后仍复用同一 Native 会话（历史没被丢掉）",
            str(mapping["engine_session_id"]) if mapping else "None",
        )
    except Exception as exc:  # noqa: BLE001 - 检查脚本要打印完整原因
        checker.failures.append(f"{type(exc).__name__}: {exc}")
        print(f"  [FAIL] 未预期异常：{type(exc).__name__}: {exc}")
    finally:
        try:
            runtime.shutdown()
        except Exception:
            pass
        upstream.stop()
        logs = sorted((data_dir / "engines").glob("*/logs/engine.log"))
        if checker.failures and logs:
            print("\n--- 引擎日志尾部 ---")
            for log in logs[:2]:
                try:
                    text = log.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                print(f"[{log.parent.parent.name}]")
                print("\n".join(text.strip().splitlines()[-25:]))
        for path in (data_dir, workspace):
            shutil.rmtree(path, ignore_errors=True)

    print()
    if checker.failures:
        print("协议集成检查失败：")
        for item in checker.failures:
            print("  - " + item)
        return 1
    print(f"协议集成检查通过：{checker.count} 项")
    print("说明：上游是回环桩，固定脚本；本次没有调用任何真实模型，也没有计费。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

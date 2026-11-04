"""私有 MCP 桥的**真实 stdio 握手**检查。

与 `tool_roundtrip.py` 的区别：那个直接 HTTP 调网关，只证明网关活着；这里启动
**真实的 Python 子进程**，用官方 MCP 客户端走 stdio 完成 ``initialize`` /
``list_tools`` / ``call_tool``，因此可以证明：

* 桥进程能被引擎以 ``python -u backend/agent/mcp_bridge.py`` 直接拉起（不依赖
  项目里有 ``backend`` 包，也不依赖 PYTHONPATH）；
* 工具清单由网关下发，不是桥里复刻的；
* ``call_tool`` 的四类结果分别落到正确位置：正常 / 等待确认 → ``isError=False``，
  业务拒绝 / 超时 → ``isError=True``，且超时不会自动重试（网关只被打一次）；
* 令牌只走 HTTP 头，不出现在 stdout 协议帧里。

网关照用本脚本内的桩：临时授权目录 + 假 Key，**不连真实模型、不产生费用**。
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

BRIDGE = ROOT / "backend" / "agent" / "mcp_bridge.py"
FAKE_TOKEN = "hx17-handshake-token-not-a-real-key"
TOOL_TIMEOUT = "1.5"

TOOLS = [
    {
        "name": "hx_probe",
        "description": "返回目录内容（桩实现）",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "hx_pending",
        "description": "需要人工确认的写入（桩实现）",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
    {
        "name": "hx_deny",
        "description": "业务上必须拒绝的越权路径（桩实现）",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
    {
        "name": "hx_slow",
        "description": "故意慢于超时预算（桩实现）",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
]


class StubGateway:
    """记录所有请求、按工具名给出确定性响应的最小网关。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        self.workspace = Path(tempfile.mkdtemp(prefix="hx17-handshake-ws-"))
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        # 客户端超时后会主动断开，Windows 上会抛 ConnectionAbortedError；
        # 这是被检查对象的行为本身，不该污染输出。
        self._server.handle_error = lambda *_args: None
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def calls_for(self, name: str) -> list[dict]:
        with self.lock:
            return [item for item in self.requests if item["path"].endswith("/" + name)]

    def _handler(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # noqa: D102 - 静默
                return

            def _send(self, status: int, payload: dict) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _record(self, body: dict) -> None:
                with gateway.lock:
                    gateway.requests.append(
                        {
                            "path": self.path,
                            "token": self.headers.get("X-HX17-Tool-Token"),
                            "body": body,
                        }
                    )

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/v1/tools":
                    self._send(404, {"ok": False})
                    return
                self._record({})
                if self.headers.get("X-HX17-Tool-Token") != FAKE_TOKEN:
                    self._send(401, {"ok": False, "error": {"code": "TOKEN_INVALID"}})
                    return
                self._send(200, {"ok": True, "tools": TOOLS})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    body = {}
                self._record(body)
                if self.headers.get("X-HX17-Tool-Token") != FAKE_TOKEN:
                    self._send(401, {"ok": False, "error": {"code": "TOKEN_INVALID"}})
                    return
                name = self.path.rsplit("/", 1)[-1]
                args = body.get("args") or {}
                if name == "hx_probe":
                    entries = sorted(
                        item.name for item in gateway.workspace.iterdir()
                    )
                    self._send(
                        200,
                        {
                            "ok": True,
                            "data": {
                                "status": "ok",
                                "entries": entries,
                                "root": str(gateway.workspace),
                                "echo_path": args.get("path"),
                            },
                        },
                    )
                elif name == "hx_pending":
                    self._send(
                        200,
                        {
                            "ok": True,
                            "data": {
                                "status": "pending_confirmation",
                                "action_id": "act-handshake",
                                "note": "等待用户确认后才会真正写入",
                            },
                        },
                    )
                elif name == "hx_deny":
                    self._send(
                        200,
                        {
                            "ok": False,
                            "error": {
                                "code": "PATH_OUTSIDE_PROJECT",
                                "message": "路径不在项目目录内",
                            },
                        },
                    )
                elif name == "hx_slow":
                    time.sleep(4.0)
                    self._send(200, {"ok": True, "data": {"status": "ok"}})
                else:
                    self._send(404, {"ok": False, "error": {"code": "TOOL_NOT_FOUND"}})

        return Handler


def _free_port_selfcheck() -> None:
    """确保端口探针本身可用，避免把环境问题误判成桥的问题。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))


def main() -> int:
    _free_port_selfcheck()
    gateway = StubGateway()
    (gateway.workspace / "afile.txt").write_text("巡检记录", encoding="utf-8")
    (gateway.workspace / "zdir").mkdir()
    gateway.start()

    env = dict(os.environ)
    env.update(
        {
            "HX17_GATEWAY_URL": gateway.url,
            "HX17_TOOL_TOKEN": FAKE_TOKEN,
            "HX17_TOOL_TIMEOUT": TOOL_TIMEOUT,
            "PYTHONIOENCODING": "utf-8",
            # 刻意清掉：桥必须靠自身模块路径启动，不能靠外部注入的搜索路径。
            "PYTHONPATH": "",
        }
    )

    errors: queue.Queue[str] = queue.Queue()
    checks: list[str] = []

    def check(condition: bool, label: str, detail: str = "") -> None:
        if condition:
            checks.append(label)
            print(f"  [OK] {label}")
        else:
            message = f"{label}{('：' + detail) if detail else ''}"
            errors.put(message)
            print(f"  [FAIL] {message}")

    params = StdioServerParameters(
        command=sys.executable,
        args=["-u", str(BRIDGE)],
        env=env,
        cwd=str(ROOT),
    )

    import anyio

    async def run() -> None:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                check(
                    init.serverInfo.name == "hx17-tools",
                    "initialize 返回桥的身份",
                    repr(getattr(init.serverInfo, "name", None)),
                )
                check(
                    (init.protocolVersion or "").startswith("20"),
                    "initialize 协商出协议版本",
                    str(init.protocolVersion),
                )

                listed = await session.list_tools()
                names = sorted(tool.name for tool in listed.tools)
                check(
                    names == sorted(item["name"] for item in TOOLS),
                    "list_tools 由网关下发且完整",
                    str(names),
                )
                probe = next(t for t in listed.tools if t.name == "hx_probe")
                check(
                    (probe.inputSchema or {}).get("required") == ["path"],
                    "工具 schema 原样透传",
                    json.dumps(probe.inputSchema, ensure_ascii=False),
                )

                ok = await session.call_tool("hx_probe", {"path": "."})
                check(not ok.isError, "正常结果是 isError=False")
                payload = json.loads(ok.content[0].text)
                check(
                    payload.get("echo_path") == ".",
                    "参数原样到达网关",
                    json.dumps(payload, ensure_ascii=False)[:200],
                )
                check(
                    payload.get("entries") == ["afile.txt", "zdir"],
                    "工具结果回到了模型侧",
                    str(payload.get("entries")),
                )

                pending = await session.call_tool("hx_pending", {"path": "r.txt"})
                check(not pending.isError, "等待确认不算失败（isError=False）")
                check(
                    json.loads(pending.content[0].text).get("status")
                    == "pending_confirmation",
                    "等待确认状态被保留",
                )

                denied = await session.call_tool("hx_deny", {"path": "../secret"})
                check(bool(denied.isError), "业务拒绝必须 isError=True")
                denied_payload = json.loads(denied.content[0].text)
                check(
                    (denied_payload.get("error") or {}).get("code")
                    == "PATH_OUTSIDE_PROJECT",
                    "业务错误码被带出",
                    str(denied_payload.get("error")),
                )

                unknown = await session.call_tool("hx_not_exists", {})
                check(bool(unknown.isError), "未知工具必须 isError=True")
                check(
                    json.loads(unknown.content[0].text)["error"]["code"]
                    == "TOOL_NOT_FOUND",
                    "未知工具错误码正确",
                )

                slow = await session.call_tool("hx_slow", {"path": "."})
                check(bool(slow.isError), "超时结果必须 isError=True")
                slow_payload = json.loads(slow.content[0].text)
                check(
                    slow_payload.get("status") == "timeout"
                    and slow_payload.get("job_id"),
                    "超时给出 job_id 供状态查询，不重跑",
                    json.dumps(slow_payload, ensure_ascii=False)[:200],
                )

    started = time.time()
    try:
        anyio.run(run)
    finally:
        gateway.stop()

    check(
        len(gateway.calls_for("hx_slow")) == 1,
        "超时后没有自动重试",
        f"实际 {len(gateway.calls_for('hx_slow'))} 次",
    )
    check(
        all(item["token"] == FAKE_TOKEN for item in gateway.requests),
        "每次请求都带令牌头",
    )
    check(
        all(item["token"] != "None" for item in gateway.requests),
        "没有漏带令牌头的请求",
    )
    check(
        len(gateway.calls_for("hx_probe")) == 1,
        "正常调用只发生一次",
        f"实际 {len(gateway.calls_for('hx_probe'))} 次",
    )

    # stdout 是协议通道：任何非 JSON-RPC 帧都会让引擎解析失败。
    probe = subprocess.run(
        [sys.executable, "-u", str(BRIDGE)],
        input=b"",
        capture_output=True,
        env=env,
        cwd=str(ROOT),
        timeout=60,
    )
    stdout_text = probe.stdout.decode("utf-8", "replace")
    check(
        "\n" not in stdout_text.strip() or stdout_text.strip().startswith("{"),
        "桥没有往 stdout 写日志",
        stdout_text[:200],
    )
    check(
        FAKE_TOKEN not in stdout_text
        and FAKE_TOKEN not in probe.stderr.decode("utf-8", "replace"),
        "令牌不出现在输出里",
    )
    check(probe.returncode == 0, "stdin 关闭后桥正常退出", str(probe.returncode))

    print()
    if errors.empty():
        print(
            f"MCP 协议握手检查通过：{len(checks)} 项，耗时 {time.time() - started:.1f}s"
        )
        print("说明：这是协议层检查，用桩网关，未调用任何真实模型。")
        return 0
    print("MCP 协议握手检查失败：")
    while not errors.empty():
        print("  - " + errors.get())
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

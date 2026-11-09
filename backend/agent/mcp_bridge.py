"""私有 stdio MCP 桥：opencode 通过它调用业务工具。

这个进程由引擎以子进程方式启动，只做三件事：

1. 带着引擎专属令牌向私有网关拉取工具清单（schema 只有服务端一份，不在这里复刻）；
2. 把模型的调用原样转发给网关，并把结构化结果回给模型；
3. 区分「连不上网关 / 网关业务拒绝 / 超时 / 结果未知」四种情况，
   绝不笼统地说“工具未连接”。

令牌只从环境变量读取，既不出现在工具参数里，也不写日志。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from typing import Any

import anyio
import requests

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

SERVER_NAME = "hx17-tools"
SERVER_VERSION = "1.8.0"
TOKEN_HEADER = "X-HX17-Tool-Token"
GATEWAY = os.environ.get("HX17_GATEWAY_URL", "").rstrip("/")
TOKEN = os.environ.get("HX17_TOOL_TOKEN", "")
CALL_TIMEOUT = float(os.environ.get("HX17_TOOL_TIMEOUT", "90"))


def _log(message: str) -> None:
    # stderr 是 MCP 的日志通道；stdout 只能走协议帧。
    sys.stderr.write("[hx17-bridge] " + message + "\n")
    sys.stderr.flush()


def _headers() -> dict[str, str]:
    return {TOKEN_HEADER: TOKEN, "Content-Type": "application/json"}


def _fetch_tools() -> list[dict]:
    last: Exception | None = None
    for _ in range(3):
        try:
            response = requests.get(
                GATEWAY + "/v1/tools", headers=_headers(), timeout=(3.0, 10.0)
            )
            if response.status_code == 200:
                return list((response.json() or {}).get("tools") or [])
            last = RuntimeError(f"HTTP {response.status_code}")
        except requests.RequestException as exc:
            last = exc
        import time

        time.sleep(0.3)
    _log(f"工具清单拉取失败：{type(last).__name__}")
    return []


def _error_payload(code: str, message: str, *, job_id: str | None = None) -> str:
    payload: dict[str, Any] = {"status": "error", "error": {"code": code, "message": message}}
    if job_id:
        payload["job_id"] = job_id
    return json.dumps(payload, ensure_ascii=False)


def _forward(name: str, arguments: dict) -> tuple[str, bool]:
    """返回 (给模型的 JSON 文本, 是否属于“这次调用失败”)。

    四类结果必须分开，否则界面会把“传输成功但业务失败”标成绿色：
    ``ok`` / ``pending_confirmation`` 是正常结果；``rejected`` / ``timeout`` /
    网关不可达 / 令牌失效都是失败，MCP 结果要带 ``isError``。
    """
    call_id = uuid.uuid4().hex
    if not GATEWAY:
        return (
            _error_payload("BRIDGE_NOT_CONFIGURED", "工具桥没有配置网关地址，无法执行"),
            True,
        )
    try:
        response = requests.post(
            f"{GATEWAY}/v1/tools/{name}",
            headers=_headers(),
            json={"args": arguments or {}, "call_id": call_id},
            timeout=(5.0, CALL_TIMEOUT),
        )
    except requests.Timeout:
        # 结果未知：给出 job_id 供状态查询，绝不让调用方重跑一遍。
        return (
            json.dumps(
                {
                    "status": "timeout",
                    "job_id": call_id,
                    "error": {
                        "code": "TOOL_TIMEOUT",
                        "message": "等待工具结果超时，执行结果未知。请用 hx_tool_status 查询该 job_id，不要重复执行。",
                    },
                },
                ensure_ascii=False,
            ),
            True,
        )
    except requests.RequestException as exc:
        return (
            _error_payload(
                "GATEWAY_UNREACHABLE",
                f"连不上本机工具网关（{type(exc).__name__}）。这不是业务错误，请让用户检查服务端。",
            ),
            True,
        )

    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code == 401:
        return (_error_payload("TOKEN_INVALID", "工具令牌无效或已过期，本轮已失效"), True)
    if not isinstance(body, dict):
        return (_error_payload("GATEWAY_BAD_RESPONSE", "工具网关返回了无法解析的内容"), True)
    if not body.get("ok"):
        error = body.get("error") or {}
        return (
            json.dumps(
                {
                    "status": "rejected",
                    "job_id": call_id,
                    "error": {
                        "code": str(error.get("code") or "TOOL_REJECTED"),
                        "message": str(error.get("message") or "工具拒绝了该调用"),
                    },
                },
                ensure_ascii=False,
            ),
            True,
        )
    data = body.get("data") or {}
    status = str(data.get("status") or "ok")
    # 等待确认、正在执行都是**正常**返回，不是错误；模型应按 note 结束或稍后查询。
    return json.dumps(data, ensure_ascii=False), status in {"error", "rejected"}


def build_server(tool_specs: list[dict]) -> Server:
    server = Server(SERVER_NAME)
    # 网关不可达时也要能启动，只是清单为空：模型会因此直接回答，而不是被隐藏能力。
    tools = [
        types.Tool(
            name=str(item["name"]),
            description=str(item.get("description") or ""),
            inputSchema=item.get("inputSchema") or {"type": "object", "properties": {}},
        )
        for item in tool_specs
    ]

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return tools

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> types.CallToolResult:
        known = {item.name for item in tools}
        if name not in known:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=_error_payload("TOOL_NOT_FOUND", f"没有这个工具：{name}"),
                    )
                ],
                isError=True,
            )
        text, failed = await anyio.to_thread.run_sync(_forward, name, dict(arguments or {}))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], isError=bool(failed)
        )

    return server


async def _main() -> None:
    specs = await anyio.to_thread.run_sync(_fetch_tools)
    _log(f"已加载 {len(specs)} 个工具")
    server = build_server(specs)
    options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, options, raise_exceptions=False)


def main() -> int:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

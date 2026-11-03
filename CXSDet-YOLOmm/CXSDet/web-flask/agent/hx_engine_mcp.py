"""海巡引擎 的私有 stdio MCP：具体业务工具 → 站点权限/确认层。

仅继承一次启动的服务端凭据；stdout 只输出 MCP 协议，不导入 Flask app。
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import urllib.error
import urllib.request

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations


def relay(method, path, payload=None):
    origin = os.environ["HAIXUN_BRIDGE_ORIGIN"].rstrip("/")
    token = os.environ["HAIXUN_BRIDGE_TOKEN"]
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(origin + "/api/agent/engine/bridge" + path,
        data=data, method=method, headers={"Authorization": "Bearer " + token,
                                          "Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=90) as response:
        return json.load(response)


def call_relay(name, arguments):
    try:
        return relay('POST', '/call', {'name': name, 'arguments': arguments or {}})
    except urllib.error.HTTPError as exc:
        # Preserve structured business errors; never turn an HTTP 500 into
        # "tool not connected", or expose HTML tracebacks and credentials.
        try:
            body = json.load(exc)
            error = body.get('error') if isinstance(body, dict) else None
            if isinstance(error, dict) and isinstance(error.get('code'), str):
                return {'error': error}
        except (ValueError, OSError):
            pass
        return {'error': {'code': 'AGENT_BRIDGE_HTTP_ERROR',
            'message': f'业务工具返回 HTTP {exc.code}，请核对服务端日志；不要重复提交写入',
            'execution_state': 'unknown'}}
    except (TimeoutError, socket.timeout):
        return {'error': {'code': 'AGENT_BRIDGE_TIMEOUT',
            'message': '业务工具响应超时，结果待核实，请先查询状态，不要重复提交写入',
            'execution_state': 'unknown'}}
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return {'error': {'code': 'AGENT_BRIDGE_TIMEOUT',
                'message': '业务工具响应超时，结果待核实，请先查询状态，不要重复提交写入',
                'execution_state': 'unknown'}}
        return {'error': {'code': 'AGENT_BRIDGE_UNAVAILABLE',
            'message': '业务工具连接不可用，请检查服务器私有桥接服务；不要重复提交写入',
            'execution_state': 'unknown'}}
    except Exception:
        return {'error': {'code': 'AGENT_BRIDGE_UNAVAILABLE',
            'message': '业务工具连接不可用，请检查服务器私有桥接服务；不要重复提交写入',
            'execution_state': 'unknown'}}


async def main():
    server = Server("haixun_mcp")

    @server.list_tools()
    async def list_tools():
        data = await asyncio.to_thread(relay, "GET", "/tools")
        return [Tool(name=item["name"], description=item["description"],
                     inputSchema=item["parameters"],
                     annotations=ToolAnnotations(readOnlyHint=item["readonly"],
                         destructiveHint=not item["readonly"], openWorldHint=True))
                for item in data["tools"]]

    @server.call_tool()
    async def call_tool(name, arguments):
        data = await asyncio.to_thread(call_relay, name, arguments)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
                              structuredContent=data, isError=bool(data.get("error")))

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

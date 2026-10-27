"""OpenCode 的私有 stdio MCP：具体业务工具 → 站点权限/确认层。

仅继承一次启动的服务端凭据；stdout 只输出 MCP 协议，不导入 Flask app。
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations


def relay(method, path, payload=None):
    origin = os.environ["HAIXUN_BRIDGE_ORIGIN"].rstrip("/")
    token = os.environ["HAIXUN_BRIDGE_TOKEN"]
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(origin + "/api/agent/opencode/bridge" + path,
        data=data, method=method, headers={"Authorization": "Bearer " + token,
                                          "Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=90) as response:
        return json.load(response)


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
        try:
            data = await asyncio.to_thread(relay, "POST", "/call",
                                           {"name": name, "arguments": arguments or {}})
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
                                  structuredContent=data, isError=bool(data.get("error")))
        except Exception:
            # 不回显 URL、令牌、请求体或模型 Key。
            return CallToolResult(content=[TextContent(type="text", text="海巡智检工具连接失败；请在网站核对状态，不要重复提交写入。")], isError=True)

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

"""Bounded MCP v1 SDK sessions; each operation closes its streams/process.

Only manually saved connections reach this factory. No model-controlled command,
shell, environment or credential argument is accepted by the Agent tools.
"""
import asyncio
import ipaddress
from contextlib import AsyncExitStack
from datetime import timedelta
from urllib.parse import urlsplit

from services.mcp_registry import McpUnavailable


class SdkTransport:
    def __init__(self, config):
        self.config = config

    async def _run(self, method, name=None, arguments=None):
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from mcp.client.streamable_http import streamable_http_client
            import httpx
        except ImportError as exc:
            raise McpUnavailable("请安装 requirements-agent.txt 中的 MCP 依赖") from exc
        cfg = self.config
        async with asyncio.timeout(45), AsyncExitStack() as stack:
            if cfg["transport"] == "stdio":
                # The SDK uses exec, never a shell. It inherits its own restricted
                # default environment; only this connection's credential is added.
                env = {"MCP_API_KEY": cfg["credential"]} if cfg.get("credential") else None
                streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
                    command=cfg["command"], args=cfg.get("args") or [], env=env,
                )))
                read, write = streams
            else:
                endpoint = cfg.get("endpoint") or ""
                parts = urlsplit(endpoint)
                if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.fragment or parts.query:
                    raise McpUnavailable("MCP 地址须为不含凭据、查询参数或片段的 HTTP(S) 地址")
                headers = {"Authorization": "Bearer " + cfg["credential"]} if cfg.get("credential") else None
                try:
                    loopback = ipaddress.ip_address(parts.hostname).is_loopback
                except ValueError:
                    loopback = parts.hostname.lower() == 'localhost'
                client = await stack.enter_async_context(httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30, read=40), follow_redirects=False, trust_env=not loopback))
                read, write, _ = await stack.enter_async_context(streamable_http_client(endpoint, http_client=client))
            session = await stack.enter_async_context(ClientSession(read, write, read_timeout_seconds=timedelta(seconds=35)))
            await session.initialize()
            if method == "list":
                tools, cursor = [], None
                for _ in range(20):
                    page = await session.list_tools(cursor=cursor)
                    tools.extend(t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in page.tools)
                    cursor = page.nextCursor
                    if not cursor:
                        return tools
                raise McpUnavailable("工具清单超过 20 页，请缩小服务范围")
            result = await session.call_tool(name, arguments or {})
            return result.model_dump(mode="json", by_alias=True, exclude_none=True)

    def list_tools(self):
        return asyncio.run(self._run("list"))

    def call_tool(self, name, arguments):
        return asyncio.run(self._run("call", name, arguments))


def transport_factory(config):
    return SdkTransport(config)


class PersonalMcpSecrets:
    def get(self, owner_id, reference):
        from agent.identity import load_actor
        from agent.connections import read_credential
        value = read_credential(load_actor(int(owner_id)), "mcp", reference)
        if not value:
            raise McpUnavailable("该 MCP 连接的个人凭据不存在，请重新填写")
        return value

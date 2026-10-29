"""Codex / DeepSeek Harness 通用 stdio 开发桥。令牌由用户显式创建并通过环境变量注入。"""
import asyncio
import json
import os
import urllib.request
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations


def relay(path, payload=None):
    from agent.opencode_engine import local_origin
    origin = local_origin(os.environ.get('HAIXUN_DEV_ORIGIN', 'http://127.0.0.1:5020'))
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(origin + '/api/agent/dev/' + path, data=data,
        headers={'Authorization': 'Bearer ' + os.environ['HAIXUN_DEV_TOKEN'], 'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=35) as response:
        return json.load(response)['data']


async def main():
    server = Server('haixun-development')
    @server.list_tools()
    async def list_tools():
        return [Tool(name='haixun_dev_state', description='读取获授权项目的独立开发会话与内部 Agent 真实任务状态',
            inputSchema={'type': 'object', 'properties': {}, 'additionalProperties': False},
            annotations=ToolAnnotations(readOnlyHint=True)),
            Tool(name='haixun_dev_message', description='向网站内部 OpenCode 发送开发诊断消息，会使用网站已配置的模型。只发送一次，不代替业务执行、操作批准或人工标注。',
                inputSchema={'type': 'object', 'properties': {'text': {'type': 'string'}, 'request_id': {'type': 'string'}},
                             'required': ['text', 'request_id'], 'additionalProperties': False},
                annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))]
    @server.call_tool()
    async def call_tool(name, arguments):
        try:
            if name not in ('haixun_dev_state', 'haixun_dev_message'):
                raise ValueError('unknown tool')
            result = await asyncio.to_thread(relay, 'state' if name == 'haixun_dev_state' else 'message',
                                            None if name == 'haixun_dev_state' else arguments)
            return CallToolResult(content=[TextContent(type='text', text=json.dumps(result, ensure_ascii=False))])
        except Exception:
            return CallToolResult(isError=True, content=[TextContent(type='text', text='开发连接失败或任务正等待网页确认。请查看网站真实状态；不要换请求ID重发，不要提供模型 Key。')])
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == '__main__':
    asyncio.run(main())

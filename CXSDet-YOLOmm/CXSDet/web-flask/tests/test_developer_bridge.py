"""开发连接只授权一个项目下的独立会话，外部客户端不能批准或代替人工。"""
from unittest.mock import Mock, patch
from tests.test_inspection_loop import setup
from agent import store
from agent.dev_bridge import dev_bp
from agent.routes import agent_bp


def test_project_scoped_expiring_development_connection_and_message_dedup(setup):
    f = setup
    f.app.register_blueprint(dev_bp)
    f.app.register_blueprint(agent_bp)
    client = f.app.test_client()
    with patch('agent.routes.get_user_id', return_value=1):
        response = client.post('/api/agent/dev/connections', headers={'Authorization': 'Bearer fixture'},
            json={'project_id': f.project['id'], 'source': 'codex'})
    assert response.status_code == 200
    connection = response.json['data']; token = connection['token']
    headers = {'Authorization': 'Bearer ' + token}
    assert client.get('/api/agent/dev/state', headers=headers).json['data']['project_id'] == f.project['id']
    assert client.get('/api/agent/sessions', headers=headers).status_code == 401
    assert client.post('/api/agent/dev/message', headers=headers, json={'text': 'approve', 'request_id': 'a', 'approved': True}).status_code == 400
    runtime = Mock()
    runtime.handle_message.return_value = {'status': 'running', 'task': None}
    with patch('agent.dev_bridge.get_runtime', return_value=runtime):
        for _ in range(2):
            assert client.post('/api/agent/dev/message', headers=headers, json={'text': '检查当前闭环缺哪一步', 'request_id': 'one'}).status_code == 200
        assert runtime.handle_message.call_count == 1
        assert '开发调试消息' in runtime.handle_message.call_args.args[2]
        assert client.post('/api/agent/dev/message', headers=headers, json={'text': '变更原请求', 'request_id': 'one'}).status_code == 409
    conn = store.connect()
    try:
        row = conn.execute('SELECT * FROM agent_dev_connection WHERE id=?', (connection['connection_id'],)).fetchone()
        assert token not in str(dict(row))
    finally:
        conn.close()
    with patch('agent.routes.get_user_id', return_value=1):
        assert client.delete('/api/agent/dev/connections/' + connection['connection_id'], headers={'Authorization': 'Bearer fixture'}).status_code == 200
    assert client.get('/api/agent/dev/state', headers=headers).status_code == 401


def test_developer_stdio_exposes_only_diagnostic_tools():
    import asyncio
    import sys
    from pathlib import Path
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    async def inspect():
        script = Path(__file__).resolve().parents[1] / 'agent/developer_mcp.py'
        async with stdio_client(StdioServerParameters(command=sys.executable, args=[str(script)])) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.list_tools()
                assert {tool.name for tool in result.tools} == {'haixun_dev_state', 'haixun_dev_message'}
    asyncio.run(inspect())

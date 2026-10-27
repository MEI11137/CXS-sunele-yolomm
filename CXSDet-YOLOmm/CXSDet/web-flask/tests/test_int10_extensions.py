"""Real stdio/HTTP MCP handshakes, stale confirmations and personal isolation."""
import os
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from services.mcp_registry import McpStore
from services.mcp_transport import transport_factory

SERVER = Path(__file__).parent / 'fixtures' / 'mcp_echo_server.py'


def server_command(port=None):
    # Test dependencies can live in a separate target directory; no shell.
    script = f'import sys,runpy,site; sys.path[:0]={sys.path!r}; [site.addsitedir(p) for p in list(sys.path) if p.endswith(".integration-deps")]; sys.argv={[str(SERVER)] + ([str(port)] if port else [])!r}; runpy.run_path({str(SERVER)!r},run_name="__main__")'
    return [sys.executable, '-c', script]


@pytest.mark.parametrize('transport', ['stdio', 'http'])
def test_actual_mcp_handshake_discovery_call_and_cleanup(tmp_path, transport):
    pytest.importorskip('mcp')
    process = None
    log = None
    command = server_command()
    payload = {'name': 'local echo fixture', 'transport': transport, 'command': command[0], 'arguments': command[1:]}
    if transport == 'http':
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        log = open(tmp_path/'mcp_server.log', 'w', encoding='utf-8')
        process = subprocess.Popen(server_command(port), stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for _ in range(100):
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.1): break
            except OSError:
                if process.poll() is not None: pytest.fail('test MCP server exited')
                time.sleep(.05)
        payload['endpoint'] = f'http://127.0.0.1:{port}/mcp'
    try:
        store = McpStore(tmp_path/'app.sqlite', transport_factory=transport_factory)
        connection = store.save(7, payload)
        try:
            discovered = store.discover(7, connection['id'])
        except Exception as exc:
            if log: log.flush()
            pytest.fail(f'{type(exc).__name__}: {exc}; server log: ' + ((tmp_path/'mcp_server.log').read_text(encoding='utf-8', errors='replace') if log else 'stdio'))
        assert [t['name'] for t in discovered['tools']] == ['echo']
        result = store.invoke(7, connection['id'], 'echo', {'text': '海巡智检 protocol test'})
        returned = result['result'].get('structuredContent') or json.loads(result['result']['content'][0]['text'])
        assert returned['text'] == '海巡智检 protocol test'
        with pytest.raises(PermissionError): store.get(connection['id'], 8)
        old_version = discovered['tools_version']
        changed = store.save(7, {**payload, 'id': connection['id'], 'enabled': False})
        assert changed['revision'] == 2 and changed['tools_version'] is None
        assert store.list_tools(7, connection['id']) == []
        assert old_version
    finally:
        if process:
            process.terminate(); process.wait(timeout=10)
        if log: log.close()


def test_risk_changes_invalidate_tool_version_and_unknown_writes_are_not_replayed(tmp_path):
    tools = [{'name': 'lookup', 'inputSchema': {'type': 'object'}, 'annotations': {'readOnlyHint': True}}]
    calls = []
    def call(*args):
        calls.append(args)
        raise TimeoutError('secret must not be stored')
    store = McpStore(tmp_path/'app.sqlite', transport_factory=lambda _: SimpleNamespace(list_tools=lambda: tools, call_tool=call))
    connection = store.save(1, {'name': 'fixture', 'transport': 'stdio', 'command': sys.executable, 'arguments': ['-V']})
    v1 = store.discover(1, connection['id'])['tools_version']
    tools[0]['annotations'] = {'readOnlyHint': False, 'idempotentHint': True}
    v2 = store.discover(1, connection['id'])['tools_version']
    assert v1 != v2
    with pytest.raises(RuntimeError, match='未知'):
        store.invoke(1, connection['id'], 'lookup', {})
    assert len(calls) == 1
    assert 'secret' not in str(store.get(connection['id'], 1))

"""原生权限自动领取前的新鲜度校验：模式/连接/工作区/待确认参数变化都不得自动执行。"""
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

from agent import actions, sessions
from agent.hx_engine_runtime import HxEngineRuntime
from tests.test_inspection_loop import setup


def _credentials(model='fake-model'):
    return SimpleNamespace(model=model, provider='haixun', base_url='http://127.0.0.1:1',
                           api_key='test-key', context_window=8192, output_tokens=1024)


def _make_engine(f, permission, fingerprint):
    replies = []

    def request(method, path, payload=None):
        if method == 'GET' and path == '/permission':
            return permission
        if method == 'POST' and path.startswith('/permission/') and path.endswith('/reply'):
            replies.append((path, payload))
            return {}
        if path.endswith('/message') or path == '/session/status':
            return {}
        return []

    engine = SimpleNamespace(
        task_id=f.task['id'], site_session=f.chat['id'], remote_session='remote',
        project_id=f.project['id'], workspace=Path(f.root), fingerprint=fingerprint,
        generation=1, busy=True, request=request, replies=replies,
        redact=lambda value: value, abort=lambda: None, lock=threading.Lock(),
        native_actions={}, permission_decisions={}, turn_notes=[])
    return engine


def _pending_action(runtime, engine, f):
    runtime._permissions(engine, f.actor, f.task['id'])
    pending = actions.list_actions(f.task['id'], [actions.STATUS_PENDING])
    assert len(pending) == 1, [item['tool_title'] for item in pending]
    return pending[0]


def _prepare(setup, monkeypatch):
    f = setup
    runtime = HxEngineRuntime(f.app)
    creds = _credentials()
    fingerprint = hashlib.sha256(json.dumps(creds.__dict__, sort_keys=True).encode()).hexdigest()
    monkeypatch.setattr('agent.connections.provider_credentials', lambda actor: creds)
    permission = [{'id': 'perm-1', 'sessionID': 'remote', 'permission': 'bash',
                   'patterns': ["Remove-Item -LiteralPath 'x.txt'"],
                   'metadata': {'command': "Remove-Item -LiteralPath 'x.txt'"},
                   'tool': {'callID': 'call-1'}}]
    engine = _make_engine(f, permission, fingerprint)
    monkeypatch.setattr(runtime.pool, 'find', lambda *args, **kwargs: engine)
    sessions.set_permission_mode(f.actor, f.chat['id'], 'request')
    return f, runtime, engine, permission


def _assert_stale(runtime, engine, f, action_id):
    assert runtime._confirm_native_policy(engine, f.actor, action_id, 'full', 'write', '测试') is False
    assert actions.get_action(f.actor, action_id)['status'] == actions.STATUS_REJECTED
    assert engine.replies and engine.replies[-1][1] == {'reply': 'reject'}
    assert any('未自动执行' in note for note in engine.turn_notes)


def test_stale_when_session_mode_changed_after_precheck(setup, monkeypatch):
    f, runtime, engine, _ = _prepare(setup, monkeypatch)
    action = _pending_action(runtime, engine, f)
    sessions.set_permission_mode(f.actor, f.chat['id'], 'assist')
    _assert_stale(runtime, engine, f, action['id'])


def test_stale_when_provider_connection_changed_after_precheck(setup, monkeypatch):
    f, runtime, engine, _ = _prepare(setup, monkeypatch)
    action = _pending_action(runtime, engine, f)
    other = _credentials(model='another-model')
    monkeypatch.setattr('agent.connections.provider_credentials', lambda actor: other)
    _assert_stale(runtime, engine, f, action['id'])


def test_stale_when_project_workspace_rebound_after_precheck(setup, monkeypatch):
    f, runtime, engine, _ = _prepare(setup, monkeypatch)
    action = _pending_action(runtime, engine, f)
    monkeypatch.setattr('agent.project_folders.checked_project_root',
                        lambda actor, project: Path(f.root).parent / '另外绑定目录')
    _assert_stale(runtime, engine, f, action['id'])


def test_stale_when_pending_arguments_changed_after_precheck(setup, monkeypatch):
    f, runtime, engine, permission = _prepare(setup, monkeypatch)
    action = _pending_action(runtime, engine, f)
    permission[0] = dict(permission[0], metadata={'command': "Remove-Item -LiteralPath 'y.txt'"})
    _assert_stale(runtime, engine, f, action['id'])
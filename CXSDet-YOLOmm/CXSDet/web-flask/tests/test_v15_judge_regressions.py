"""Regressions from the real v1.4 judge logs; isolated DB and model doubles."""
import io
import json
import threading
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from agent import actions, identity, permissions, sessions, tasks
from agent.errors import AgentError
from agent.hx_engine_runtime import HxEngineRuntime, hxengine_bp
from agent.llm.provider import ToolCall
from agent.runtime import LoopState
from tests.test_inspection_loop import setup, start
from tests.test_business_approval_modes import invoke


def test_ordinary_owner_can_change_modes_but_not_other_sessions(setup):
    f = setup
    f.db.execute('UPDATE user SET role=0 WHERE id=1')
    actor = identity.load_actor(1)
    for mode in ('request', 'assist', 'full'):
        assert sessions.set_permission_mode(actor, f.chat['id'], mode)['permission_mode'] == mode
    with pytest.raises(AgentError):
        sessions.set_permission_mode(identity.ActorContext(2), f.chat['id'], 'full')


def test_local_rgb_ir_registration_detection_and_legacy_asset_without_plan(setup):
    f = setup
    run = start(f)
    f.db.execute('UPDATE user SET role=0 WHERE id=1')
    f.actor = identity.load_actor(1)
    f.app.config['DEMO_WORKSPACE_ROOT'] = str(f.root)
    sessions.set_permission_mode(f.actor, f.chat['id'], 'assist')
    runtime = HxEngineRuntime(f.app)
    task = tasks.create_task(f.actor, f.chat['id'], f.project['id'], '登记RGB与IR后检测')
    registered = []
    for modality in ('images', 'images_ir'):
        assert invoke(f, runtime, task, 'business_file_register_local', {
            'project_id': f.project['id'], 'path': f'dataset/{modality}/train/0.png', 'kind': 'image'}) is None
        operation = actions.list_actions(task['id'])[-1]
        assert operation['status'] == actions.STATUS_SUCCEEDED
        registered.append(operation['result']['outputs']['asset_id'])
    rows = f.db.query('SELECT kind FROM file_assets WHERE id IN (?,?)', tuple(registered))
    assert [row['kind'] for row in rows] == ['image', 'image']
    # Existing v1.4 image uploads work too, after content validation.
    f.db.execute("UPDATE file_assets SET kind='project_file' WHERE id=?", (registered[0],))
    before = len(f.db.query('SELECT id FROM detection_records'))
    assert invoke(f, runtime, task, 'business_detection_run', {
        'asset_id': registered[0], 'secondary_asset_id': registered[1],
        'model_id': run['state']['model_id'], 'confidence': .25,
        'output_project_id': f.project['id']}) is None
    assert len(f.db.query('SELECT id FROM detection_records')) == before + 1
    assert not actions.list_actions(task['id'], [actions.STATUS_PENDING])
    assert tasks.get_task(f.actor, task['id'])['plan_version'] == 0


def test_deleted_model_index_does_not_break_loop_with_existing_model(setup):
    f = setup
    run = start(f)
    permissions.register_resource(f.actor, project_id=f.project['id'], kind='model',
        display_name='deleted legacy model', source_table='models', source_id='missing-business-row')
    args = {**f.args, 'model_id': run['state']['model_id']}
    args.pop('weight_path')
    prepared = f.provider.prepare(f.actor, f.specs['workspace_start_inspection_loop'], args, f.context)
    assert prepared['preview']['reuse']['model_id'] == run['state']['model_id']


def test_cached_plan_call_is_noop_without_version_growth(setup):
    f = setup
    runtime = HxEngineRuntime(f.app)
    for _ in range(3):
        result = invoke(f, runtime, f.task, 'propose_plan', {'steps': [{'title': 'read'}]})
        assert result['status'] == 'continue'
    assert tasks.get_task(f.actor, f.task['id'])['plan_version'] == 0


def test_bridge_missing_business_row_is_structured_not_connection_failure(setup, monkeypatch):
    f = setup
    runtime = HxEngineRuntime(f.app)
    engine = SimpleNamespace(lock=threading.RLock(), busy=True, task_id=f.task['id'],
        site_session=f.chat['id'], project_id=f.project['id'], redact=lambda value: value)
    monkeypatch.setattr('agent.hx_engine_runtime._bridge_engine', lambda: (runtime, engine, f.actor))
    monkeypatch.setattr(runtime, '_handle_tool_call', lambda *args: (_ for _ in ()).throw(FileNotFoundError('private path')))
    f.app.register_blueprint(hxengine_bp)
    reply = f.app.test_client().post('/api/agent/engine/bridge/call', json={
        'name': 'workspace_list_resources', 'arguments': {'project_id': f.project['id']}})
    assert reply.status_code == 200
    assert reply.json['error']['code'] == 'AGENT_RESOURCE_NOT_FOUND'
    assert 'private path' not in reply.get_data(as_text=True)


def test_mcp_preserves_server_error_and_distinguishes_timeout():
    from agent.hx_engine_mcp import call_relay
    payload = {'error': {'code': 'AGENT_RESOURCE_NOT_FOUND', 'message': '已删除', 'execution_state': 'not_executed'}}
    error = urllib.error.HTTPError('http://private', 500, 'bad', {}, io.BytesIO(json.dumps(payload).encode()))
    with patch('agent.hx_engine_mcp.relay', side_effect=error):
        assert call_relay('tool', {}) == payload
    with patch('agent.hx_engine_mcp.relay', side_effect=TimeoutError()):
        assert call_relay('tool', {})['error']['code'] == 'AGENT_BRIDGE_TIMEOUT'
    with patch('agent.hx_engine_mcp.relay', side_effect=urllib.error.URLError(TimeoutError())):
        assert call_relay('tool', {})['error']['code'] == 'AGENT_BRIDGE_TIMEOUT'


def test_intermediate_messages_are_distinct_from_final_and_never_reasoning(setup):
    f = setup
    runtime = HxEngineRuntime(f.app)
    engine = SimpleNamespace(site_session=f.chat['id'], redact=lambda value: value)
    runtime._save_message(engine, f.task['id'], 'mid', '检查文件中', intermediate=True)
    runtime._save_message(engine, f.task['id'], 'final', '检测完成')
    messages = sessions.list_messages(f.chat['id'])
    assert messages[-2]['refs'] == [{'type': 'execution_note'}]
    assert messages[-1]['refs'] == []


def test_ordinary_file_deletion_needs_approval_in_every_mode(setup, monkeypatch):
    f = setup
    f.db.execute('UPDATE user SET role=0 WHERE id=1')
    actor = identity.load_actor(1)
    f.app.config['DEMO_WORKSPACE_ROOT'] = str(f.root)
    runtime = HxEngineRuntime(f.app)
    approved = []
    monkeypatch.setattr(runtime, 'confirm_action', lambda *args, **kwargs: approved.append(args))
    target = str(f.source / 'labels/train/0.txt')
    for mode in ('request', 'assist', 'full'):
        sessions.set_permission_mode(actor, f.chat['id'], mode)
        item = {'id': mode, 'sessionID': 'remote', 'permission': 'edit',
            'metadata': {'files': [{'filePath': target, 'type': 'delete'}], 'diff': '+++ /dev/null'}}
        engine = SimpleNamespace(site_session=f.chat['id'], project_id=f.project['id'],
            remote_session='remote', workspace=f.root.resolve(), permission_decisions={},
            native_actions={}, fingerprint='fixture', redact=lambda value: value,
            request=lambda method, path, *args: [item] if path == '/permission' else None)
        runtime._permissions(engine, actor, f.task['id'])
    assert not approved
    assert len(actions.list_actions(f.task['id'], [actions.STATUS_PENDING])) == 3


def test_unknown_or_mixed_commands_cannot_gain_remote_approval(setup, monkeypatch):
    from agent.native_policy import confined_permission
    f = setup
    target = str(f.source / 'labels/train/0.txt')
    assert not confined_permission({}, {'kind': 'unknown', 'targets': [target]}, f.root)
    item = {'permission': 'bash', 'metadata': {'command': 'untrusted mixed command'}}
    monkeypatch.setattr('agent.native_policy.inspect_command', lambda _: {
        'valid': True, 'commands': [{'name': 'Remove-Item'}, {'name': 'Invoke-Expression'}]})
    assert not confined_permission(item, {'kind': 'delete', 'targets': [target]}, f.root)

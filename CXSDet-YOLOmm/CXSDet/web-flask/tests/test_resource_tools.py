"""业务回收站与对话工具使用临时数据；不删除用户文件或调用真实模型。"""
import pytest

from agent import identity, permissions, store
from agent.errors import AgentError
from agent.resource_tools import ResourceTools, MANAGE, QUERY, SAMPLES
from services import resource_lifecycle as lifecycle
from tests.test_inspection_loop import setup, start
from tests.test_review_agent_tools import card, execute


def model_and_dataset(f):
    run = start(f)
    model_id = run['state']['model_id']
    dataset_id = f.db.query('SELECT dataset_id FROM models WHERE id=?', (model_id,), fetchone=True)['dataset_id']
    return model_id, dataset_id


def test_legacy_record_alias_query_resolve_and_delete_share_real_source(setup):
    from agent.resource_tools import DELETE_RECORDS
    from agent.project_tools import ProjectTools
    from services.record_lifecycle import set_deleted_many
    f = setup; start(f)
    ids = [row['id'] for row in f.db.query('SELECT id FROM detection_records ORDER BY id')]
    old, removed, current = ids
    for key in (old, removed):
        f.db.execute("UPDATE agent_resource SET kind='record' WHERE source_table='detection_records' AND source_id=?", (key,))
        f.db.execute("UPDATE detection_records SET title='历史检测',create_time='2026-10-27T22:42:19' WHERE id=?", (key,))
    set_deleted_many(f.db, [{'record_id': removed, 'expected_version': 0}], f.actor.user_id)
    provider = ResourceTools(); query = next(tool for tool in provider.list_tools(f.actor) if tool.name == QUERY)
    args = {'project_id': f.project['id'], 'kind': 'detection_record'}
    result = provider.execute(f.actor, query, args, request_key='active', context=f.context).outputs
    assert {row['id'] for row in result['items']} == {old, current}
    dated = provider.execute(f.actor, query, {**args, 'search': '2026-10-27'}, request_key='dated', context=f.context).outputs
    assert [row['id'] for row in dated['items']] == [old]
    assert dated['items'][0]['create_time'] == '2026-10-27T22:42:19'
    assert dated['items'][0]['record_status'] != 'deleted' and not dated['items'][0]['is_deleted']
    trash = provider.execute(f.actor, query, {**args, 'include_deleted': True}, request_key='trash', context=f.context).outputs
    deleted = next(row for row in trash['items'] if row['id'] == removed)
    assert deleted['is_deleted'] and deleted['record_status'] == 'deleted'
    assert permissions.resolve_resource(f.actor, {'kind': 'detection_record', 'id': old})['source_id'] == old
    assert {row['source_id'] for row in permissions.list_project_resources(f.actor, f.project['id'], ['detection_record'])} == set(ids)
    project_provider = ProjectTools()
    list_spec = next(tool for tool in project_provider.list_tools(f.actor) if tool.name == 'workspace_list_resources')
    listed = project_provider.execute(f.actor, list_spec, {'project_id': f.project['id']}, request_key='project-list', context=f.context).outputs['resources']
    assert removed not in {row['source_id'] for row in listed}
    assert old in {row['source_id'] for row in listed}
    delete_spec = next(tool for tool in provider.list_tools(f.actor) if tool.name == DELETE_RECORDS)
    arguments = {'project_id': f.project['id'], 'records': [{'record_id': old, 'expected_version': 0}]}
    prepared, action = card(f, provider, delete_spec, arguments)
    assert prepared['preview']['records'][0]['record_id'] == old
    with pytest.raises(AgentError):
        provider.execute(f.actor, delete_spec, arguments, request_key=action['request_key'], prepared_id=prepared['prepared_id'], context=f.context)
    execute(f, provider, delete_spec, arguments, prepared, action)
    assert f.db.query('SELECT record_status FROM detection_records WHERE id=?', (old,), fetchone=True)['record_status'] == 'deleted'
    assert f.db.query('SELECT record_status FROM detection_records WHERE id=?', (current,), fetchone=True)['record_status'] != 'deleted'


def test_legacy_draft_alias_can_be_found_and_resolved_without_moving_project(setup):
    from services.dataset_draft_service import create_draft
    f = setup
    draft = create_draft({'name': '旧候选', 'creator_id': f.actor.user_id}, f.db)
    resource = permissions.register_resource(f.actor, project_id=f.project['id'], kind='dataset_draft', display_name=draft.name,
        source_table='dataset_drafts', source_id=draft.id)
    provider = ResourceTools(); query = next(tool for tool in provider.list_tools(f.actor) if tool.name == QUERY)
    result = provider.execute(f.actor, query, {'project_id': f.project['id'], 'kind': 'draft'}, request_key='draft', context=f.context).outputs
    assert [row['id'] for row in result['items']] == [draft.id]
    assert permissions.resolve_resource(f.actor, {'kind': 'draft', 'id': draft.id})['id'] == resource['id']
    assert permissions.resolve_resource(f.actor, {'kind': 'draft', 'id': draft.id})['project_id'] == f.project['id']


def test_delete_model_requires_unpublish_then_keeps_files_and_can_restore(setup):
    f = setup
    model_id, dataset_id = model_and_dataset(f)
    project_id = f.project['id']
    preview = lifecycle.preview(f.actor, project_id, 'model', model_id, 'delete', task_id=f.task['id'])
    assert any('撤销发布' in reason for reason in preview['blocked_reasons'])
    with pytest.raises(AgentError):
        lifecycle.change(f.actor, project_id, 'model', model_id, 'delete', preview['revision'], request_key='published')
    f.db.execute("UPDATE models SET status=1,publish_status='archived' WHERE id=?", (model_id,))
    provider = ResourceTools(); spec = next(tool for tool in provider.list_tools(f.actor) if tool.name == MANAGE)
    arguments = {'project_id': project_id, 'kind': 'model', 'resource_id': model_id, 'operation': 'delete', 'expected_revision': preview['revision']}
    prepared, action = card(f, provider, spec, arguments)
    with pytest.raises(AgentError):
        provider.execute(f.actor, spec, arguments, request_key=action['request_key'], prepared_id=prepared['prepared_id'], context=f.context)
    result = execute(f, provider, spec, arguments, prepared, action).outputs
    assert result['deleted_at'] and result['recoverable'] and result['files_preserved']
    assert (f.root / 'best.pt').is_file()
    assert f.db.query('SELECT id FROM models WHERE id=?', (model_id,), fetchone=True)
    with pytest.raises((AgentError, PermissionError)):
        f.backend.resolve(f.actor.user_id, 'model', model_id, ('model:read',))
    assert model_id not in {r['source_id'] for r in permissions.list_project_resources(f.actor, project_id)}
    restored = lifecycle.change(f.actor, project_id, 'model', model_id, 'restore', result['revision'], request_key='restore-model', task_id=f.task['id'])
    assert restored['deleted_at'] is None
    assert f.db.query('SELECT publish_status FROM models WHERE id=?', (model_id,), fetchone=True)['publish_status'] == 'archived'
    assert f.backend.resolve(f.actor.user_id, 'model', model_id, ('model:read',))


def test_dataset_dependencies_and_restore_order_are_checked(setup):
    f = setup; model_id, dataset_id = model_and_dataset(f); project_id = f.project['id']
    preview = lifecycle.preview(f.actor, project_id, 'dataset', dataset_id, 'delete', task_id=f.task['id'])
    assert any(model_id in reason for reason in preview['blocked_reasons'])
    with pytest.raises(AgentError):
        lifecycle.change(f.actor, project_id, 'dataset', dataset_id, 'delete', preview['revision'], request_key='in-use')
    f.db.execute("UPDATE models SET status=1,publish_status='archived' WHERE id=?", (model_id,))
    model_preview = lifecycle.preview(f.actor, project_id, 'model', model_id, 'delete', task_id=f.task['id'])
    deleted_model = lifecycle.change(f.actor, project_id, 'model', model_id, 'delete', model_preview['revision'], request_key='delete-model')
    deleted = lifecycle.change(f.actor, project_id, 'dataset', dataset_id, 'delete', preview['revision'], request_key='delete-dataset')
    assert lifecycle.list_deleted(f.actor, project_id)
    assert (f.source / 'data.yaml').is_file()
    with pytest.raises(AgentError):
        lifecycle.change(f.actor, project_id, 'model', model_id, 'restore', deleted_model['revision'], request_key='wrong-order')
    same = lifecycle.change(f.actor, project_id, 'dataset', dataset_id, 'delete', preview['revision'], request_key='delete-dataset')
    assert same == deleted
    with pytest.raises(AgentError):
        lifecycle.change(f.actor, project_id, 'dataset', dataset_id, 'restore', preview['revision'], request_key='stale')
    restored = lifecycle.change(f.actor, project_id, 'dataset', dataset_id, 'restore', deleted['revision'], request_key='restore-dataset')
    assert restored['deleted_at'] is None


def test_cross_account_and_cross_project_resource_operations_fail(setup):
    f = setup; model_id, dataset_id = model_and_dataset(f)
    other = identity.load_actor(2)
    other_project = permissions.create_project(other, '其他人的项目')
    for actor, project_id in [(other, f.project['id']), (other, other_project['id'])]:
        with pytest.raises(AgentError):
            lifecycle.preview(actor, project_id, 'dataset', dataset_id, 'delete')
    provider = ResourceTools(); spec = next(tool for tool in provider.list_tools(f.actor) if tool.name == QUERY)
    with pytest.raises(AgentError):
        provider.execute(f.actor, spec, {'project_id': other_project['id'], 'kind': 'dataset'}, request_key='cross', context=f.context)


def test_resource_query_reports_real_versions_and_explicit_trash(setup):
    f = setup; model_id, dataset_id = model_and_dataset(f)
    f.db.execute("UPDATE models SET status=1,publish_status='archived' WHERE id=?", (model_id,))
    preview = lifecycle.preview(f.actor, f.project['id'], 'model', model_id, 'delete', task_id=f.task['id'])
    lifecycle.change(f.actor, f.project['id'], 'model', model_id, 'delete', preview['revision'], request_key='delete')
    provider = ResourceTools(); spec = next(tool for tool in provider.list_tools(f.actor) if tool.name == QUERY)
    arguments = {'project_id': f.project['id'], 'kind': 'model'}
    assert provider.execute(f.actor, spec, arguments, request_key='read', context=f.context).outputs['items'] == []
    trash = provider.execute(f.actor, spec, {**arguments, 'include_deleted': True}, request_key='trash', context=f.context).outputs['items']
    assert trash[0]['id'] == model_id and trash[0]['revision'] == preview['revision'] + 1


def test_agent_can_read_actual_draft_sample_ids_and_assign_explicit_split(setup):
    from services.dataset_draft_service import create_draft, add_draft_sample, mark_draft_ready
    from agent.tools.business import build_provider
    from agent.tools.grants import needs_confirmation
    f = setup
    draft = create_draft({'name': '对话划分测试', 'creator_id': f.actor.user_id, 'source_type': 'feedback'}, f.db)
    add_draft_sample(draft.id, {'id': 'sample-actual', 'sample_key': 'original-001', 'annotations': [], 'pairing_state': 'not_required'}, f.db)
    mark_draft_ready(draft.id, f.db)
    permissions.register_resource(f.actor, project_id=f.project['id'], kind='draft', display_name=draft.name,
                                  source_table='dataset_drafts', source_id=draft.id)
    provider = ResourceTools(); spec = next(tool for tool in provider.list_tools(f.actor) if tool.name == SAMPLES)
    data = provider.execute(f.actor, spec, {'project_id': f.project['id'], 'draft_id': draft.id}, request_key='samples', context=f.context).outputs
    assert data['samples'][0]['id'] == 'sample-actual' and data['samples'][0]['split'] == 'unassigned'
    assert 'rgb_object_key' not in data['samples'][0]
    bridge = build_provider(f.backend)
    split = next(tool for tool in bridge.list_tools(f.actor) if tool.name == 'business_draft_assign_splits')
    assert needs_confirmation(f.actor, split, project_id=f.project['id'])
    arguments = {'draft_id': draft.id, 'sample_ids': ['sample-actual'], 'split': 'val'}
    prepared, action = card(f, bridge, split, arguments)
    assert f.db.query('SELECT split FROM dataset_draft_samples WHERE id=?', ('sample-actual',), fetchone=True)['split'] == 'unassigned'
    result = execute(f, bridge, split, arguments, prepared, action)
    assert result.outputs['updated'] == 1
    assert f.db.query('SELECT split FROM dataset_draft_samples WHERE id=?', ('sample-actual',), fetchone=True)['split'] == 'val'


def test_manual_lifecycle_preview_uses_current_project_header(setup):
    from agent.routes import agent_bp
    from utils.jwt_util import generate_token
    f = setup
    _, dataset_id = model_and_dataset(f)
    f.app.register_blueprint(agent_bp)
    client = f.app.test_client()
    headers = {'Authorization': 'Bearer ' + generate_token(f.actor.user_id)}
    path = '/api/agent/resources/lifecycle/dataset/' + dataset_id + '?operation=delete'
    assert client.get(path, headers=headers).status_code == 404
    response = client.get(path, headers={**headers, 'X-Project-Id': f.project['id']})
    assert response.status_code == 200
    preview = response.get_json()['data']
    assert preview['id'] == dataset_id and preview['project_id'] == f.project['id']
    assert any('模型' in reason for reason in preview['blocked_reasons'])

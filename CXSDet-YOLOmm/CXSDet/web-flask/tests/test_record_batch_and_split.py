from pathlib import Path
import pytest

from agent import resources
from agent.errors import AgentError
from agent.resource_tools import ResourceTools, DELETE_RECORDS
from services.record_lifecycle import set_deleted_many, delete_preview
from services.review_service import ReviewConflictError
from tests.test_inspection_loop import setup, start
from tests.test_review_agent_tools import card, execute


def test_batch_delete_is_atomic_and_only_changes_selected_records(setup):
    f = setup; run = start(f)
    ids = [row['id'] for row in f.db.query('SELECT id FROM detection_records ORDER BY id')]
    entries = [{'record_id': key, 'expected_version': 0} for key in ids[:2]]
    f.db.execute('UPDATE detection_records SET review_version=1 WHERE id=?', (ids[1],))
    with pytest.raises(ReviewConflictError): set_deleted_many(f.db, entries, f.actor.user_id)
    assert f.db.query("SELECT COUNT(*) AS n FROM detection_records WHERE record_status='deleted'", fetchone=True)['n'] == 0
    entries[1]['expected_version'] = 1
    provider = ResourceTools(); spec = next(item for item in provider.list_tools(f.actor) if item.name == DELETE_RECORDS)
    args = {'project_id': f.project['id'], 'records': entries}
    prepared, action = card(f, provider, spec, args)
    with pytest.raises(AgentError):
        provider.execute(f.actor, spec, args, request_key=action['request_key'], prepared_id=prepared['prepared_id'], context=f.context)
    result = execute(f, provider, spec, args, prepared, action)
    assert result.outputs['count'] == 2 and result.outputs['files_preserved']
    assert f.db.query('SELECT record_status FROM detection_records WHERE id=?', (ids[2],), fetchone=True)['record_status'] != 'deleted'
    assert f.db.query('SELECT COUNT(*) AS n FROM detection_records', fetchone=True)['n'] == 3
    assert f.source.is_dir()


def test_batch_delete_rejects_foreign_project_and_foreign_account(setup):
    from agent import identity, permissions
    f = setup; start(f)
    key = f.db.query('SELECT id FROM detection_records LIMIT 1', fetchone=True)['id']
    other = identity.load_actor(2); project = permissions.create_project(other, '其他项目')
    entries = [{'record_id': key, 'expected_version': 0}]
    with pytest.raises((AgentError, ValueError)): delete_preview(other, project['id'], entries)


def test_complete_dataset_split_creates_new_version_in_workspace_without_training(setup):
    f = setup; run = start(f)
    source_id = f.db.query('SELECT dataset_id FROM models WHERE id=?', (run['state']['model_id'],), fetchone=True)['dataset_id']
    original = f.db.query('SELECT zip_object_key FROM datasets WHERE id=?', (source_id,), fetchone=True)['zip_object_key']
    original_models = f.db.query('SELECT COUNT(*) AS n FROM models', fetchone=True)['n']
    args = {'dataset_id': source_id, 'name': '三份独立划分', 'split_policy': {'train': .34, 'val': .33, 'test': .33}}
    prepared = f.backend.prepare_tool(f.actor.user_id, 'dataset.split', args)
    assert not prepared['preview']['blocked']
    assert prepared['preview']['counts'] == {'train': 1, 'val': 1, 'test': 1}
    result = f.backend.execute_tool(f.actor.user_id, prepared['operation_id'], 'test-split-once')
    assert result['status'] == 'succeeded', result.get('error')
    outputs = result['outputs']
    assert outputs['dataset_id'] != source_id and outputs['total_samples'] == 3
    assert Path(outputs['output_dir']).is_relative_to(f.root)
    for split in ('train', 'val', 'test'):
        assert len(list((Path(outputs['output_dir']) / 'images' / split).glob('*.png'))) == 1
        assert len(list((Path(outputs['output_dir']) / 'images_ir' / split).glob('*.png'))) == 1
        assert len(list((Path(outputs['output_dir']) / 'labels' / split).glob('*.txt'))) == 1
    assert f.db.query('SELECT zip_object_key FROM datasets WHERE id=?', (source_id,), fetchone=True)['zip_object_key'] == original
    replay = f.backend.execute_tool(f.actor.user_id, prepared['operation_id'], 'test-split-once')
    assert replay['outputs']['dataset_id'] == outputs['dataset_id']
    assert f.db.query('SELECT COUNT(*) AS n FROM models', fetchone=True)['n'] == original_models


def test_dataset_split_rejects_bad_ratios_and_previews_empty_validation_split(setup):
    f = setup; run = start(f)
    source_id = f.db.query('SELECT dataset_id FROM models WHERE id=?', (run['state']['model_id'],), fetchone=True)['dataset_id']
    args = {'dataset_id': source_id, 'name': '无效比例', 'split_policy': {'train': .8, 'val': .3}}
    with pytest.raises(ValueError): f.backend.prepare_tool(f.actor.user_id, 'dataset.split', args)
    prepared = f.backend.prepare_tool(f.actor.user_id, 'dataset.split', {**args, 'name': '不足验证样本', 'split_policy': {'train': .8, 'val': .1, 'test': .1}})
    assert prepared['preview']['blocked']
    assert any(item['code'] == 'EMPTY_SPLIT' for item in prepared['preview']['conflicts'])

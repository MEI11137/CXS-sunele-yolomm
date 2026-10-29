"""Real archive writes in isolated project folders; no model/API/training calls."""
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from tests.test_inspection_loop import setup, start


def feedback(f):
    run = start(f)
    dataset_id = run['state']['dataset_id']
    record = f.db.query('SELECT * FROM detection_records ORDER BY id LIMIT 1', fetchone=True)
    f.db.execute("UPDATE detection_records SET human_review_status='confirmed',corrected_annotations='[]' WHERE id=?", (record['id'],))
    p = f.backend.prepare_tool(f.actor.user_id, 'feedback.add_confirmed', {'record_id': record['id'], 'dataset_id': dataset_id})
    result = f.backend.execute_tool(f.actor.user_id, p['operation_id'], 'pool-record')
    assert result['status'] == 'succeeded', result
    return dataset_id, record


def args(dataset_id, name='原数据集-修订'):
    return {'dataset_id': dataset_id, 'name': name, 'new_sample_split': 'train'}


def test_revision_replaces_real_labels_preserves_source_split_and_replays_once(setup):
    f = setup; dataset_id, record = feedback(f)
    source = f.backend._dataset_row(dataset_id)
    model_count = f.db.query('SELECT COUNT(*) AS n FROM models', fetchone=True)['n']
    source_hash = hashlib.sha256(f.backend.storage_client.get(source['zip_bucket'], source['zip_object_key'])).hexdigest()
    p = f.backend.prepare_tool(f.actor.user_id, 'dataset.feedback_revision', args(dataset_id))
    preview = p['preview']
    assert not preview['blocked'], preview
    assert preview['sample_count'] == 3
    assert preview['source']['revision']['replaced_samples'] == 1
    assert preview['source']['revision']['added_samples'] == 0
    result = f.backend.execute_tool(f.actor.user_id, p['operation_id'], 'revision-once')
    assert result['status'] == 'succeeded', result
    output = Path(result['outputs']['output_dir'])
    assert output.is_relative_to(f.root)
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    decision = manifest['feedback_samples'][0]
    entry = next(item for item in manifest['entries'] if item['key'] in decision['sample_keys'])
    assert (output / entry['label']).read_text() == ''  # confirmed no-defect replaces old box
    assert (output / entry['secondary']).is_file()
    assert entry['split'] == entry['key'].split('/')[0]
    assert manifest['source']['dataset_id'] == dataset_id
    assert hashlib.sha256(f.backend.storage_client.get(source['zip_bucket'], source['zip_object_key'])).hexdigest() == source_hash
    replay = f.backend.execute_tool(f.actor.user_id, p['operation_id'], 'revision-once')
    assert replay['outputs']['dataset_id'] == result['outputs']['dataset_id']
    assert f.db.query('SELECT COUNT(*) AS n FROM models', fetchone=True)['n'] == model_count
    # Mirror the real Agent bridge success hook (manual REST uses register_new).
    from agent import resources
    stats = resources.register_refs(f.actor.user_id, result['business_refs'], project_id=f.project['id'])
    assert stats['failed'] == 0
    f.backend.resolve(f.actor.user_id, 'dataset', result['outputs']['dataset_id'], ('dataset:read',))


def test_revision_appends_new_pair_to_explicit_split(setup):
    f = setup; dataset_id, record = feedback(f)
    image = f.root / 'new.png'; Image.new('RGB', (40,40), (200,200,200)).save(image)
    stored = f.backend.storage_client.upload('test-images', str(image))
    f.db.execute('UPDATE detection_records SET original_image_bucket=?,original_image_object_key=?,input_identity_json=? WHERE id=?', (stored['bucket'], stored['objectKey'], json.dumps({'rgb':{'original_name':'new.png'}}), record['id']))
    p = f.backend.prepare_tool(f.actor.user_id, 'dataset.feedback_revision', {**args(dataset_id), 'new_sample_split': 'test'})
    assert p['preview']['source']['revision']['added_samples'] == 1
    assert p['preview']['sample_count'] == 4
    result = f.backend.execute_tool(f.actor.user_id, p['operation_id'], 'revision-new-photo')
    assert result['status'] == 'succeeded', result
    root = Path(result['outputs']['output_dir'])
    assert list((root/'images/test').glob('feedback_*.png'))
    assert list((root/'images_ir/test').glob('feedback_*.png'))
    assert list((root/'labels/test').glob('feedback_*.txt'))


@pytest.mark.parametrize('change', ['version', 'rejected', 'deleted', 'label'])
def test_revision_refuses_changed_feedback_after_preview(setup, change):
    f = setup; dataset_id, record = feedback(f)
    p = f.backend.prepare_tool(f.actor.user_id, 'dataset.feedback_revision', args(dataset_id))
    dataset_count = f.db.query('SELECT COUNT(*) AS n FROM datasets', fetchone=True)['n']
    sql = {'version': 'review_version=review_version+1', 'rejected': "human_review_status='rejected'",
           'deleted': "record_status='deleted'", 'label': "corrected_annotations='[{\"class_id\":0,\"class_name\":\"热斑\",\"bbox\":[0.1,0.1,0.3,0.3]}]'"}[change]
    f.db.execute('UPDATE detection_records SET '+sql+' WHERE id=?', (record['id'],))
    with pytest.raises(ValueError):
        f.backend.execute_tool(f.actor.user_id, p['operation_id'], 'revision-changed')
    assert f.db.query('SELECT COUNT(*) AS n FROM datasets', fetchone=True)['n'] == dataset_count


def test_revision_no_confirmed_feedback_and_invalid_split_refused(setup):
    f = setup; run = start(f); dataset_id = run['state']['dataset_id']
    with pytest.raises(ValueError): f.backend.prepare_tool(f.actor.user_id, 'dataset.feedback_revision', args(dataset_id))
    with pytest.raises(ValueError): f.backend.prepare_tool(f.actor.user_id, 'dataset.feedback_revision', {**args(dataset_id), 'new_sample_split': 'anywhere'})


def test_manual_revision_route_registers_real_project_and_replays_once(setup):
    from routes.dataset import dataset_bp
    from utils.jwt_util import generate_token
    f = setup; dataset_id, _ = feedback(f)
    f.app.register_blueprint(dataset_bp, url_prefix='/api/dataset')
    client = f.app.test_client()
    headers = {'Authorization': 'Bearer '+generate_token(f.actor.user_id), 'X-Project-Id': f.project['id']}
    path = '/api/dataset/'+dataset_id+'/feedback-revision'
    result = client.post(path+'/preview', headers=headers, json=args(dataset_id)).get_json()
    assert result['code'] == 200, result
    operation = result['data']['operation_id']
    results = [client.post(path, headers=headers, json={'operation_id': operation}).get_json() for _ in range(2)]
    assert all(item['code'] == 200 for item in results), results
    new_id = results[0]['data']['outputs']['dataset_id']
    assert results[1]['data']['outputs']['dataset_id'] == new_id
    resource = f.backend.resolve(f.actor.user_id, 'dataset', new_id, ('dataset:read',))
    assert resource.project_id == f.project['id']
    rejected = client.post(path+'/preview', headers={'Authorization': headers['Authorization']}, json=args(dataset_id)).get_json()
    assert rejected['code'] != 200


def test_embedded_agent_can_execute_both_feedback_destinations(setup):
    from agent.business_bridge import BusinessBackendBridge
    from agent.tools.grants import needs_confirmation
    from tests.test_review_agent_tools import card, execute
    f = setup; dataset_id, _ = feedback(f)
    provider = BusinessBackendBridge(f.backend)
    specs = {spec.name: spec for spec in provider.list_tools(f.actor)}
    for tool, parameters in [('business_dataset_feedback_revision', args(dataset_id)),
                             ('business_draft_create_from_feedback', {'dataset_id': dataset_id})]:
        spec = specs[tool]
        assert needs_confirmation(f.actor, spec, project_id=f.project['id'])
        prepared, action = card(f, provider, spec, parameters)
        result = execute(f, provider, spec, parameters, prepared, action)
        assert result.status == 'succeeded', result
        if tool == 'business_dataset_feedback_revision':
            assert result.outputs['total_samples'] == 3
        else:
            assert result.outputs['sample_count'] == 1
            assert result.outputs['workflow_status'] == 'draft'

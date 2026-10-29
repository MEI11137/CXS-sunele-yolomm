"""核对网站内部 OpenCode 实际注册的业务工具，不调用模型或真实用户数据。"""
import pytest

from agent.errors import AgentError
from agent.resource_tools import ResourceTools, FEEDBACK_RECORDS
from tests.test_inspection_loop import setup, start
from tests.test_review_agent_tools import card, execute


def test_internal_opencode_catalogue_covers_project_business_pages(setup):
    from agent.opencode_runtime import OpenCodeRuntime
    f = setup
    runtime = OpenCodeRuntime(f.app)
    names = {spec.name for spec in runtime.registry.specs(f.actor, f.project['id'])}
    required = {
        'workspace_open_page', 'workspace_list_resources', 'workspace_query_resources',
        'workspace_attach_file', 'workspace_discover_inputs', 'workspace_start_inspection_loop',
        'workspace_route_reviewed_samples', 'workspace_inspection_state',
        'workspace_manage_resource', 'workspace_delete_records', 'workspace_add_feedback',
        'workspace_draft_samples', 'workspace_review_state', 'workspace_review_view', 'workspace_review_record',
        'business_dataset_import', 'business_dataset_transform', 'business_dataset_split', 'business_dataset_feedback_revision', 'business_dataset_inspect_samples',
        'business_model_import_weight', 'business_model_publish', 'business_model_unpublish', 'business_model_compare',
        'business_detection_run', 'business_detection_get_state', 'business_review_ai',
        'business_feedback_add_confirmed', 'business_draft_create_from_feedback',
        'business_draft_assign_splits', 'business_draft_finalize', 'business_training_start', 'business_training_get_state',
        'business_directory_inspect', 'business_directory_search', 'business_directory_transform',
        'business_document_parse', 'business_document_search',
        'workspace_projects','workspace_manage_project','workspace_sessions','workspace_manage_session',
        'workspace_switch_context','workspace_export_records_csv','workspace_documents',
        'workspace_manuals','workspace_manage_manual','workspace_ui',
    }
    assert not (required - names), sorted(required - names)
    assert 'business_review_submit_human' not in names
    assert 'business_mcp_save_connection' not in names


def test_batch_feedback_uses_one_real_card_and_same_source_service(setup):
    f = setup; start(f)
    rows = f.db.query('SELECT id FROM detection_records ORDER BY id')
    ids = [row['id'] for row in rows]
    model_count = f.db.query('SELECT COUNT(*) AS n FROM models', fetchone=True)['n']
    provider = ResourceTools()
    spec = next(tool for tool in provider.list_tools(f.actor) if tool.name == FEEDBACK_RECORDS)
    args = {'project_id': f.project['id'], 'records': [{'record_id': key, 'expected_version': 0} for key in ids[:2]]}
    with pytest.raises(AgentError):
        provider.prepare(f.actor, spec, args, f.context)
    for key in ids[:2]:
        f.db.execute("UPDATE detection_records SET human_review_status='confirmed',corrected_annotations='[]' WHERE id=?", (key,))
    prepared, action = card(f, provider, spec, args)
    assert prepared['preview']['count'] == 2
    assert all(item['dataset_id'] for item in prepared['preview']['records'])
    with pytest.raises(AgentError):
        provider.execute(f.actor, spec, args, request_key=action['request_key'], context=f.context, prepared_id=prepared['prepared_id'])
    assert f.db.query('SELECT COUNT(*) AS n FROM feedback_samples', fetchone=True)['n'] == 0
    result = execute(f, provider, spec, args, prepared, action)
    assert result.status == 'succeeded'
    assert result.outputs['succeeded'] == 2 and result.outputs['failed'] == 0
    assert f.db.query('SELECT COUNT(*) AS n FROM feedback_samples', fetchone=True)['n'] == 2
    assert f.db.query('SELECT feedback_dataset_id FROM detection_records WHERE id=?', (ids[2],), fetchone=True)['feedback_dataset_id'] is None
    assert f.db.query('SELECT COUNT(*) AS n FROM models', fetchone=True)['n'] == model_count

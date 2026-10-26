"""Integration coverage for management APIs, pinned manuals and real feedback files."""
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace
import pytest
from agent import permissions, manuals, tasks, sessions, recovery
from agent.identity import load_actor
from agent.tools.registry import get_registry
from agent.tools.contract import SOURCE_MCP
from agent.tools import grants
from services.file_access import FileAccess
from utils.jwt_util import generate_token


@pytest.fixture
def assembled():
    from test_integration_assembly import IntegrationAssemblyTest
    case = IntegrationAssemblyTest()
    case.setUp()
    try:
        actor = load_actor(2)
        project = permissions.create_project(actor, name='integration-private')
        yield SimpleNamespace(case=case, actor=actor, project=project, backend=case.adapter,
                              client=case.app.test_client(), headers={'Authorization': 'Bearer ' + generate_token(2)})
    finally:
        case.doCleanups()


def test_mcp_fixed_settings_individual_grants_staleness_and_no_generic_bypass(assembled):
    f = assembled
    advertised = [{'name': 'lookup', 'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}, 'annotations': {'readOnlyHint': True}}]
    calls = []
    f.backend.mcp_store.transport_factory = lambda _: SimpleNamespace(list_tools=lambda: advertised, call_tool=lambda name, args: calls.append((name, args)) or {'content': [{'type': 'text', 'text': 'real transport substitute'}]})
    response = f.client.post('/api/agent/mcp/connections', headers=f.headers, json={'name': 'private', 'transport': 'stdio', 'command': 'python', 'arguments': ['server.py'], 'credential': 'fixture-only-never-real-secret'})
    assert response.status_code == 200
    row = response.json['data']; connection_id = row['id']
    assert 'credential_ref' not in row and 'fixture-only-never-real-secret' not in response.get_data(as_text=True)
    assert f.client.post(f'/api/agent/mcp/connections/{connection_id}/discover', headers=f.headers).status_code == 200
    specs = get_registry().specs(f.actor, f.project['id'])
    assert not any(s.name.startswith('business_mcp') for s in specs)
    spec = next(s for s in specs if s.source == SOURCE_MCP)
    assert grants.needs_confirmation(f.actor, spec, project_id=f.project['id'])
    granted = f.client.post('/api/agent/grants', headers=f.headers, json={'server_id': connection_id, 'tool_name': spec.name, 'tool_version': spec.version, 'project_id': f.project['id']})
    assert granted.status_code == 200
    assert not grants.needs_confirmation(f.actor, spec, project_id=f.project['id'])
    assert not calls
    provider = get_registry().resolve(spec.name, f.actor)[1]
    result = provider.execute(f.actor, spec, {'query': 'sample'}, request_key='fixture-mcp-call', context=SimpleNamespace(project_id=f.project['id']))
    assert result.status == 'succeeded' and len(calls) == 1
    advertised[0]['annotations']['readOnlyHint'] = False
    f.backend.mcp_store.discover(2, connection_id)
    changed = next(s for s in get_registry().specs(f.actor) if s.source == SOURCE_MCP)
    assert changed.version != spec.version and grants.needs_confirmation(f.actor, changed, project_id=f.project['id'])
    from agent.errors import AgentError
    with pytest.raises(AgentError): provider.execute(f.actor, spec, {'query':'stale'}, request_key='must-not-run', context=SimpleNamespace(project_id=f.project['id']))
    assert len(calls) == 1
    refused = f.client.post('/api/agent/grants', headers=f.headers, json={'server_id': connection_id, 'tool_name': changed.name, 'tool_version': changed.version})
    assert refused.status_code == 400


def test_documents_upload_parse_dedupe_search_and_other_account_denied(assembled):
    from test_business_tool_assets import _docx
    f = assembled
    source = _docx(f.case.tmp/'inspection.docx')
    stored = f.backend.storage_client.upload('project_uploads', str(source))
    access = FileAccess(f.case.db_path, project_access=f.case.policy.project_file_access)
    asset_id = access.register_upload(2, stored['bucket'], stored['objectKey'], 'document', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', source.stat().st_size, f.project['id'], original_name=source.name, status='uploaded')
    body = {'asset_id': asset_id, 'title': '巡检规范'}
    result = f.client.post('/api/agent/documents/parse', headers=f.headers, json=body)
    assert result.status_code == 200, result.json
    document_id = result.json['data']['outputs']['document_id']
    again = f.client.post('/api/agent/documents/parse', headers=f.headers, json=body)
    assert again.status_code == 200 and again.json['data']['outputs']['document_id'] == document_id
    listing = f.client.get('/api/agent/documents', headers=f.headers, query_string={'project_id': f.project['id']})
    assert len(listing.json['data']) == 1
    hit = f.client.get(f'/api/agent/documents/{document_id}/search?q=每日', headers=f.headers)
    assert hit.status_code == 200 and hit.json['data']['hits'][0]['paragraph_no'] == 2
    assert hit.json['data']['hits'][0]['document_version']
    with sqlite3.connect(f.case.db_path) as conn:
        conn.execute("INSERT INTO user(id,username,password,role,status) VALUES(3,'other','x',0,1)")
    stranger = {'Authorization': 'Bearer ' + generate_token(3)}
    assert f.client.get(f'/api/agent/documents/{document_id}/search?q=每日', headers=stranger).status_code in (403,404)
    assert f.client.post('/api/agent/documents/parse', headers=stranger, json=body).status_code in (403,404)


def test_manual_uses_its_pinned_version_and_directory_revocation_is_real(assembled):
    f = assembled
    manual = manuals.create_user_manual(f.actor, name='核查', content_md='第一版规则')
    manuals.create_user_manual(f.actor, name='核查', content_md='第二版规则', manual_id=manual['id'])
    assert manuals.get_manual(f.actor, manual['id'], version=1)['content_md'] == '第一版规则'
    assert manuals.get_manual(f.actor, manual['id'])['version'] == 2
    root = f.case.tmp/'dataset-source'; root.mkdir()
    response = f.client.post('/api/agent/directories', headers=f.headers, json={'project_id': f.project['id'], 'root': str(root), 'label': 'sample'})
    assert response.status_code == 200, response.json
    directory_id = response.json['data']['id']
    assert permissions.resolve_resource(f.actor, {'kind':'directory','id':directory_id})['project_id'] == f.project['id']
    assert f.client.delete('/api/agent/directories/'+directory_id, headers=f.headers).status_code == 200
    with pytest.raises(ValueError): f.backend.directory_store.get(directory_id)


def test_feedback_merge_copies_confirmed_images_and_labels_into_real_candidate(tmp_path):
    from test_business_tool_assets import Fixture
    from services.feedback_dataset_service import _canonical_annotations
    f = Fixture(tmp_path)
    dataset_id = f.dataset_with_archive()
    record_id, sample_id = str(uuid.uuid4()), str(uuid.uuid4())
    annotations = [{'class_name': 'crack', 'class_id': 0, 'bbox': [.1,.1,.3,.3]}]
    canonical, _ = _canonical_annotations(json.dumps(annotations))
    image = f.put('detection_images', 'feedback.jpg', b'unique-confirmed-feedback-image')
    f.sql("INSERT INTO detection_records(id,user_id,model_id,model_name,create_time,title,corrected_annotations,human_review_status,original_image_bucket,original_image_object_key) VALUES(?,7,'fixture-model','fixture','2026-10-26','feedback',?,'confirmed',?,?)", (record_id,canonical,image['bucket'],image['objectKey']))
    f.sql("INSERT INTO feedback_samples(id,record_id,dataset_id,annotation_hash,export_status,create_time) VALUES(?,?,?,?,'pending','2026-10-26')", (sample_id,record_id,dataset_id,hashlib.sha256(canonical.encode()).hexdigest()))
    adapter = f.adapter()
    args = {'dataset_id': dataset_id, 'name':'merged-confirmed', 'include_feedback':True, 'split_policy':{'train': .67,'val':.33}}
    prepared = adapter.prepare_tool(7, 'dataset.transform', args)
    assert not prepared['preview']['blocked'], prepared
    result = adapter.execute_tool(7, prepared['operation_id'], 'real-feedback-merge')
    assert result['status'] == 'succeeded', result
    manifest = json.loads((Path(result['outputs']['output_dir'])/'manifest.json').read_text(encoding='utf-8'))
    assert manifest['sample_count'] == 3 and len(manifest['feedback_samples']) == 1
    entry = next(x for x in manifest['entries'] if 'feedback_' in x['key'])
    assert (Path(result['outputs']['output_dir'])/entry['image']).read_bytes() == b'unique-confirmed-feedback-image'
    assert (Path(result['outputs']['output_dir'])/entry['label']).read_text().startswith('0 ')
    assert not list(f.work.glob('transform-src-*'))


def test_human_entry_waits_for_real_submission_and_notifier_preserves_handoff(assembled):
    from test_agent_backend import ScriptedGateway, tool_turn
    from agent.runtime import AgentRuntime
    f = assembled
    record_id = str(uuid.uuid4())
    with sqlite3.connect(f.case.db_path) as conn:
        conn.execute("INSERT INTO detection_records(id,user_id,model_id,model_name,create_time,original_image_bucket,original_image_object_key,human_review_status) VALUES(?,2,'fixture','fixture','2026-10-26','images','fixture.jpg','pending')", (record_id,))
    permissions.register_resource(f.actor, project_id=f.project['id'], kind='detection_record', display_name='review fixture', source_table='detection_records', source_id=record_id)
    session = sessions.create_session(f.actor, f.project['id'])
    task = tasks.create_task(f.actor, session['id'], f.project['id'], '人工核查')
    tasks.set_plan(task['id'], goal='人工核查', steps=[{'title':'确认标注', 'tool':'wait_for_human', 'needs_human':True}])
    current = tasks.get_task_by_id(task['id'])
    task = tasks.confirm_plan(task['id'], 2, expected_version=current['plan_version'])
    with pytest.raises(Exception): tasks.confirm_plan(task['id'], 2, expected_version=current['plan_version'])
    gateway = ScriptedGateway(tool_turn('wait_for_human', kind='detection_record', id=record_id, reason='请检查标注', step_id=task['steps'][0]['id']))
    result = AgentRuntime(gateway_factory=lambda _: gateway)._run_loop(f.actor, session, f.project, task)
    assert result['status'] == 'awaiting_human'
    assert recovery.reconcile_task(f.actor, task['id'], auto_continue=False)['changed'] is False
    with sqlite3.connect(f.case.db_path) as conn:
        conn.execute("UPDATE detection_records SET human_review_status='confirmed',review_version=1 WHERE id=?", (record_id,))
    notifier = f.case.app.extensions['haixun_agent']['notifier']
    notifier._reconcile(2, task['id'])
    assert tasks.get_task_by_id(task['id'])['status'] == 'awaiting_human'
    ready = recovery.reconcile_task(f.actor, task['id'], auto_continue=False)
    assert ready['changed'] and ready['task']['steps'][0]['status'] == 'done'


def test_stop_during_model_response_does_not_execute_the_returned_tool(assembled):
    from test_agent_backend import tool_turn
    from agent.runtime import AgentRuntime
    f = assembled
    session = sessions.create_session(f.actor, f.project['id'])
    task = tasks.create_task(f.actor, session['id'], f.project['id'], 'stop fixture')
    tasks.set_status(task['id'], tasks.STATUS_RUNNING, force=True)
    def complete(*args):
        tasks.set_status(task['id'], tasks.STATUS_STOPPED, force=True)
        return tool_turn('business_model_publish', model_id='does-not-matter')
    result = AgentRuntime(gateway_factory=lambda _: SimpleNamespace(complete=complete))._run_loop(f.actor, session, f.project, tasks.get_task_by_id(task['id']))
    assert result['status'] == 'stopped'
    from agent.operations import list_for_task
    assert list_for_task(task['id']) == []


def test_goal_change_invalidates_old_model_reply_and_plan_confirmation(assembled):
    from test_agent_backend import tool_turn
    from agent.runtime import AgentRuntime
    from agent.errors import AgentError
    f = assembled
    session = sessions.create_session(f.actor, f.project['id'])
    task = tasks.create_task(f.actor, session['id'], f.project['id'], 'old goal')
    tasks.set_plan(task['id'], goal='old goal', steps=[{'title':'检查', 'tool':'business_dataset_get_state'}])
    old = tasks.get_task_by_id(task['id'])
    runtime = AgentRuntime()
    def complete(*args):
        runtime._apply_change(old)
        return tool_turn('business_model_publish', model_id='does-not-matter')
    runtime._gateway_factory = lambda _: SimpleNamespace(complete=complete)
    result = runtime._run_loop(f.actor, session, f.project, old)
    assert result['status'] == 'planning'
    with pytest.raises(AgentError): tasks.confirm_plan(old['id'], 2, expected_version=old['plan_version'])
    from agent.operations import list_for_task
    assert list_for_task(old['id']) == []


def test_business_project_ref_wins_over_chat_project_for_new_model(assembled):
    from agent import resources
    f = assembled
    other = permissions.create_project(f.actor, name='chat-in-another-project')
    model_id = str(uuid.uuid4())
    with sqlite3.connect(f.case.db_path) as conn:
        conn.execute("INSERT INTO models(id,name,dataset_id,base_model,create_time) VALUES(?,'child','fixture','fixture','2026-10-26')", (model_id,))
    result = resources.register_refs(2, [{'kind':'model','id':model_id,'project_id':f.project['id']}], project_id=other['id'])
    assert result['registered'] == 1
    assert permissions.resolve_resource(f.actor, {'kind':'model','id':model_id})['project_id'] == f.project['id']


def test_another_live_backend_cannot_mark_current_tasks_interrupted(assembled, monkeypatch):
    from agent import process_lease
    f = assembled
    with sqlite3.connect(f.case.db_path) as conn:
        conn.execute('UPDATE agent_process_lease SET pid=999999,started=123 WHERE id=1')
    original = process_lease.psutil.Process
    monkeypatch.setattr(process_lease.psutil, 'Process', lambda pid=None: SimpleNamespace(create_time=lambda:123, is_running=lambda:True) if pid == 999999 else original(pid))
    with pytest.raises(RuntimeError, match='已有后端'):
        process_lease.claim(f.case.db_path)


def test_pdf_citation_page_follows_page_tree_not_object_numbers():
    from io import BytesIO
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    from services.document_service import parse_pdf
    writer = PdfWriter()
    for text in ('object-one', 'object-two'):
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject('/Type'):NameObject('/Font'), NameObject('/Subtype'):NameObject('/Type1'), NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream = DecodedStreamObject(); stream.set_data(f'BT /F1 12 Tf 72 720 Td ({text}) Tj ET'.encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    writer._pages.get_object()['/Kids'].reverse()
    buffer = BytesIO(); writer.write(buffer)
    parsed = parse_pdf(buffer.getvalue())
    assert parsed['blocks'][0]['page_no'] == 1 and 'object-two' in parsed['blocks'][0]['text']


def test_source_changes_after_transform_preview_cannot_write_candidate(tmp_path):
    import zipfile
    from test_business_tool_assets import Fixture
    f = Fixture(tmp_path)
    dataset_id = f.dataset_with_archive()
    adapter = f.adapter()
    prepared = adapter.prepare_tool(7, 'dataset.transform', {'dataset_id':dataset_id,'name':'candidate-stale'})
    row = adapter._dataset_row(dataset_id)
    archive = f.storage._path(row['zip_bucket'],row['zip_object_key'])
    with zipfile.ZipFile(archive) as source:
        files = {name:source.read(name) for name in source.namelist()}
    files['images/val/b.jpg'] = b'changed-source-content'
    with zipfile.ZipFile(archive,'w') as output:
        for name, data in files.items(): output.writestr(name,data)
    from services.tool_common import ToolRefused
    with pytest.raises(ToolRefused) as refused:
        adapter.execute_tool(7, prepared['operation_id'], 'stale-source')
    assert refused.value.code == 'TRANSFORM_INPUT_CHANGED'
    assert f.sql("SELECT status FROM business_tool_operations WHERE request_key='stale-source'")[0][0] == 'failed'
    assert f.sql('SELECT COUNT(*) FROM dataset_candidates')[0][0] == 0


def test_training_recovery_preserves_live_worker_and_marks_orphans(assembled, monkeypatch):
    import os
    from services import training_runner
    f = assembled
    monkeypatch.setattr(os, 'kill', lambda *args: (_ for _ in ()).throw(AssertionError('process inspection must not send signals')))
    assert training_runner._process_alive(os.getpid())
    with sqlite3.connect(f.case.db_path) as conn:
        for mid in ('live-worker', 'orphan-worker'):
            conn.execute("INSERT INTO models(id,name,dataset_id,base_model,create_time,status) VALUES(?,?,'fixture','fixture','2026-10-26',0)", (mid,mid))
    training_runner.acquire_training_lock(f.case.db_path, 'live-worker', os.getpid())
    assert training_runner.reconcile_training_jobs(f.case.db_path) == 1
    with sqlite3.connect(f.case.db_path) as conn:
        states = dict(conn.execute('SELECT id,status FROM models'))
    assert states['live-worker'] == 0 and states['orphan-worker'] == 4
    assert states['default-model-001'] == 3
    training_runner.release_training_lock(f.case.db_path, 'live-worker')
    assert training_runner.reconcile_training_jobs(f.case.db_path) == 1

import json
import os
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
from PIL import Image
import io
from unittest.mock import patch

from agent import resources
from services import training_runner as runner
from services.tool_common import ResourceSnapshot, ToolRefused
from services.tool_handlers import ModelCreate, TrainingCancel, DatasetExportZip, DetectionEnhanceImage, DetectionAnalyzeDefects, DetectionSuggest
from services.project_storage import project_scope
from services.business_tool_specs import TOOL_SPECS
from services.personal_review_client import PersonalReviewClient
from tests.test_inspection_loop import setup, start
from services.model_registry_service import start_incremental_training, ModelRegistryError


@pytest.fixture
def business(setup):
    f = setup
    start(f)
    f.dataset_id = f.db.query('SELECT id FROM datasets', fetchone=True)['id']
    f.model_id = f.db.query('SELECT id FROM models', fetchone=True)['id']
    f.record_id = f.db.query('SELECT id FROM detection_records', fetchone=True)['id']
    return f


def model_input(f, name='new-model'):
    args = {'dataset_id': f.dataset_id, 'name': name, 'epochs': 1, 'batch_size': 1}
    resource, preview = ModelCreate().prepare(f.backend, f.actor.user_id, args)
    return args, resource, preview


def test_model_busy_creates_no_row(business):
    f = business
    args, resource, preview = model_input(f)
    runner.acquire_training_lock(f.backend.database_path, 'other', os.getpid())
    before = f.db.query('SELECT COUNT(*) AS n FROM models', fetchone=True)['n']
    with pytest.raises(ToolRefused, match='正在训练'):
        ModelCreate().execute(f.backend, 1, args, preview, resource)
    assert f.db.query('SELECT COUNT(*) AS n FROM models', fetchone=True)['n'] == before
    assert runner.read_lock(f.backend.database_path)['owner'] == 'other'


def test_model_launch_failure_marks_failed_and_releases_slot(business):
    f = business
    args, resource, preview = model_input(f)
    def fail(**kwargs): raise RuntimeError('fixture launcher failed before starting')
    f.backend.training_launcher = fail
    with pytest.raises(ToolRefused, match='启动失败'):
        ModelCreate().execute(f.backend, 1, args, preview, resource)
    row = f.db.query('SELECT id,status FROM models WHERE name=?', ('new-model',), fetchone=True)
    assert row['status'] == 2
    assert runner.read_lock(f.backend.database_path) is None
    assert resources.ensure('models', row['id'])['project_id'] == f.project['id']


def test_model_launch_success_keeps_slot_and_project(business):
    f = business
    calls = []
    f.backend.training_launcher = lambda **kwargs: calls.append(kwargs)
    args, resource, preview = model_input(f)
    result, refs = ModelCreate().execute(f.backend, 1, args, preview, resource)
    assert calls[0]['base_model'] == preview['base_model']
    assert calls[0]['project_id'] == f.project['id']
    assert resources.ensure('models', result['model_id'])['owner_id'] == 1
    assert runner.read_lock(f.backend.database_path)['owner'] == result['model_id']


def cancel_fixture(f, monkeypatch, outcome):
    f.db.execute('UPDATE models SET status=0 WHERE id=?', (f.model_id,))
    runner.acquire_training_lock(f.backend.database_path, f.model_id, os.getpid())
    with runner._connect(f.backend.database_path) as conn:
        conn.execute('UPDATE training_lock SET holder_pid=424242,holder_started=1.0')
    live = {424242: True, 424243: True}
    class Process:
        def __init__(self, pid): self.pid = pid
        def create_time(self): return 1.0
        def children(self, recursive=True): return [Process(424243)] if self.pid == 424242 else []
        def terminate(self):
            if outcome == 'denied': raise psutil.AccessDenied(self.pid)
            if outcome == 'success': live[self.pid] = False
    monkeypatch.setattr(runner, '_process_alive', lambda pid, started=None: live.get(pid, False))
    monkeypatch.setattr(runner.psutil, 'Process', Process)
    monkeypatch.setattr(runner.psutil, 'wait_procs', lambda processes, timeout: ([], [p for p in processes if live[p.pid]]))


@pytest.mark.parametrize('outcome', ['denied', 'timeout'])
def test_cancel_failure_preserves_state_and_slot(business, monkeypatch, outcome):
    f = business
    cancel_fixture(f, monkeypatch, outcome)
    with pytest.raises(runner.TrainingCancelError):
        runner.cancel_training_job(f.backend.database_path, f.model_id, 'test', timeout=0)
    assert f.db.query('SELECT status FROM models WHERE id=?', (f.model_id,), fetchone=True)['status'] == 0
    assert runner.read_lock(f.backend.database_path)['owner'] == f.model_id
    with pytest.raises(runner.TrainingBusy): runner.acquire_training_lock(f.backend.database_path, 'next', 55)


def test_cancel_success_waits_for_parent_and_child(business, monkeypatch):
    f = business
    cancel_fixture(f, monkeypatch, 'success')
    result = runner.cancel_training_job(f.backend.database_path, f.model_id, 'test', timeout=0)
    assert result['canceled'] and result['process_terminated']
    assert f.db.query('SELECT status FROM models WHERE id=?', (f.model_id,), fetchone=True)['status'] == 4
    assert runner.read_lock(f.backend.database_path) is None


def test_cancel_cannot_kill_server_reservation(business):
    f = business
    f.db.execute('UPDATE models SET status=0 WHERE id=?', (f.model_id,))
    runner.acquire_training_lock(f.backend.database_path, f.model_id, os.getpid())
    with pytest.raises(runner.TrainingCancelError, match='网站服务'):
        runner.cancel_training_job(f.backend.database_path, f.model_id, 'test')
    assert runner.read_lock(f.backend.database_path)


def test_export_ignores_paths_in_display_name_and_never_overwrites(business, tmp_path):
    f = business
    f.db.execute('UPDATE datasets SET name=? WHERE id=?', (str(tmp_path/'outside'), f.dataset_id))
    tool = DatasetExportZip()
    args = {'dataset_id': f.dataset_id}
    with project_scope(f.backend.database_path, f.project['id']):
        resource, preview = tool.prepare(f.backend, 1, args)
        a, _ = tool.execute(f.backend, 1, args, preview, resource)
        b, _ = tool.execute(f.backend, 1, args, preview, resource)
    assert Path(a['path']).resolve().is_relative_to(f.root/'exports')
    assert a['path'] != b['path'] and Path(a['path']).read_bytes() == Path(b['path']).read_bytes()
    assert not (tmp_path/'outside.zip').exists()


def test_enhance_registers_project_asset_and_preserves_original(business):
    f = business
    tool = DetectionEnhanceImage()
    args = {'record_id': f.record_id}
    original = f.backend._read_record(f.record_id)
    before = f.backend.storage_client.get(original['original_image_bucket'], original['original_image_object_key'])
    with project_scope(f.backend.database_path, f.project['id']):
        resource, preview = tool.prepare(f.backend, 1, args)
        out, _ = tool.execute(f.backend, 1, args, preview, resource)
    asset = f.db.query('SELECT * FROM file_assets WHERE id=?', (out['asset_id'],), fetchone=True)
    assert asset['project_id'] == f.project['id'] and asset['owner_id'] == 1
    assert before == f.backend.storage_client.get(original['original_image_bucket'], original['original_image_object_key'])


def test_enhance_registration_failure_removes_uploaded_object(business, monkeypatch):
    f = business
    tool = DetectionEnhanceImage()
    args = {'record_id': f.record_id}
    removed = []
    actual = f.backend.storage_client.delete
    def delete(bucket, key): removed.append((bucket, key)); actual(bucket, key)
    monkeypatch.setattr(f.backend.storage_client, 'delete', delete)
    def deny(*args, **kwargs): raise PermissionError('fixture refuses registration')
    monkeypatch.setattr(f.backend.file_assets.access, 'register_upload', deny)
    with project_scope(f.backend.database_path, f.project['id']):
        resource, preview = tool.prepare(f.backend, 1, args)
        with pytest.raises(PermissionError): tool.execute(f.backend, 1, args, preview, resource)
    assert len(removed) == 1
    assert not f.backend.storage_client.exists(*removed[0])


@pytest.mark.parametrize('tool', [DetectionAnalyzeDefects(), DetectionSuggest()])
def test_visual_tools_use_personal_connection_and_both_images(business, tool):
    f = business
    calls = []
    class Gateway:
        credentials = SimpleNamespace(model='personal-test-model')
        def complete(self, messages, **kwargs):
            calls.append(messages)
            content = messages[0]['content']
            text = json.dumps({'status':'consistent', 'summary':'fixture review', 'possible_false_positives':[], 'possible_missed_detections':[]}) if isinstance(content, list) else 'fixture personal suggestion'
            return SimpleNamespace(text=text)
    gateway = Gateway()
    # Bootstrap returns an already adapted personal client, not a raw gateway.
    f.backend.model_client_factory = lambda: PersonalReviewClient(gateway)
    f.backend.review_client_factory = lambda: PersonalReviewClient(gateway)
    args = {'record_id': f.record_id}
    resource, preview = tool.prepare(f.backend, 1, args)
    real_get = f.backend.storage_client.get
    record = f.backend._read_record(f.record_id)
    secondary_key = record['secondary_image_object_key']
    buffered = io.BytesIO()
    Image.new('RGB', (40, 40), (0, 0, 255)).save(buffered, format='PNG')
    def get(bucket, key):
        return buffered.getvalue() if key == secondary_key else real_get(bucket, key)
    with patch.object(f.backend.storage_client, 'get', side_effect=get):
        out, _ = tool.execute(f.backend, 1, args, preview, resource)
    images = [x for x in calls[0][0]['content'] if x['type'] == 'image_url']
    assert len(images) == 2 and images[0] != images[1]
    assert out['model_calls'] == (2 if isinstance(tool, DetectionSuggest) else 1)
    assert f.backend._read_record(f.record_id)['human_review_status'] != 'confirmed'


def test_visual_tool_metadata_matches_paid_writes():
    for name in ['detection.analyze_defects', 'detection.suggest']:
        spec = TOOL_SPECS[name]
        assert spec.descriptor['effect'] == 'write' and spec.descriptor['billable'] is True
    assert TOOL_SPECS['dataset.export_zip'].descriptor['effect'] == 'write'


def test_worker_cannot_train_until_lock_handoff(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, 'run_training_job', lambda **payload: calls.append(payload))
    gate = SimpleNamespace(wait=lambda timeout: False)
    runner._run_after_handoff(gate, {'model_id':'not-handed-off'})
    assert calls == []
    gate.wait = lambda timeout: True
    runner._run_after_handoff(gate, {'model_id':'handed-off'})
    assert calls == [{'model_id':'handed-off'}]


def test_initial_busy_page_creates_no_training_row(business, monkeypatch):
    f = business
    import routes.model as route
    from utils.jwt_util import generate_token
    monkeypatch.setattr(route, 'db', f.db)
    f.app.register_blueprint(route.model_bp)
    runner.acquire_training_lock(f.backend.database_path, 'other', os.getpid())
    before = f.db.query('SELECT COUNT(*) n FROM models',fetchone=True)['n']
    response = f.app.test_client().post('/api/model/create',headers={'Authorization':'Bearer '+generate_token(1), 'X-Project-Id':f.project['id']},
        json={'name':'busy-page','dataset_id':f.dataset_id,'epochs':1,'batch_size':1})
    assert response.status_code == 409
    assert f.db.query('SELECT COUNT(*) n FROM models',fetchone=True)['n'] == before
    assert runner.read_lock(f.backend.database_path)['owner'] == 'other'


def test_incremental_page_rejects_other_users_dataset_before_launch(business, monkeypatch):
    f = business
    import routes.model as route
    from utils.jwt_util import generate_token
    monkeypatch.setattr(route, 'db', f.db)
    f.app.register_blueprint(route.model_bp)
    f.db.execute('UPDATE user SET role=0 WHERE id=1')
    f.db.execute("UPDATE agent_resource SET owner_id=2 WHERE source_table='datasets' AND source_id=?", (f.dataset_id,))
    calls = []
    monkeypatch.setattr(route,'launch_training_job', lambda p: calls.append(p))
    before = f.db.query('SELECT COUNT(*) n FROM models',fetchone=True)['n']
    response = f.app.test_client().post('/api/model/'+f.model_id+'/incremental',
        headers={'Authorization':'Bearer '+generate_token(1)},json={'feedback_dataset_id':f.dataset_id,'epochs':1,'batch_size':1})
    assert response.get_json()['code'] == 403
    assert not calls
    assert f.db.query('SELECT COUNT(*) n FROM models',fetchone=True)['n'] == before


def test_incremental_agent_registers_child_to_actual_project(business):
    from services.tool_handlers import TrainingStart
    f = business
    calls = []
    f.backend.training_launcher = lambda **kwargs: calls.append(kwargs)
    args = {'parent_model_id':f.model_id,'dataset_id':f.dataset_id,'epochs':1,'batch_size':1}
    resource, preview = TrainingStart().prepare(f.backend,1,args)
    result, _ = TrainingStart().execute(f.backend,1,args,preview,resource)
    registered = resources.ensure('models',result['model_id'])
    assert registered['owner_id'] == 1 and registered['project_id'] == f.project['id']
    assert calls[0]['project_id'] == f.project['id']


def test_incremental_refuses_unapproved_dataset(business):
    f = business
    f.db.execute('UPDATE datasets SET status=0 WHERE id=?',(f.dataset_id,))
    with pytest.raises(ModelRegistryError,match='定稿'):
        start_incremental_training(f.model_id,f.dataset_id,{},f.db)


def test_incremental_refuses_changed_class_order(business):
    f = business
    row = f.db.query('SELECT * FROM datasets WHERE id=?',(f.dataset_id,),fetchone=True)
    row['id'] = 'different-classes'
    row['name'] = 'different-classes'
    row['structure_info'] = json.dumps({'class_names':['different']})
    f.db.execute('INSERT INTO datasets ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
    with pytest.raises(ModelRegistryError,match='类别'):
        start_incremental_training(f.model_id,row['id'],{},f.db)


def test_verified_cancel_terminates_real_harmless_process_tree(business, tmp_path):
    import subprocess, sys, time
    f = business
    marker = tmp_path/'child.pid'
    child_code = 'import os,time,pathlib;pathlib.Path('+repr(str(marker))+').write_text(str(os.getpid()));time.sleep(60)'
    parent_code = 'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",'+repr(child_code)+']);time.sleep(60)'
    process = subprocess.Popen([sys.executable,'-c',parent_code])
    targets = []
    try:
        deadline = time.monotonic()+10
        while not marker.exists() and time.monotonic()<deadline: time.sleep(.05)
        assert marker.exists(), 'harmless process fixture did not start'
        targets = [(p.pid,p.create_time()) for p in [psutil.Process(process.pid),*psutil.Process(process.pid).children(recursive=True)]]
        f.db.execute('UPDATE models SET status=0 WHERE id=?',(f.model_id,))
        runner.acquire_training_lock(f.backend.database_path,f.model_id,process.pid)
        result = runner.cancel_training_job(f.backend.database_path,f.model_id,'harmless process verification')
        assert result['process_terminated']
        assert all(not runner._process_alive(pid,started) for pid,started in targets)
        assert runner.read_lock(f.backend.database_path) is None
    finally:
        for pid,started in targets:
            if runner._process_alive(pid,started): psutil.Process(pid).terminate()
        if process.poll() is None: process.terminate()
        process.wait(timeout=10)

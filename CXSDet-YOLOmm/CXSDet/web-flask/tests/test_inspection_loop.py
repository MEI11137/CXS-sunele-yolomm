"""真实临时库/文件/业务服务；仅替换 YOLO 推理及付费模型，不触碰业务数据。"""
import io
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, g
from PIL import Image
import pytest
import yaml

from agent import identity, permissions, sessions, store, tasks
from agent import inspection_loop as loop
from agent.bootstrap import configure_business_backend, reset_business_backend
from agent.errors import AgentError
from agent.llm.provider import _PROBE_PNG, classify_failure, REASON_PARSE_ERROR
from services.yolo_runtime import DetectionOutput
from utils.db import Database


@pytest.fixture
def setup(tmp_path, monkeypatch, request):
    database = tmp_path / 'site.sqlite3'
    db = Database(database)
    app = Flask(__name__)
    work = tmp_path / 'runtime'; work.mkdir()
    app.config.update(DATABASE=str(database), TEMP_DIR=str(work), FILE_STORE_PATH=str(tmp_path / 'store'), TESTING=True)
    with app.app_context():
        store.reset_schema_cache()
        actor = identity.load_actor(1)
        # 本机项目不能绑定 APPDATA；测试库可在系统临时目录，项目放工作区测试目录。
        base = Path(os.environ.get('OPENCODE_TEST_TMP') or Path(__file__).resolve().parents[4] / '.integration-checks')
        base.mkdir(parents=True, exist_ok=True)
        local_project = tempfile.TemporaryDirectory(prefix='loop-project-', dir=base)
        request.addfinalizer(local_project.cleanup)
        root = Path(local_project.name) / 'project'; root.mkdir()
        source = root / 'dataset'; source.mkdir()
        (source / 'data.yaml').write_text(yaml.safe_dump({'names': ['热斑'], 'nc': 1, 'train': 'images/train', 'val': 'images/val',
            'modalities': {'rgb': 'images', 'ir': 'images_ir'}, 'modality_used': ['rgb', 'ir']}), encoding='utf-8')
        for index, split in enumerate(['train', 'train', 'val']):
            for modality in ['images', 'images_ir']:
                directory = source / modality / split; directory.mkdir(parents=True, exist_ok=True)
                Image.new('RGB', (40, 40), (index * 40 + 30, 20, 0)).save(directory / f'{index}.png')
            directory = source / 'labels' / split; directory.mkdir(parents=True, exist_ok=True)
            (directory / f'{index}.txt').write_text('0 0.5 0.5 0.5 0.5\n')
        (root / 'best.pt').write_bytes(b'PK\x03\x04test-only')
        project = permissions.create_project(actor, '闭环测试', root_directory=str(root))
        chat = sessions.create_session(actor, project['id'])
        task = tasks.create_task(actor, chat['id'], project['id'], '3 张图片，置信度 0.25')
        calls = []
        class Review:
            def review_detection_structured(self, image_bytes, detections, **kwargs):
                calls.append(kwargs)
                return json.dumps({'status': 'needs_human_review', 'summary': '请检查',
                    'possible_false_positives': [], 'possible_missed_detections': [], 'attention_regions': []})
        configure_business_backend(app, overrides={
            'detector_loader': lambda *args: SimpleNamespace(model=SimpleNamespace(mm_router=object())),
            'review_client_factory': lambda: Review(),
            'training_launcher': lambda **kwargs: None,
        })
        backend = app.extensions['haixun_agent']['business_backend']
        backend.detector_predictor = lambda *args: DetectionOutput([{'class_id': 0, 'class_name': 'english_hotspot',
            'bbox': [10,10,30,30], 'confidence': .9}], [])
        monkeypatch.setattr('services.model_import_service.TEMP_DIR', str(work))
        monkeypatch.setattr(loop.connections, 'require_capability', lambda *args: None)
        monkeypatch.setattr(loop.connections, 'capabilities', lambda *args: {'vision': {'status': 'ok'}})
        monkeypatch.setattr(loop.connections, 'get_connection_view', lambda *args: {'provider': 'fixture', 'model': 'fixture', 'key_fingerprint': 'fixture'})
        provider = loop.InspectionLoopTools()
        specs = {item.name: item for item in provider.list_tools(actor)}
        args = {'project_id': project['id'], 'dataset_path': 'dataset', 'weight_path': 'best.pt', 'confidence': .25, 'sample_limit': 3}
        context = {'project_id': project['id'], 'task_id': task['id'], 'authorization': {'confirmed': True}}
        g.user_id = actor.user_id
        yield SimpleNamespace(**locals())
        reset_business_backend(app)


def start(f):
    prepared = f.provider.prepare(f.actor, f.specs[loop.START], f.args, f.context)
    result = f.provider.execute(f.actor, f.specs[loop.START], f.args, request_key='test-start',
        context=f.context, prepared_id=prepared['prepared_id'])
    assert result.status == 'running'
    run = loop.get_run(f.actor, prepared['prepared_id'])
    loop._detect(run)
    return loop.get_run(f.actor, run['id'])


def test_full_flow_real_business_services_and_single_plan(setup):
    f = setup
    discovery = f.provider.execute(f.actor, f.specs['workspace_discover_inputs'], {'project_id': f.project['id']}, request_key='discover', context=f.context).outputs
    assert discovery['dataset_summaries'][0]['sample_count'] == 3
    assert discovery['dataset_summaries'][0]['modalities'] == ['ir', 'rgb']
    # 错误路径、跨项目、未批准均不能写入。
    with pytest.raises(AgentError):
        f.provider.prepare(f.actor, f.specs[loop.START], {**f.args, 'dataset_path': '..'}, f.context)
    prepared = f.provider.prepare(f.actor, f.specs[loop.START], f.args, f.context)
    with pytest.raises(AgentError):
        f.provider.execute(f.actor, f.specs[loop.START], f.args, request_key='unapproved',
            context={**f.context, 'authorization': {}}, prepared_id=prepared['prepared_id'])
    run = start(f)
    assert run['status'] == 'awaiting_human'
    assert len(run['state']['items']) == len(f.calls) == 3
    assert all(call['secondary_image_bytes'] for call in f.calls)
    assert all(call['class_names'] == ['热斑'] for call in f.calls)
    records = loop._review_queue(run)
    assert all(f.backend._read_record(key)['human_review_status'] != 'confirmed' for key in records)
    assert all(json.loads(f.backend._read_record(key)['detection_result'])[0]['class_name'] == '热斑' for key in records)
    assert tasks.get_task(f.actor, f.task['id'])['status'] == 'awaiting_human'
    assert tasks.get_task(f.actor, f.task['id'])['waiting_for']['page_hint'].endswith('flow_id=' + run['id'])
    reused = f.provider.prepare(f.actor, f.specs[loop.START], f.args, f.context)['preview']['reuse']
    assert reused['model_id'] == run['state']['model_id']
    registered_args = {key: value for key, value in f.args.items() if key != 'weight_path'}
    registered_args['model_id'] = run['state']['model_id']
    assert f.provider.prepare(f.actor, f.specs[loop.START], registered_args, f.context)['preview']['reuse']['model_id'] == run['state']['model_id']
    with pytest.raises(AgentError): loop.get_run(identity.load_actor(2), run['id'])
    arguments = {'project_id': f.project['id'], 'run_id': run['id'], 'destinations':
        [{'record_ids': records, 'destination': 'original', 'name': '人工修订版'}]}
    with pytest.raises(AgentError): f.provider.prepare(f.actor, f.specs[loop.ROUTE], arguments, f.context)
    from services.review_service import submit_human_review
    for record_id in records:
        submit_human_review(record_id=record_id, reviewer_id=1, decision='no_defect',
            annotations=[], expected_version=0, database=f.db)
    assert loop.reconcile(f.actor, f.task['id'])['status'] == 'awaiting_input'
    run = loop.get_run(f.actor, run['id'])
    prepared = f.provider.prepare(f.actor, f.specs[loop.ROUTE], arguments, f.context)
    f.provider.execute(f.actor, f.specs[loop.ROUTE], arguments, request_key='route', context=f.context, prepared_id=prepared['prepared_id'])
    with pytest.raises(AgentError):
        f.provider.execute(f.actor, f.specs[loop.ROUTE], arguments, request_key='route', context=f.context, prepared_id=prepared['prepared_id'])
    loop._route(loop.get_run(f.actor, run['id']))
    run = loop.get_run(f.actor, run['id'])
    assert run['status'] == 'completed'
    assert len(run['state']['datasets']) == 1
    assert len(f.calls) == 3
    # 原标签未改，新版本已替换同源标签而非复制成重复图片。
    assert (f.source / 'labels/train/0.txt').read_text().strip() != ''
    from services.dataset_transform_service import _dataset_scan
    child = f.backend.resolve(1, 'dataset', run['state']['datasets'][0]['dataset_id'], ('dataset:read',))
    scan, _ = _dataset_scan(f.backend, child)
    assert scan['sample_count'] == 3
    assert all(Path(sample['label']).read_text() == '' for sample in scan['samples'])
    exported = run['state']['datasets'][0]['workspace_export']
    assert exported['status'] == 'succeeded'
    assert Path(exported['directory']).is_relative_to(f.root)
    assert (Path(exported['directory']) / 'labels/train/0.txt').read_text() == ''
    assert Path(exported['archive']).is_file()


def test_changed_source_is_rejected_and_startup_does_not_retry(setup):
    f = setup
    prepared = f.provider.prepare(f.actor, f.specs[loop.START], f.args, f.context)
    f.provider.execute(f.actor, f.specs[loop.START], f.args, request_key='start', context=f.context, prepared_id=prepared['prepared_id'])
    (f.root / 'best.pt').write_bytes(b'PKchanged')
    with pytest.raises(AgentError): loop._detect(loop.get_run(f.actor, prepared['prepared_id']))
    loop.interrupt_on_startup()
    assert loop.get_run(f.actor, prepared['prepared_id'])['status'] == 'interrupted'
    assert f.calls == []


def test_probe_image_is_decodable_and_bad_image_is_not_missing_vision():
    Image.open(io.BytesIO(_PROBE_PNG)).verify()
    image = Image.open(io.BytesIO(_PROBE_PNG)); image.load()
    assert image.size == (64, 64)
    exc = type('BadImage', (Exception,), {'status_code': 400})('You uploaded an unsupported image; make sure your image is valid')
    reason, message = classify_failure(exc)
    assert reason == REASON_PARSE_ERROR
    assert '不能证明' in message


def test_new_dataset_uses_only_confirmed_samples_and_explicit_splits(setup):
    f = setup; run = start(f); records = loop._review_queue(run)
    from services.review_service import submit_human_review
    for record_id in records:
        submit_human_review(record_id=record_id, reviewer_id=1, decision='no_defect',
                            annotations=[], expected_version=0, database=f.db)
    loop.reconcile(f.actor, f.task['id']); run = loop.get_run(f.actor, run['id'])
    group = {'record_ids': records[:2], 'destination': 'new', 'name': '独立反馈数据集',
             'splits': {records[0]: 'train', records[1]: 'val'}}
    arguments = {'project_id': f.project['id'], 'run_id': run['id'], 'destinations': [group]}
    prepared = f.provider.prepare(f.actor, f.specs[loop.ROUTE], arguments, f.context)
    f.provider.execute(f.actor, f.specs[loop.ROUTE], arguments, request_key='new', context=f.context, prepared_id=prepared['prepared_id'])
    loop._route(loop.get_run(f.actor, run['id'])); run = loop.get_run(f.actor, run['id'])
    assert run['status'] == 'awaiting_destination'
    assert len(run['state']['datasets']) == 1
    assert run['state']['datasets'][0]['total_samples'] == 2
    assert len(run['state']['routed_records']) == 2
    exported = run['state']['datasets'][0]['workspace_export']
    from services.authorized_directories import scan_dataset_tree
    scan = scan_dataset_tree(exported['directory'])
    assert scan['sample_count'] == 2
    assert {item['split'] for item in scan['samples']} == {'train', 'val'}
    assert all(set(item['modalities']) == {'rgb', 'ir'} for item in scan['samples'])
    from services.project_dataset_export import export_dataset_to_project
    dataset_id = run['state']['datasets'][0]['dataset_id']
    assert export_dataset_to_project(f.backend, f.actor, f.project['id'], dataset_id) == exported
    # 人工改了工作区文件后不能被“再次导出”覆盖；另一项目也不能取走它。
    label = Path(scan['samples'][0]['label']); label.write_text('0 0.5 0.5 0.2 0.2\n')
    with pytest.raises(ValueError, match='发生变化'):
        export_dataset_to_project(f.backend, f.actor, f.project['id'], dataset_id)
    assert label.read_text().strip() == '0 0.5 0.5 0.2 0.2'
    other = permissions.create_project(f.actor, '其他项目')
    with pytest.raises(AgentError):
        export_dataset_to_project(f.backend, f.actor, other['id'], dataset_id)


def test_workspace_export_failure_is_not_success_and_never_starts_training(setup, monkeypatch):
    f = setup; run = start(f); records = loop._review_queue(run)
    from services.review_service import submit_human_review
    submit_human_review(record_id=records[0], reviewer_id=1, decision='no_defect', annotations=[], expected_version=0, database=f.db)
    arguments = {'project_id': f.project['id'], 'run_id': run['id'], 'destinations': [{
        'record_ids': records[:1], 'destination': 'original', 'name': '导出失败待核实',
        'training': {'epochs': 1, 'batch_size': 1, 'publish': False}}]}
    prepared = f.provider.prepare(f.actor, f.specs[loop.ROUTE], arguments, f.context)
    f.provider.execute(f.actor, f.specs[loop.ROUTE], arguments, request_key='failed-export', context=f.context, prepared_id=prepared['prepared_id'])
    def denied(*args):
        raise PermissionError('输出目录不可写')
    monkeypatch.setattr('services.project_dataset_export.export_dataset_to_project', denied)
    with pytest.raises(AgentError, match='工作区导出失败'):
        loop._route(loop.get_run(f.actor, run['id']))
    run = loop.get_run(f.actor, run['id'])
    assert run['status'] != 'completed'
    assert len(run['state']['datasets']) == 1
    assert run['state']['datasets'][0]['workspace_export']['status'] == 'failed'
    assert not run['state'].get('training')
    assert f.db.query('SELECT count(*) AS n FROM models WHERE parent_model_id=?', (run['state']['model_id'],), fetchone=True)['n'] == 0


def test_feedback_can_launch_existing_training_and_reconcile_without_resubmitting(setup):
    f = setup; run = start(f); records = loop._review_queue(run)
    from services.review_service import submit_human_review
    for record_id in records:
        submit_human_review(record_id=record_id, reviewer_id=1, decision='no_defect', annotations=[], expected_version=0, database=f.db)
    loop.reconcile(f.actor, f.task['id'])
    arguments = {'project_id': f.project['id'], 'run_id': run['id'], 'destinations': [{
        'record_ids': records, 'destination': 'original', 'name': '训练用修订版',
        'training': {'epochs': 1, 'batch_size': 1, 'publish': False}}]}
    prepared = f.provider.prepare(f.actor, f.specs[loop.ROUTE], arguments, f.context)
    f.provider.execute(f.actor, f.specs[loop.ROUTE], arguments, request_key='training-route', context=f.context, prepared_id=prepared['prepared_id'])
    loop._route(loop.get_run(f.actor, run['id'])); run = loop.get_run(f.actor, run['id'])
    assert run['status'] == 'awaiting_training'
    child_id = run['state']['training']['model_id']
    # 仅模拟已注入 launcher 的结束事件；不宣称执行了 GPU 训练或改善了指标。
    f.db.execute('UPDATE models SET status=1 WHERE id=?', (child_id,))
    from services.training_runner import release_training_lock
    release_training_lock(f.backend.database_path, child_id)
    assert loop.reconcile(f.actor, f.task['id'])['status'] == 'completed'
    assert loop.reconcile(f.actor, f.task['id'])['status'] == 'completed'
    assert len(f.calls) == 3
    assert f.db.query('SELECT count(*) AS n FROM models WHERE parent_model_id=?', (run['state']['model_id'],), fetchone=True)['n'] == 1

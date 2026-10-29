"""项目存储必须同时覆盖网页和工具，不能泄漏到后端目录或其他项目。"""
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from agent import permissions
from routes.file import file_bp
from services.project_storage import project_scope, temp_directory, current_scope
from services.project_file_migration import migrate_project_files
from utils.jwt_util import generate_token
from tests.test_inspection_loop import setup, start


def _png():
    content = io.BytesIO()
    Image.new('RGB', (4, 4), 'red').save(content, format='PNG')
    return content.getvalue()


def test_concurrent_project_files_and_temp_paths_never_cross(setup):
    f = setup
    other_root = f.root.parent / 'other'; other_root.mkdir()
    other = permissions.create_project(f.actor, '第二项目', root_directory=str(other_root))
    source = f.root / 'input.png'; source.write_bytes(_png())
    storage = f.backend.storage_client
    def write(project_id):
        with project_scope(f.database, project_id):
            saved = storage.upload('detection_results', str(source))
            return saved, temp_directory(f.work)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [f.project['id'], other['id']]))
    for project, root, (saved, temporary) in zip([f.project, other], [f.root, other_root], results):
        assert saved['bucket'].startswith('workspace.' + project['id'] + '.')
        assert storage._path(saved['bucket'], saved['objectKey']).is_relative_to(root)
        assert Path(temporary) == root / '.cache'
        assert storage.get(saved['bucket'], saved['objectKey']) == _png()
    assert current_scope() is None
    assert not list(storage.base_path.rglob('*'))
    with project_scope(f.database, f.project['id']):
        with pytest.raises(ValueError, match='其他项目'):
            storage.upload(results[1][0]['bucket'], str(source))


def test_manual_upload_is_in_workspace_authenticated_and_requires_binding(setup):
    f = setup
    f.app.config['PROJECT_FILES_IN_WORKSPACE'] = True
    f.app.register_blueprint(file_bp)
    client = f.app.test_client()
    headers = {'Authorization': 'Bearer ' + generate_token(1)}
    response = client.post('/api/file/project/' + f.project['id'] + '/upload', headers=headers,
                           data={'file': (io.BytesIO(_png()), '现场.png')})
    assert response.status_code == 200, response.json
    data = response.json['data']
    assert data['bucket'].startswith('workspace.' + f.project['id'] + '.')
    assert f.backend.storage_client._path(data['bucket'], data['object_key']).is_relative_to(f.root)
    url = '/api/file/' + data['bucket'] + '/' + data['object_key']
    assert client.get(url, headers=headers).data == _png()
    assert client.get(url, headers={'Authorization': 'Bearer ' + generate_token(2)}).status_code == 403
    unbound = permissions.create_project(f.actor, '未绑定目录')
    response = client.post('/api/file/project/' + unbound['id'] + '/upload', headers=headers,
                           data={'file': (io.BytesIO(_png()), 'blocked.png')})
    assert response.status_code == 409
    assert not list(f.backend.storage_client.base_path.rglob('*'))


def test_migrate_existing_files_keeps_old_links_and_preserves_user_changes(setup):
    f = setup
    source = f.root / 'old.png'; source.write_bytes(_png())
    storage = f.backend.storage_client
    legacy = storage.upload('project_uploads', str(source))
    old_path = storage._path(legacy['bucket'], legacy['objectKey'])
    f.backend.file_assets.access.register_upload(1, legacy['bucket'], legacy['objectKey'],
        'image', 'image/png', source.stat().st_size, f.project['id'])
    migrated = migrate_project_files(f.database, storage, f.project['id'])
    assert migrated['copied'] == 1
    canonical = storage._path(legacy['bucket'], legacy['objectKey'])
    assert canonical.is_relative_to(f.root)
    assert canonical != old_path and old_path.read_bytes() == canonical.read_bytes()
    assert migrate_project_files(f.database, storage, f.project['id'])['copied'] == 0
    f.app.register_blueprint(file_bp)
    url = '/api/file/' + legacy['bucket'] + '/' + legacy['objectKey']
    assert f.app.test_client().get(url, headers={'Authorization': 'Bearer ' + generate_token(1)}).data == _png()
    canonical.write_bytes(b'user changed')
    with pytest.raises(ValueError, match='修改'):
        migrate_project_files(f.database, storage, f.project['id'])
    assert canonical.read_bytes() == b'user changed'


def test_agent_loop_leaves_no_business_files_in_backend_store(setup):
    f = setup
    f.app.config['PROJECT_FILES_IN_WORKSPACE'] = True
    run = start(f)
    assert len(run['state']['items']) == 3
    assert not list(f.backend.storage_client.base_path.rglob('*'))
    assert (f.root / 'artifacts/project_uploads').is_dir()
    assert (f.root / 'artifacts/models').is_dir()
    assert (f.root / 'artifacts/detection_results').is_dir()
    assert not list(f.work.rglob('*.pt'))
    assert not list(f.work.rglob('*.jpg'))


def test_training_worker_keeps_outputs_in_workspace_after_cache_cleanup(setup, monkeypatch):
    f = setup; run = start(f)
    from services.model_registry_service import start_incremental_training
    from services.training_runner import run_training_job, acquire_training_lock, read_lock
    from services.training_service import TrainingResult
    child = start_incremental_training(run['state']['model_id'], run['state']['dataset_id'],
                                      {'epochs': 1, 'batch_size': 1}, f.db)
    def fake_training(settings):
        output = Path(settings.output_dir)
        assert output.is_relative_to(f.root / 'training')
        assert Path(settings.runtime_yaml).is_relative_to(f.root / '.cache')
        weight = output / 'train/weights/best.pt'
        weight.parent.mkdir(parents=True)
        weight.write_bytes(b'test-only-weight')
        return TrainingResult(str(weight), {'mAP50': 0.5})
    monkeypatch.setattr('services.training_runner.run_training', fake_training)
    acquire_training_lock(f.database, child.model_id)
    result = run_training_job(child.model_id, child.dataset_id, '', 1, 1,
        parent_model_id=run['state']['model_id'], database_path=f.database,
        file_store_path=f.backend.storage_client.base_path, temp_root=f.work, project_id=f.project['id'])
    assert result['status'] == 'completed', result
    output = f.root / 'training' / child.model_id
    assert (output / 'train/weights/best.pt').read_bytes() == b'test-only-weight'
    assert (output / 'metrics.json').is_file()
    assert not list((f.root / '.cache').glob('model_training_*'))
    assert read_lock(f.database) is None
    assert f.backend._model_row(child.model_id)['model_bucket'].startswith('workspace.' + f.project['id'] + '.')

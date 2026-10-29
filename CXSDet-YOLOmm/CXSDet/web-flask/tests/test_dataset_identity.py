import hashlib
import io
import json
import random
import zipfile
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from services.dataset_identity_service import image_identity, match_image
from tests.test_inspection_loop import setup
from tests.test_feedback_revision import feedback, args


def picture(seed=1):
    randomizer = random.Random(seed)
    image = Image.frombytes('RGB', (24, 24), randomizer.randbytes(24*24*3))
    output = io.BytesIO(); image.save(output, 'PNG'); return output.getvalue()


def encoded(payload):
    output = io.BytesIO()
    with Image.open(io.BytesIO(payload)) as im: im.convert('RGB').save(output, 'JPEG', quality=95)
    return output.getvalue()


def index(payload, name='000084.png'):
    return [{'key': 'train/000084', 'identity': image_identity(payload, name, legacy=True)}]


def test_file_name_changes_do_not_change_content_identity():
    raw = picture()
    assert match_image(raw, index(raw), 'renamed.png')['classification'] == 'existing'


def test_old_jpeg_upload_is_same_source_not_a_new_sample():
    raw = picture(); legacy = encoded(raw)
    result = match_image(legacy, index(raw), 'rgb.jpg')
    assert result['classification'] == 'existing'
    assert result['match_basis'] == 'legacy_jpeg95'
    assert hashlib.sha256(raw).hexdigest() != result['image']['sha256']


def test_changed_container_metadata_is_matched_by_pixels():
    raw = picture(); output = io.BytesIO()
    info = PngImagePlugin.PngInfo(); info.add_text('test', 'metadata changed')
    with Image.open(io.BytesIO(raw)) as image: image.save(output, 'PNG', pnginfo=info)
    assert match_image(output.getvalue(), index(raw))['match_basis'] == 'decoded_pixels'


def test_same_name_and_multiple_content_matches_require_human_resolution():
    raw = picture()
    assert match_image(picture(2), index(raw), '000084.png')['classification'] == 'ambiguous'
    assert match_image(raw, index(raw) + index(raw))['classification'] == 'ambiguous'
    assert match_image(picture(2), index(raw), 'new.png')['classification'] == 'new'


def test_revision_handles_legacy_rgb_and_secondary_without_appending(setup):
    f = setup; dataset_id, record = feedback(f)
    for prefix in ['original_image', 'secondary_image']:
        data = f.backend.storage_client.get(record[prefix+'_bucket'], record[prefix+'_object_key'])
        path = f.root / (prefix+'.jpg'); path.write_bytes(encoded(data))
        obj = f.backend.storage_client.upload('legacy-fixture', str(path))
        f.db.execute(f'UPDATE detection_records SET {prefix}_bucket=?,{prefix}_object_key=? WHERE id=?', (obj['bucket'],obj['objectKey'],record['id']))
    prepared = f.backend.prepare_tool(f.actor.user_id, 'dataset.feedback_revision', args(dataset_id))
    preview = prepared['preview']
    assert preview['source']['revision']['replaced_samples'] == 1
    assert preview['source']['revision']['added_samples'] == 0
    assert preview['sample_count'] == 3 and not preview['blocked']
    assert preview['source']['feedback_samples'][0]['match_basis'].startswith('legacy_jpeg95')
    result = f.backend.execute_tool(f.actor.user_id, prepared['operation_id'], 'legacy-revision')
    assert result['status'] == 'succeeded'
    labels = list(Path(result['outputs']['output_dir']).rglob('*.txt'))
    assert len(labels) == 3 and sum(path.read_text() == '' for path in labels) == 1


def test_agent_read_only_audit_finds_duplicate_and_split_leakage(setup):
    f = setup; dataset_id, record = feedback(f)
    source = f.backend._dataset_row(dataset_id)
    raw = f.backend.storage_client.get(source['zip_bucket'],source['zip_object_key'])
    bundle = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source_zip, zipfile.ZipFile(bundle, 'w') as target:
        for name in source_zip.namelist(): target.writestr(name, source_zip.read(name))
        train = next(name for name in source_zip.namelist() if name.startswith('images/train/') and name.endswith('.png'))
        for directory, suffix in [('images','.png'),('images_ir','.png'),('labels','.txt')]:
            original = directory + train[len('images'):]
            if directory == 'labels': original = str(Path(original).with_suffix('.txt')).replace('\\','/')
            target.writestr(directory+'/test/duplicate'+suffix, source_zip.read(original))
    path = f.root/'with-duplicate.zip'; path.write_bytes(bundle.getvalue())
    obj = f.backend.storage_client.upload('test-datasets', str(path))
    f.db.execute('UPDATE datasets SET zip_bucket=?,zip_object_key=? WHERE id=?', (obj['bucket'],obj['objectKey'],dataset_id))
    before = f.db.query('SELECT COUNT(*) AS n FROM datasets', fetchone=True)['n']
    prepared = f.backend.prepare_tool(f.actor.user_id, 'dataset.inspect_samples', {'dataset_id':dataset_id, 'page_size':1})
    result = f.backend.execute_tool(f.actor.user_id, prepared['operation_id'], 'audit-only')
    assert result['status'] == 'succeeded'
    output = result['outputs']
    assert output['duplicate_groups'] == 1 and output['duplicates'][0]['cross_split']
    assert output['total_samples'] == 4 and len(output['samples']) == 1
    assert output['samples'][0]['identity']['sha256']
    assert output['feedback_matches'][0]['record_id'] == record['id']
    assert f.db.query('SELECT COUNT(*) AS n FROM datasets', fetchone=True)['n'] == before
    with pytest.raises(PermissionError):
        f.backend.prepare_tool(2, 'dataset.inspect_samples', {'dataset_id':dataset_id})


def test_read_only_audit_http_exposes_file_identity_for_authorized_record(setup):
    from routes.dataset import dataset_bp
    from utils.jwt_util import generate_token
    f=setup;dataset_id,record=feedback(f)
    f.app.register_blueprint(dataset_bp,url_prefix='/api/dataset')
    headers={'Authorization':'Bearer '+generate_token(f.actor.user_id),'X-Project-Id':f.project['id']}
    response=f.app.test_client().get('/api/dataset/'+dataset_id+'/inspect',headers=headers,
                                   query_string={'record_id':record['id'],'page_size':1})
    body=response.get_json()
    assert body['code']==200,body
    output=body['data']
    assert len(output['samples'])==1 and output['samples'][0]['label']
    assert output['feedback_matches'][0]['classification']=='existing'
    assert output['feedback_matches'][0]['input_identity']['rgb']['sha256']

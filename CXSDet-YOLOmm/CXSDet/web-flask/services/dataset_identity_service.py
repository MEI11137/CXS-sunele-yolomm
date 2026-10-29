"""Image identity and read-only dataset audits shared by UI and Agent tools."""
from __future__ import annotations

import hashlib
import io
import json
import shutil
from pathlib import Path

from PIL import Image, UnidentifiedImageError


def image_identity(payload, name='', *, legacy=False):
    info = {'original_name': Path(str(name).replace('\\', '/')).name,
            'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
    try:
        with Image.open(io.BytesIO(payload)) as image:
            rgb = image.convert('RGB')
            info.update(width=rgb.width, height=rgb.height, format=image.format,
                        pixel_sha256=hashlib.sha256(f'{rgb.size}:RGB:'.encode() + rgb.tobytes()).hexdigest())
            if legacy:
                # The old manual upload endpoint performed precisely this conversion.
                encoded = io.BytesIO(); rgb.save(encoded, 'JPEG', quality=95)
                old = image_identity(encoded.getvalue())
                info.update(legacy_jpeg_sha256=old['sha256'], legacy_pixel_sha256=old.get('pixel_sha256'))
    except (UnidentifiedImageError, OSError):
        # Archive validators handle invalid images; byte identity remains useful for diagnostics.
        info['unreadable'] = True
    return info


def sample_index(scan):
    index = []
    root = Path(scan['root'])
    for sample in scan['samples']:
        rgb = sample['modalities'].get('rgb')
        if not rgb:
            continue
        index.append({**sample, 'identity': image_identity(Path(rgb).read_bytes(), Path(rgb).name, legacy=True),
                      'rgb_relative': Path(rgb).relative_to(root).as_posix(),
                      'label_relative': Path(sample['label']).relative_to(root).as_posix() if sample.get('label') else None})
    return index


def match_image(payload, index, name=''):
    identity = image_identity(payload, name)
    stages = [('file_sha256', 'sha256', 'sha256'), ('decoded_pixels', 'pixel_sha256', 'pixel_sha256'),
              ('legacy_jpeg95', 'sha256', 'legacy_jpeg_sha256'),
              ('legacy_jpeg95_pixels', 'pixel_sha256', 'legacy_pixel_sha256')]
    for basis, query_key, sample_key in stages:
        value = identity.get(query_key)
        matches = [item for item in index if value and item['identity'].get(sample_key) == value]
        if matches:
            return {'classification': 'existing' if len(matches) == 1 else 'ambiguous',
                    'match_basis': basis, 'image': identity, 'candidates': matches}
    same_name = [item for item in index if identity['original_name'] and item['identity']['original_name'].casefold() == identity['original_name'].casefold()]
    return {'classification': 'ambiguous' if same_name else 'new',
            'match_basis': 'filename_only' if same_name else 'no_content_match',
            'image': identity, 'candidates': same_name}


def public_match(result):
    return {**{key: result[key] for key in ('classification', 'match_basis', 'image')},
            'candidates': [{'sample_key': item['key'], 'split': item['split'],
                            'image': item['rgb_relative'], 'label': item['label_relative'],
                            'identity': item['identity']} for item in result['candidates']]}


def inspect_dataset(adapter, resource, *, record=None, page=1, page_size=50, search=''):
    from services.dataset_transform_service import _dataset_scan, _feedback_samples
    scan, source = _dataset_scan(adapter, resource)
    try:
        index = sample_index(scan)
        # Union by exact bytes, decoded pixels and the historical JPEG conversion.
        parents = list(range(len(index)))
        def find(i):
            while parents[i] != i:
                parents[i] = parents[parents[i]]; i = parents[i]
            return i
        seen = {}
        for i, sample in enumerate(index):
            identity = sample['identity']
            values = [('file', identity.get('sha256')), ('pixels', identity.get('pixel_sha256')),
                      ('file', identity.get('legacy_jpeg_sha256')), ('pixels', identity.get('legacy_pixel_sha256'))]
            for kind, digest in values:
                if not digest: continue
                key = (kind, digest)
                if key in seen: parents[find(i)] = find(seen[key])
                else: seen[key] = i
        groups = {}
        for i, sample in enumerate(index): groups.setdefault(find(i), []).append(sample)
        duplicates = []
        for group in groups.values():
            if len(group) < 2: continue
            splits = sorted({item['split'] for item in group})
            labels = {Path(item['label']).read_bytes() for item in group if item.get('label')}
            duplicates.append({'sample_keys': [item['key'] for item in group], 'splits': splits,
                               'cross_split': len(splits) > 1, 'label_conflict': len(labels) > 1})
        records = [record] if record else _feedback_samples(adapter, resource.id)
        feedback = []
        for row in records[:100]:
            metadata = json.loads(row.get('input_identity_json') or '{}')
            name = (metadata.get('rgb') or {}).get('original_name', '')
            result = match_image(adapter.storage_client.get(row['original_image_bucket'], row['original_image_object_key']), index, name)
            feedback.append({'record_id': row.get('record_id') or row.get('id'), 'review_version': row.get('review_version'),
                             'input_identity': metadata, **public_match(result)})
        selected = [item for item in index if not search or search.casefold() in item['key'].casefold()]
        start = (page - 1) * page_size
        return {'dataset_id': resource.id, 'source': source, 'total_samples': scan['sample_count'],
                'scan_complete': scan['sample_count'] == len(scan['samples']),
                'duplicates': duplicates[:100], 'duplicate_groups': len(duplicates),
                'duplicates_truncated': len(duplicates) > 100,
                'feedback_matches': feedback, 'feedback_truncated': len(records) > 100,
                'page': page, 'page_size': page_size, 'filtered_total': len(selected),
                'samples': [{'sample_key': item['key'], 'split': item['split'], 'image': item['rgb_relative'],
                             'label': item['label_relative'], 'identity': item['identity'],
                             'modalities': sorted(item['modalities'])} for item in selected[start:start+page_size]],
                'note': '只读检查；文件名、时间和大小不单独作为覆盖依据。相似、裁剪或未知重编码图片不保证能自动认同，疑似冲突需人工核对。'}
    finally:
        shutil.rmtree(scan['_workspace'], ignore_errors=True)

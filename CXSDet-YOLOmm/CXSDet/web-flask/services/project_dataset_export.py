"""将已登记的数据集发布到其本人项目目录；保留源文件，不覆盖用户修改。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile

from agent import permissions
from agent.errors import AgentError, Code
from agent.project_folders import checked_project_root
from services.dataset_contract import safe_extract_dataset


def _digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _files(directory):
    files = {}
    for path in directory.rglob('*'):
        if path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError('导出目录包含链接，不能覆盖或沿链接访问')
        if path.is_file():
            files[path.relative_to(directory).as_posix()] = _digest(path)
    return files


def export_dataset_to_project(backend, actor, project_id, dataset_id):
    resource = backend.resolve(actor.user_id, 'dataset', dataset_id, ('dataset:read',))
    if resource.project_id != project_id:
        raise AgentError(Code.FORBIDDEN, '只能导出当前项目的数据集', http_status=403)
    project = permissions.get_project(actor, project_id)
    root = checked_project_root(actor, project)
    if root is None or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', dataset_id):
        raise ValueError('请先绑定有效的本机项目目录')
    base = root / 'datasets'
    base.mkdir(exist_ok=True)
    if base.resolve() != base.absolute():
        raise ValueError('工作区 datasets 目录不能是链接或重定向目录')
    target = base / dataset_id
    row = backend._dataset_row(dataset_id)
    payload = backend.storage_client.get(row['zip_bucket'], row['zip_object_key'])
    archive_hash = hashlib.sha256(payload).hexdigest()
    if target.exists() or target.is_symlink():
        if target.resolve() != target.absolute():
            raise ValueError('数据集输出目录不能是链接')
        manifest_path = target / 'export.json'
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise ValueError('输出目录已存在，不能覆盖已有文件')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        actual = _files(target)
        actual.pop('export.json', None)
        if (manifest.get('project_id') != project_id or manifest.get('dataset_id') != dataset_id
                or manifest.get('archive_sha256') != archive_hash or actual != manifest.get('files')):
            raise ValueError('已导出文件发生变化，保留用户文件，不自动覆盖')
    else:
        # 临时文件与最终输出均在已校验的同一工作区；完整写入后一次改名可见。
        with tempfile.TemporaryDirectory(prefix='.dataset-export-', dir=base) as scratch:
            stage = Path(scratch) / 'output'; stage.mkdir()
            archive = stage / 'dataset.zip'; archive.write_bytes(payload)
            extracted = Path(safe_extract_dataset(str(archive), str(stage / 'dataset')))
            manifest = {'project_id': project_id, 'dataset_id': dataset_id, 'name': row['name'],
                        'archive_sha256': archive_hash,
                        'dataset_subdirectory': extracted.relative_to(stage).as_posix(), 'files': _files(stage)}
            (stage / 'export.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            if checked_project_root(actor, permissions.get_project(actor, project_id)) != root or base.resolve() != base.absolute():
                raise ValueError('项目目录在导出时改变，已停止')
            if target.exists() or target.is_symlink():
                raise ValueError('输出目录已出现，不能覆盖已有文件')
            # 源/目标绝对路径均已核实位于 root/datasets，不跨目录移动用户数据。
            stage.rename(target)
    dataset_directory = (target / manifest['dataset_subdirectory']).resolve(strict=True)
    if not dataset_directory.is_relative_to(target / 'dataset') or not dataset_directory.is_dir():
        raise ValueError('导出清单中的数据集目录无效')
    return {'status': 'succeeded', 'directory': str(dataset_directory),
            'archive': str(target / 'dataset.zip'), 'manifest': str(target / 'export.json'),
            'archive_sha256': archive_hash}

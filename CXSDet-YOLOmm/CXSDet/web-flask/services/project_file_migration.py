"""将本人项目的历史对象复制到工作区，旧 URL 通过别名继续读取；源文件保留。"""
import hashlib
from pathlib import Path
import sqlite3
from contextlib import closing

from services.project_storage import project_scope


def _hash(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def migrate_project_files(database, storage, project_id):
    with closing(sqlite3.connect(str(database))) as conn, conn:
        conn.row_factory = sqlite3.Row
        resources = conn.execute('SELECT source_table,source_id FROM agent_resource WHERE project_id=? AND source_id IS NOT NULL', (project_id,)).fetchall()
        refs = set()
        # 只接受服务器已登记的业务表，不把模型输出或请求参数当作 SQL 标识符。
        allowed = {'datasets', 'models', 'detection_records', 'video_detection_records', 'realtime_detection_records', 'dataset_drafts'}
        for resource in resources:
            table = resource['source_table']
            if table not in allowed:
                continue
            row = conn.execute(f'SELECT * FROM "{table}" WHERE id=?', (resource['source_id'],)).fetchone()
            if row is None:
                continue
            for column in row.keys():
                key_column = column[:-7] + '_object_key' if column.endswith('_bucket') else ''
                if key_column in row.keys() and row[column] and row[key_column]:
                    refs.add((row[column], row[key_column]))
            if table == 'dataset_drafts':
                for sample in conn.execute('SELECT * FROM dataset_draft_samples WHERE draft_id=?', (resource['source_id'],)):
                    for column in sample.keys():
                        key_column = column[:-7] + '_object_key' if column.endswith('_bucket') else ''
                        if key_column in sample.keys() and sample[column] and sample[key_column]:
                            refs.add((sample[column], sample[key_column]))
        refs.update(tuple(row) for row in conn.execute('SELECT bucket,object_key FROM file_assets WHERE project_id=? AND status<>?', (project_id, 'deleted')))
        foreign = set(tuple(row) for row in conn.execute('SELECT bucket,object_key FROM file_assets WHERE project_id<>? AND status<>?', (project_id, 'deleted')))
        # 旧对象被其他项目共同引用时不能把全局旧链接暗中指向当前项目。
        for table in allowed:
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            for column in columns:
                key_column = column[:-7] + '_object_key' if column.endswith('_bucket') else ''
                if key_column not in columns:
                    continue
                foreign.update(tuple(row) for row in conn.execute(
                    f'SELECT t."{column}",t."{key_column}" FROM "{table}" t JOIN agent_resource r ON r.source_table=? AND r.source_id=t.id WHERE r.project_id<>?',
                    (table, project_id)))
        if refs & foreign:
            raise ValueError('历史文件被多个项目共同引用，不能自动切换其工作区；请先明确归属')
        conn.execute('''CREATE TABLE IF NOT EXISTS workspace_file_alias (
            old_bucket TEXT NOT NULL, old_key TEXT NOT NULL, project_id TEXT NOT NULL,
            bucket TEXT NOT NULL, object_key TEXT NOT NULL, sha256 TEXT NOT NULL,
            PRIMARY KEY(old_bucket,old_key))''')
    result = {'project_id': project_id, 'copied': 0, 'already_in_workspace': 0, 'files': []}
    with project_scope(database, project_id) as scope:
        if scope is None:
            raise ValueError('历史文件迁移必须绑定工作区')
        for bucket, key in sorted(refs):
            if bucket.startswith('workspace.'):
                storage._path(bucket, key)  # 验证实际工作区路径。
                result['already_in_workspace'] += 1
                continue
            with closing(sqlite3.connect(str(database))) as conn:
                prior = conn.execute('SELECT project_id,bucket,object_key,sha256 FROM workspace_file_alias WHERE old_bucket=? AND old_key=?', (bucket, key)).fetchone()
            if prior:
                if prior[0] != project_id or _hash(storage._path(prior[1], prior[2])) != prior[3]:
                    raise ValueError('历史文件归属冲突或工作区副本已修改，停止迁移')
                result['already_in_workspace'] += 1
                continue
            source = storage._path(bucket, key)
            digest = _hash(source)
            new_bucket = f'workspace.{project_id}.{bucket}'
            target = storage._path(new_bucket, source.name)
            if target.exists():
                if _hash(target) != digest:
                    raise ValueError('目标文件已存在且不同，保留用户文件并停止迁移')
                saved = {'bucket': new_bucket, 'objectKey': source.name}
            else:
                saved = storage.upload(bucket, str(source), is_cache=True)
            if _hash(storage._path(saved['bucket'], saved['objectKey'])) != digest:
                raise ValueError('迁移文件校验失败，未切换旧链接')
            with closing(sqlite3.connect(str(database))) as conn, conn:
                conn.execute('INSERT INTO workspace_file_alias VALUES (?,?,?,?,?,?)',
                    (bucket, key, project_id, saved['bucket'], saved['objectKey'], digest))
            result['copied'] += 1
            result['files'].append({'bucket': bucket, 'key': key, 'path': str(target), 'sha256': digest})
    return result

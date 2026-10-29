"""记录删除仅改变可见性，保留图片、人工结果和回流来源供审计/恢复。"""
import sqlite3

from services.review_service import ReviewConflictError, ReviewValidationError


def delete_preview(actor, project_id, entries):
    from agent import permissions, resources
    project = permissions.get_project(actor, project_id)
    if project.get('origin') == 'system':
        raise ReviewValidationError('系统默认项目只读')
    if not isinstance(entries, list) or not 1 <= len(entries) <= 50:
        raise ReviewValidationError('请选择1–50条记录')
    seen, result = set(), []
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get('expected_version'), int) or isinstance(item.get('expected_version'), bool):
            raise ReviewValidationError('每条记录需要真实ID和当前复核版本')
        record_id = item.get('record_id')
        if not isinstance(record_id, str) or not record_id or record_id in seen:
            raise ReviewValidationError('记录ID不能为空或重复')
        seen.add(record_id)
        resource = resources.ensure_kind('detection_record', record_id)
        if not resource or resource['project_id'] != project_id or resource['owner_id'] != actor.user_id:
            raise ReviewValidationError('记录不存在或不属于当前账号/项目')
        permissions.require_access(actor, resource, 'write')
        row = resources.business_row('detection_records', record_id)
        if row is None or row['user_id'] != actor.user_id:
            raise ReviewValidationError('记录不存在或不属于当前账号')
        if int(row.get('review_version') or 0) != item['expected_version']:
            raise ReviewConflictError('记录已变化，请刷新后重新确认')
        result.append({'record_id': record_id, 'title': row.get('title') or record_id, 'expected_version': item['expected_version'], 'record_status': row.get('record_status')})
    return {'project_id': project_id, 'count': len(result), 'records': result,
            'impact': '仅隐藏选中记录，保留图片、模型、数据集和回流来源；可由助手恢复，不删除文件。'}


def set_deleted_many(database, entries, user_id):
    """上层先核实项目范围；事务内再核对所有归属和版本，冲突则整批不写。"""
    if not isinstance(entries, list) or not 1 <= len(entries) <= 50:
        raise ReviewValidationError('请选择1–50条记录')
    conn = database.get_connection(); conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN IMMEDIATE')
        rows, seen = [], set()
        for item in entries:
            if not isinstance(item, dict): raise ReviewValidationError('每条记录必须包含ID和当前版本')
            key, version = item.get('record_id'), item.get('expected_version')
            if key in seen or not isinstance(key, str) or not isinstance(version, int) or isinstance(version, bool):
                raise ReviewValidationError('记录ID或版本无效，不能重复删除')
            seen.add(key)
            row = conn.execute('SELECT * FROM detection_records WHERE id=? AND user_id=?', (key, user_id)).fetchone()
            if not row: raise ReviewValidationError('记录不存在或不属于当前用户')
            if int(row['review_version'] or 0) != version: raise ReviewConflictError('记录已变化，整批删除未执行，请刷新后重新确认')
            rows.append(row)
        results = []
        for row in rows:
            version = int(row['review_version'] or 0)
            if row['record_status'] != 'deleted':
                version += 1
                conn.execute("UPDATE detection_records SET record_status='deleted',review_version=? WHERE id=?", (version, row['id']))
                conn.execute("UPDATE agent_resource SET revision=revision+1 WHERE source_table='detection_records' AND source_id=?", (row['id'],))
            results.append({'record_id': row['id'], 'record_status': 'deleted', 'review_version': version, 'files_preserved': True, 'recoverable': True})
        conn.commit()
        return {'count': len(results), 'records': results, 'files_preserved': True, 'recoverable': True}
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


def set_deleted(database, record_id, user_id, expected_version, *, deleted=True):
    conn = database.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM detection_records WHERE id=? AND user_id=?', (record_id, user_id)).fetchone()
        if not row:
            raise ReviewValidationError('识别记录不存在或不属于当前用户')
        version = int(row['review_version'] or 0)
        if expected_version is not None and version != expected_version:
            raise ReviewConflictError('记录已变化，请重新查看并确认')
        target = 'deleted' if deleted else 'completed'
        if row['record_status'] != target:
            version += 1
            conn.execute('UPDATE detection_records SET record_status=?,review_version=? WHERE id=?', (target, version, record_id))
        conn.commit()
        return {'record_id': record_id, 'record_status': target, 'review_version': version,
                'files_preserved': True, 'recoverable': True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

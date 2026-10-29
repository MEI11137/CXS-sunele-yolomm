"""页面与 Agent 共用的数据集/模型/草稿回收站；不删除业务行和文件。"""
from agent import permissions, resources
from agent.errors import AgentError, Code
from agent.store import connect, query_one, query_all, now, dumps, loads

TABLES = {'dataset': 'datasets', 'model': 'models', 'draft': 'dataset_drafts'}


def _error(message):
    raise AgentError(Code.VALIDATION, message, http_status=409)


def _row(conn, actor, project_id, kind, resource_id):
    if kind not in TABLES:
        _error('仅支持数据集、模型和草稿')
    project = permissions.get_project(actor, project_id)
    row = query_one(conn, 'SELECT * FROM agent_resource WHERE source_table=? AND source_id=? AND project_id=? AND owner_id=?',
                    (TABLES[kind], resource_id, project_id, actor.user_id))
    if not row or project['owner_id'] != actor.user_id:
        raise AgentError(Code.RESOURCE_NOT_FOUND, '资源不存在或不属于当前账号/项目', http_status=404)
    if project.get('origin') == 'system' or row['access_policy'] == permissions.POLICY_SHARED_READONLY:
        raise AgentError(Code.FORBIDDEN, '系统默认资源只读，不能删除或恢复', http_status=403)
    business = query_one(conn, f'SELECT * FROM {TABLES[kind]} WHERE id=?', (resource_id,))
    if not business:
        _error('业务资源已不存在，不能恢复空记录')
    return row, business


def _blockers(conn, row, business, kind, operation, task_id):
    result = []
    if query_one(conn, "SELECT id FROM agent_task WHERE project_id=? AND id<>? AND status IN ('running','planning','awaiting_business','unknown') LIMIT 1",
                 (row['project_id'], task_id or '')):
        result.append('项目有正在执行或待核实任务，请先完成或停止后再操作')
    if kind == 'model' and int(business.get('status', -1)) == 0:
        result.append('模型正在训练，不能删除或恢复')
    if kind == 'draft' and business.get('workflow_status') in ('generating', 'processing'):
        result.append('草稿正在生成，不能删除或恢复')
    if operation == 'delete':
        if kind == 'model' and (int(business.get('status', -1)) == 3 or business.get('publish_status') == 'published'):
            result.append('模型仍已发布，请先撤销发布再删除')
        if kind == 'dataset':
            models = query_all(conn, "SELECT m.id,m.name FROM models m LEFT JOIN agent_resource a ON a.source_table='models' AND a.source_id=m.id WHERE m.dataset_id=? AND a.deleted_at IS NULL", (business['id'],))
            if models:
                result.append('仍被模型引用，请先处理这些模型：' + '、'.join(f"{m['name']} ({m['id']})" for m in models))
    elif kind == 'model':
        source = query_one(conn, "SELECT deleted_at FROM agent_resource WHERE source_table='datasets' AND source_id=?", (business.get('dataset_id'),))
        if source and source['deleted_at']:
            result.append('来源数据集在回收站，请先恢复来源数据集')
    return result


def preview(actor, project_id, kind, resource_id, operation, *, task_id=None):
    if operation not in ('delete', 'restore'):
        _error('操作必须为 delete 或 restore')
    # 补登记只使用业务行已有的真实归属，不相信请求中的 owner。
    resources.ensure_kind(kind, resource_id)
    conn = connect()
    try:
        row, business = _row(conn, actor, project_id, kind, resource_id)
        blockers = _blockers(conn, row, business, kind, operation, task_id)
        return {'project_id': project_id, 'kind': kind, 'id': resource_id, 'name': business.get('name', row['display_name']),
                'revision': row['revision'], 'deleted_at': row.get('deleted_at'), 'operation': operation,
                'blocked_reasons': blockers, 'recoverable': True, 'files_preserved': True,
                'impact': '移入回收站，隐藏业务入口，保留源文件、权重、历史记录和回流来源；不级联删除' if operation == 'delete' else
                          '恢复业务入口和保留的文件引用；不会自动发布模型或启动任务'}
    finally:
        conn.close()


def change(actor, project_id, kind, resource_id, operation, expected_revision, *, request_key, action_id=None, task_id=None):
    if operation not in ('delete', 'restore') or not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
        _error('需要明确的操作和当前资源版本，请重新预览')
    conn = connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row, business = _row(conn, actor, project_id, kind, resource_id)
        previous = query_one(conn, 'SELECT * FROM agent_resource_change WHERE request_key=?', (request_key,))
        if previous:
            if (previous['owner_id'], previous['project_id'], previous['resource_id'], previous['operation'], previous['action_id']) != (actor.user_id, project_id, row['id'], operation, action_id):
                _error('请求键已被其他操作使用')
            conn.commit()
            return loads(previous['outputs_json'])
        if row['revision'] != expected_revision:
            _error('资源已变化，请重新预览并确认')
        blockers = _blockers(conn, row, business, kind, operation, task_id)
        if blockers:
            _error('；'.join(blockers))
        deleting = operation == 'delete'
        if bool(row.get('deleted_at')) != deleting:
            conn.execute('UPDATE agent_resource SET deleted_at=?, revision=revision+1,updated_at=? WHERE id=?',
                         (now() if deleting else None, now(), row['id']))
        current = query_one(conn, 'SELECT revision,deleted_at FROM agent_resource WHERE id=?', (row['id'],))
        outputs = {'kind': kind, 'id': resource_id, 'project_id': project_id, 'name': business.get('name', row['display_name']),
                   'revision': current['revision'], 'deleted_at': current['deleted_at'], 'recoverable': True, 'files_preserved': True}
        conn.execute('INSERT INTO agent_resource_change VALUES (?,?,?,?,?,?,?,?)',
                     (request_key, action_id, actor.user_id, project_id, row['id'], operation, dumps(outputs), now()))
        conn.commit()
        return outputs
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_deleted(actor, project_id):
    permissions.get_project(actor, project_id)
    conn = connect()
    try:
        return query_all(conn, "SELECT kind,source_id AS id,display_name AS name,revision,deleted_at FROM agent_resource WHERE project_id=? AND owner_id=? AND kind IN ('dataset','model','draft') AND deleted_at IS NOT NULL ORDER BY deleted_at DESC", (project_id, actor.user_id))
    finally:
        conn.close()

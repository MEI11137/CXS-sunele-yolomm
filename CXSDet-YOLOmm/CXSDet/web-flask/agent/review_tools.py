"""海巡助手复核操作：模型提出改动，用户真实确认后复用页面业务服务。"""
import hashlib
import io
import json
import re
from pathlib import Path

from PIL import Image
from agent import actions, permissions, sessions, tasks
from agent.errors import AgentError, Code
from agent.store import connect, dumps, loads, now
from agent.tools.business import get_backend
from agent.tools.contract import ToolProvider, ToolSpec, OperationResult, RISK_CRITICAL
from services.project_storage import project_tool, directory
from services.review_service import _validate_annotations, submit_human_review, ReviewConflictError, ReviewValidationError
from utils.dataset_classes import dataset_class_names

WRITE = 'workspace_review_record'
STATE = 'workspace_review_state'
VIEW = 'workspace_review_view'


def _error(message, code=Code.VALIDATION):
    raise AgentError(code, message, http_status=409)


def _write_access(actor, arguments):
    project = permissions.get_project(actor, arguments['project_id'])
    if project.get('origin') == 'system' or project['owner_id'] != actor.user_id:
        _error('只能修改本人的可写项目，系统默认资源保持只读', Code.FORBIDDEN)
    get_backend().resolve(actor.user_id, 'record', arguments['record_id'], ('detection:review',))


def record_state(actor, project_id, record_id):
    backend = get_backend()
    resource = backend.resolve(actor.user_id, 'record', record_id, ('detection:read',))
    if resource.project_id != project_id:
        _error('记录不属于当前项目', Code.FORBIDDEN)
    row = backend.database().query('SELECT * FROM detection_records WHERE id=?', (record_id,), fetchone=True)
    if not row or int(row['user_id']) != actor.user_id:
        _error('记录不存在或不属于本人', Code.FORBIDDEN)
    source = backend.database().query('SELECT d.structure_info FROM models m LEFT JOIN datasets d ON d.id=m.dataset_id WHERE m.id=?',
                                      (row['model_id'],), fetchone=True)
    classes = dataset_class_names(source['structure_info'] if source else None) or []
    annotations = loads(row.get('corrected_annotations'), [])
    if row.get('human_review_status') != 'confirmed' and not annotations:
        detections = loads(row.get('detection_result'), [])
        if detections:
            with Image.open(io.BytesIO(backend.storage_client.get(row['original_image_bucket'], row['original_image_object_key']))) as image:
                width, height = image.size
            for detection in detections:
                bbox = detection.get('bbox', [])
                if len(bbox) != 4:
                    continue
                bbox = [float(value) for value in bbox]
                if max(bbox) > 1.0001:
                    bbox = [bbox[0] / width, bbox[1] / height, bbox[2] / width, bbox[3] / height]
                name = detection.get('class_name', '')
                annotations.append({'class_name': name, 'class_id': classes.index(name) if name in classes else -1, 'bbox': bbox})
    return {'project_id': project_id, 'record_id': record_id, 'title': row.get('title'),
            'review_version': int(row.get('review_version') or 0), 'record_status': row.get('record_status'),
            'human_review_status': row.get('human_review_status'), 'class_names': classes, 'annotations': annotations,
            'ai_review': loads(row.get('ai_review_result'), {}),
            'image_url': backend.storage_client.get_file_url(row['original_image_bucket'], row['original_image_object_key'])}


def validate_page_context(actor, session_id, value):
    if not isinstance(value, dict):
        _error('复核页面上下文必须是对象')
    session = sessions.get_session(actor, session_id)
    state = record_state(actor, session['project_id'], value.get('record_id'))
    if value.get('project_id') != session['project_id']:
        _error('页面上下文不属于会话项目', Code.FORBIDDEN)
    result = {key: state[key] for key in ('project_id', 'record_id', 'review_version', 'class_names')}
    result['stale'] = value.get('expected_version') != state['review_version']
    if not result['stale']:
        annotations = value.get('annotations', [])
        if not isinstance(annotations, list) or len(annotations) > 1000:
            _error('复核草稿数量无效')
        result['annotations'] = _validate_annotations(annotations, state['class_names'])
        revision = value.get('draft_revision')
        if isinstance(revision, bool) or not isinstance(revision, int) or not 0 <= revision <= 2**53 - 1:
            _error('页面草稿版本无效')
        result['draft_revision'] = revision
        selected = value.get('selected_index')
        result['selected_index'] = selected if isinstance(selected, int) and not isinstance(selected, bool) and 0 <= selected < len(annotations) else None
        result['view'] = value.get('view') if value.get('view') in ('original', 'result', 'compare') else 'original'
        result['mode'] = value.get('mode') if value.get('mode') in ('select', 'draw', 'pan') else 'select'
        result['zoom'] = min(3, max(.5, float(value.get('zoom', 1))))
    return result


class ReviewTools(ToolProvider):
    def list_tools(self, actor):
        base = {'project_id': {'type': 'string'}, 'record_id': {'type': 'string'}}
        def spec(name, title, properties, required=(), write=False):
            return ToolSpec(name=name, title=title, description=title,
                parameters={'type': 'object', 'properties': {**base, **properties},
                    'required': ['project_id', 'record_id', *required], 'additionalProperties': False},
                **({'risk': RISK_CRITICAL, 'supports_preview': True} if write else {}))
        return [spec(STATE, '读取当前项目真实复核记录、类别、归一化标注和版本；页面草稿需以用户消息附带的页面上下文为准', {}),
            spec(VIEW, '控制复核页视野：定位记录、缩放、适应窗口、选择框、切换平移/画框/选择模式或原图/结果/对照；不提交标注', {
                'command': {'type': 'string', 'enum': ['focus', 'zoom', 'fit', 'mode', 'view', 'select', 'next', 'previous', 'undo']},
                'value': {'type': 'string', 'description': 'zoom 为0.5到3；mode 为pan/select/draw；view 为original/result/compare；select 为从0开始的框编号'},
            }, ['command']),
            spec(WRITE, '按用户指示编辑复核草稿、提交标注、确认无缺陷、驳回、删除或恢复记录。必须先读取当前版本，展示具体改动并由用户点击确认；AI不能自行确认为人工结果。删除只隐藏记录，保留文件和回流来源', {
                'operation': {'type': 'string', 'enum': ['draft', 'confirmed', 'no_defect', 'rejected', 'delete', 'restore']},
                'expected_version': {'type': 'integer'}, 'annotations': {'type': 'array', 'items': {'type': 'object'}},
                'editor_revision': {'type': 'integer', 'description': '编辑页面草稿时必须使用最新页面上下文的draft_revision'},
            }, ['operation', 'expected_version'], True)]

    @project_tool
    def prepare(self, actor, spec, arguments, context):
        _write_access(actor, arguments)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', arguments['record_id']):
            _error('记录ID格式无效，不能生成复核审计路径')
        state = record_state(actor, arguments['project_id'], arguments['record_id'])
        operation = arguments['operation']
        if arguments['expected_version'] != state['review_version']:
            _error('记录已变化，请读取新版本后重新提出改动')
        if state['record_status'] == 'deleted' and operation != 'restore':
            _error('记录已删除，请先恢复')
        annotations = arguments.get('annotations') or []
        if len(annotations) > 1000:
            _error('标注数量超过限制')
        clean = _validate_annotations(annotations, state['class_names'])
        if operation == 'confirmed' and not clean:
            _error('空标注请明确选择确认无缺陷，不能自动推断')
        if operation in ('no_defect', 'rejected', 'delete', 'restore') and clean:
            _error('此操作不能同时提交标注')
        if operation == 'draft' and not isinstance(arguments.get('editor_revision'), int):
            _error('修改草稿需要当前页面的草稿版本')
        preview = {'project_id': arguments['project_id'], 'record_id': state['record_id'], 'title': state['title'],
            'current_version': state['review_version'], 'operation': operation,
            'before': state['annotations'], 'after': clean, 'class_names': state['class_names'],
            'editor_revision': arguments.get('editor_revision'),
            'impact': '只请求更新页面草稿，未提交人工确认' if operation == 'draft' else
                      '隐藏/恢复记录；保留原图、数据集、权重和回流审计，不删除项目文件' if operation in ('delete', 'restore') else
                      '你点击确认后，以你的账号提交此人工决定；AI建议本身不会变成人工确认'}
        digest = hashlib.sha256(dumps({'state': state, 'arguments': arguments}).encode()).hexdigest()
        return {'prepared_id': digest, 'preview': preview}

    @project_tool
    def execute(self, actor, spec, arguments, *, request_key, context, prepared_id=None):
        if context.get('project_id') != arguments['project_id']:
            _error('工具只能操作当前会话项目', Code.FORBIDDEN)
        state = record_state(actor, arguments['project_id'], arguments['record_id'])
        task = tasks.get_task(actor, context['task_id'])
        if task['project_id'] != arguments['project_id']:
            _error('任务与目标项目不一致', Code.FORBIDDEN)
        if spec.name == STATE:
            return OperationResult(outputs=state)
        if spec.name == VIEW:
            command = arguments['command']; value = arguments.get('value', '')
            if command == 'zoom' and not .5 <= float(value) <= 3:
                _error('缩放范围为0.5到3')
            if command == 'mode' and value not in ('pan', 'select', 'draw') or command == 'view' and value not in ('original', 'result', 'compare'):
                _error('复核视图参数无效')
            if command == 'select' and (not value.isdigit() or int(value) >= 1000):
                _error('请选择实际存在的框编号')
            payload = {**arguments, 'task_id': task['id']}
            tasks.append_event(task['session_id'], task['id'], 'review.view', payload)
            return OperationResult(outputs={'view_requested': payload, 'persisted': False})
        _write_access(actor, arguments)
        auth = context.get('authorization') or {}
        action = actions.get_action(actor, auth.get('action_id') or '') if auth.get('confirmed') else None
        if (not action or action['status'] != actions.STATUS_EXECUTING or action['task_id'] != task['id']
                or action['tool_name'] != WRITE or action['parameters'] != arguments or action['request_key'] != request_key
                or action['prepared_id'] != prepared_id):
            _error('必须由用户在本站确认这张具体复核/删除卡片，不能由模型或外部工具自行批准', Code.FORBIDDEN)
        conn = connect()
        try:
            previous = conn.execute('SELECT * FROM agent_review_action WHERE request_key=?', (request_key,)).fetchone()
            if previous:
                if previous['status'] == 'succeeded':
                    return OperationResult(outputs=loads(previous['outputs_json'], {}))
                _error('此前操作结果待核实，不会重复提交人工复核')
            prepared = self.prepare(actor, spec, arguments, context)
            if prepared['prepared_id'] != prepared_id:
                _error('记录或标注已改变，旧确认卡不能执行')
            conn.execute('INSERT INTO agent_review_action VALUES (?,?,?,?,?,?,?,?)',
                         (request_key, actor.user_id, arguments['project_id'], state['record_id'], action['id'], 'started', None, now()))
            conn.commit()
        finally:
            conn.close()
        operation = arguments['operation']; backend = get_backend()
        if operation == 'draft':
            outputs = {'record_id': state['record_id'], 'draft_requested': True, 'human_review_status': state['human_review_status']}
            payload = {**arguments, 'annotations': prepared['preview']['after']}
            tasks.append_event(task['session_id'], task['id'], 'review.draft', payload)
        elif operation in ('delete', 'restore'):
            from services.record_lifecycle import set_deleted
            outputs = set_deleted(backend.database(), state['record_id'], actor.user_id, arguments['expected_version'], deleted=operation == 'delete')
        else:
            result = submit_human_review(state['record_id'], actor.user_id, arguments['expected_version'], prepared['preview']['after'], operation, backend.database())
            outputs = {'record_id': state['record_id'], 'human_review_status': result.status, 'review_version': result.new_version,
                       'annotations': result.annotations, 'review_source': 'user_confirmed_conversation'}
        if operation != 'draft':
            from agent import resources
            resources.bump('detection_records', state['record_id'])
            tasks.append_event(task['session_id'], task['id'], 'review.changed', {**outputs, 'project_id': arguments['project_id']})
        snapshot_path = Path(directory('review/' + state['record_id'])) / (request_key + '.json')
        snapshot_path.write_text(json.dumps({'action_id': action['id'], 'reviewer_id': actor.user_id, 'operation': operation,
            'before': state, 'result': outputs}, ensure_ascii=False, indent=2), encoding='utf-8')
        outputs['audit_file'] = str(snapshot_path)
        conn = connect()
        try:
            conn.execute("UPDATE agent_review_action SET status='succeeded',outputs_json=? WHERE request_key=?", (dumps(outputs), request_key))
            conn.commit()
        finally:
            conn.close()
        return OperationResult(outputs=outputs, business_refs=[{'kind': 'record', 'id': state['record_id'], 'project_id': arguments['project_id']}])

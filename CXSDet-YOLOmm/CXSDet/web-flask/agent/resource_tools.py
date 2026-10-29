"""海巡工作区业务扩展；沿用 OpenCode、真实服务和用户确认卡。"""
import hashlib

from agent import actions, permissions, resources, tasks
from agent.errors import AgentError, Code
from agent.store import connect, query_all, dumps
from agent.tools.contract import ToolProvider, ToolSpec, OperationResult, RISK_READ, RISK_CRITICAL
from agent.tools.business import get_backend
from services import resource_lifecycle as lifecycle

MANAGE = 'workspace_manage_resource'
QUERY = 'workspace_query_resources'
SAMPLES = 'workspace_draft_samples'
DELETE_RECORDS = 'workspace_delete_records'
FEEDBACK_RECORDS = 'workspace_add_feedback'


class ResourceTools(ToolProvider):
    def list_tools(self, actor):
        base = {'project_id': {'type': 'string'}}
        def spec(name, title, props, required=(), write=False):
            return ToolSpec(name=name, title=title, description=title,
                parameters={'type': 'object', 'properties': {**base, **props},
                            'required': ['project_id', *required], 'additionalProperties': False},
                risk=RISK_CRITICAL if write else RISK_READ, supports_preview=write, supports_status=False,
                requires_confirmation=write)
        return [spec(QUERY, '按项目查询真实数据集、模型、草稿或识别记录，可按名称和状态过滤，查询回收站时显式 include_deleted；返回真实ID与当前版本，不猜对象', {
            'kind': {'type': 'string', 'enum': ['dataset', 'model', 'draft', 'detection_record']},
            'search': {'type': 'string'}, 'status': {'type': 'string'}, 'include_deleted': {'type': 'boolean'},
            'page': {'type': 'integer', 'minimum': 1}}, ['kind']),
            spec(MANAGE, '删除或恢复当前项目的数据集、模型、草稿。删除移入回收站且保留文件，不级联删除；在用数据集、训练中或已发布模型会阻止。必须展示具体预览并等用户确认，不能改SQL或删文件替代', {
                'kind': {'type': 'string', 'enum': list(lifecycle.TABLES)}, 'resource_id': {'type': 'string'},
                'operation': {'type': 'string', 'enum': ['delete', 'restore']}, 'expected_revision': {'type': 'integer'}},
                ['kind', 'resource_id', 'operation', 'expected_revision'], True),
            spec(DELETE_RECORDS, '按用户指定范围单条或批量删除识别记录。先用资源查询取得真实ID和review_version，列清目标和数量，一张确认卡；删除仅隐藏记录且保留文件，未选记录不受影响，任一版本冲突整批不执行', {
                'records': {'type': 'array', 'minItems': 1, 'maxItems': 50, 'items': {'type': 'object', 'properties': {
                    'record_id': {'type': 'string'}, 'expected_version': {'type': 'integer'}}, 'required': ['record_id', 'expected_version'], 'additionalProperties': False}}}, ['records'], True),
            spec(FEEDBACK_RECORDS, '将指定记录批量加入各自原训练数据集反馈池，一次确认；先查真实ID与review_version，仅接收人工确认记录，不改原数据集文件、不自动定稿或训练，逐条报告结果', {
                'records': {'type': 'array', 'minItems': 1, 'maxItems': 50, 'items': {'type': 'object', 'properties': {
                    'record_id': {'type': 'string'}, 'expected_version': {'type': 'integer'}}, 'required': ['record_id', 'expected_version'], 'additionalProperties': False}}}, ['records'], True),
            spec(SAMPLES, '读取当前项目草稿的实际样本ID、文件名、配对状态和划分；随后可按用户要求调用 business_draft_assign_splits，不能猜样本ID或划分', {
                'draft_id': {'type': 'string'}, 'page': {'type': 'integer', 'minimum': 1}}, ['draft_id'])]

    def prepare(self, actor, spec, arguments, context):
        if spec.name in (DELETE_RECORDS, FEEDBACK_RECORDS):
            from services.record_lifecycle import delete_preview
            from services.review_service import ReviewConflictError, ReviewValidationError
            try:
                preview = delete_preview(actor, arguments['project_id'], arguments['records'])
                if spec.name == FEEDBACK_RECORDS:
                    for item in preview['records']:
                        row = resources.business_row('detection_records', item['record_id'])
                        if row.get('record_status') == 'deleted' or row.get('human_review_status') != 'confirmed':
                            raise ReviewValidationError('反馈只接收未删除且已人工确认的记录，请重新选择')
                        model = resources.business_row('models', row.get('model_id')) or {}
                        dataset_id = model.get('dataset_id')
                        dataset = permissions.resolve_resource(actor, {'kind': 'dataset', 'id': dataset_id}) if dataset_id else None
                        if not dataset or dataset['project_id'] != arguments['project_id']:
                            raise ReviewValidationError('检测模型缺少当前项目可写的原训练数据集')
                        permissions.require_access(actor, dataset, 'write')
                        item.update(dataset_id=dataset_id, dataset_name=dataset['display_name'])
                    preview['impact'] = '仅加入各检测模型原训练数据集的反馈池；原标签、原数据集文件保留，不自动生成新版本或训练。'
            except (ReviewConflictError, ReviewValidationError) as exc:
                raise AgentError(Code.ACTION_STALE, str(exc), http_status=409) from exc
        else:
            preview = lifecycle.preview(actor, arguments['project_id'], arguments['kind'], arguments['resource_id'], arguments['operation'], task_id=context.get('task_id'))
            if preview['revision'] != arguments['expected_revision']:
                raise AgentError(Code.ACTION_STALE, '资源已改变，请查询新版本', http_status=409)
        return {'prepared_id': hashlib.sha256(dumps(preview).encode()).hexdigest(), 'preview': preview}

    def execute(self, actor, spec, arguments, *, request_key, context, prepared_id=None):
        project_id = arguments['project_id']
        if context.get('project_id') != project_id:
            raise AgentError(Code.FORBIDDEN, '只能操作当前会话项目', http_status=403)
        permissions.get_project(actor, project_id)
        if spec.name == QUERY:
            kind = arguments['kind']; table = resources.SOURCE_TABLE_BY_KIND[kind]
            conn = connect()
            try:
                # 旧索引的 record / dataset_draft 与新名称共享同一来源表，不能按 kind 漏查。
                rows = query_all(conn, 'SELECT * FROM agent_resource WHERE project_id=? AND source_table=? ORDER BY updated_at DESC', (project_id, table))
                result = []
                for row in rows:
                    if row.get('deleted_at') and (not arguments.get('include_deleted') or row['owner_id'] != actor.user_id):
                        continue
                    if not row.get('deleted_at'):
                        permissions.require_access(actor, permissions._resource_view(row), 'read')
                    source = resources.business_row(table, row.get('source_id'))
                    if not source:
                        continue
                    if kind == 'detection_record' and source.get('record_status') == 'deleted' and not arguments.get('include_deleted'):
                        continue
                    status = str(source.get('human_review_status', source.get('workflow_status', source.get('status', ''))))
                    if arguments.get('status') and arguments['status'] not in (status, source.get('record_status')):
                        continue
                    name = source.get('name') or source.get('title') or row['display_name']
                    searchable = ' '.join(str(value or '') for value in (name, row['source_id'], source.get('create_time'), source.get('upload_time'), source.get('description'), source.get('model_name')))
                    if arguments.get('search', '').lower() not in searchable.lower():
                        continue
                    result.append({'id': row['source_id'], 'name': name, 'kind': kind, 'project_id': project_id,
                        'revision': row['revision'], 'status': status, 'deleted_at': row.get('deleted_at'),
                        'review_version': source.get('review_version'), 'dataset_id': source.get('dataset_id'),
                        'publish_status': source.get('publish_status'), 'create_time': source.get('create_time') or source.get('upload_time'),
                        'record_status': source.get('record_status'), 'human_review_status': source.get('human_review_status'),
                        'model_id': source.get('model_id'), 'model_name': source.get('model_name'),
                        'feedback_dataset_id': source.get('feedback_dataset_id'),
                        'is_deleted': bool(row.get('deleted_at') or source.get('record_status') == 'deleted')})
                page = max(1, int(arguments.get('page', 1))); start = (page - 1) * 50
                return OperationResult(outputs={'items': result[start:start+50], 'total': len(result), 'page': page, 'page_size': 50})
            finally:
                conn.close()
        if spec.name == SAMPLES:
            resource = permissions.resolve_resource(actor, {'kind': 'draft', 'id': arguments['draft_id']})
            if resource['project_id'] != project_id:
                raise AgentError(Code.FORBIDDEN, '草稿不属于当前项目', http_status=403)
            from services.dataset_draft_service import list_draft_samples
            data = list_draft_samples(arguments['draft_id'], get_backend().database(), page=arguments.get('page', 1), page_size=50)
            # 不向模型暴露后台私有文件路径，文件使用现有鉴权预览接口。
            data['samples'] = [{key: sample.get(key) for key in ('id', 'sample_key', 'split', 'pairing_state', 'class_names')} for sample in data['samples']]
            return OperationResult(outputs=data)
        authorization = context.get('authorization') or {}
        action = actions.get_action(actor, authorization.get('action_id') or '') if authorization.get('confirmed') else None
        if (not action or action['status'] != actions.STATUS_EXECUTING or action['task_id'] != context.get('task_id')
                or action['tool_name'] != spec.name or action['parameters'] != arguments or action['request_key'] != request_key
                or action['prepared_id'] != prepared_id):
            raise AgentError(Code.FORBIDDEN, '必须由用户确认这张具体业务操作卡片', http_status=403)
        prepared = self.prepare(actor, spec, arguments, context)
        if prepared['prepared_id'] != prepared_id:
            raise AgentError(Code.ACTION_STALE, '资源或依赖已变化，请重新预览', http_status=409)
        if spec.name == FEEDBACK_RECORDS:
            from services.batch_review_service import add_batch_confirmed_records
            outputs = add_batch_confirmed_records([item['record_id'] for item in arguments['records']], actor.user_id, False, get_backend().database())
            outputs['project_id'] = project_id
        elif spec.name == DELETE_RECORDS:
            from services.record_lifecycle import set_deleted_many
            outputs = set_deleted_many(get_backend().database(), arguments['records'], actor.user_id)
            outputs['project_id'] = project_id
        else:
            outputs = lifecycle.change(actor, project_id, arguments['kind'], arguments['resource_id'], arguments['operation'], arguments['expected_revision'],
                request_key=request_key, action_id=action['id'], task_id=context['task_id'])
        task = tasks.get_task(actor, context['task_id'])
        tasks.append_event(task['session_id'], task['id'], 'workspace.resources_changed', outputs)
        if spec.name == DELETE_RECORDS:
            for record in outputs['records']:
                tasks.append_event(task['session_id'], task['id'], 'review.changed', {**record, 'project_id': project_id})
        status = 'partial_failed' if outputs.get('failed') and outputs.get('succeeded') else 'failed' if outputs.get('failed') else 'succeeded'
        return OperationResult(status=status, outputs=outputs)

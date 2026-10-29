"""OpenCode 发起的持久化巡检批任务。只编排已有业务服务，不另建模型循环。

确认一次不可变计划，逐项记账；重启不重发写入/付费调用。人工结果只读业务库。
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import zipfile

from flask import current_app, g
import yaml

from agent import connections, permissions, sessions, tasks
from agent.errors import AgentError, Code
from agent.identity import load_actor
from agent.project_folders import checked_project_root
from agent.store import connect, dumps, loads, new_id, now
from agent.tools.business import get_backend
from agent.tools.contract import ToolProvider, ToolSpec, OperationResult, RISK_READ, RISK_WRITE
from services.project_storage import project_tool, project_run, temp_directory

START = 'workspace_start_inspection_loop'
ROUTE = 'workspace_route_reviewed_samples'
ACTIVE = {'queued', 'running', 'routing', 'queued_route', 'awaiting_training'}
_WORKERS = set()
_LOCK = threading.RLock()


def _error(message):
    raise AgentError(Code.VALIDATION, message, http_status=409)


def _hash(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _path(actor, project_id, value):
    root = checked_project_root(actor, permissions.get_project(actor, project_id))
    if root is None:
        _error('请先为项目绑定本机文件夹，人工上传入口也仍可使用')
    candidate = Path(value)
    try:
        path = (candidate if candidate.is_absolute() else root / candidate).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError()
    except (ValueError, OSError):
        _error('文件必须位于当前项目绑定的目录内')
    return path


@contextmanager
def _scan_source(source):
    from services.authorized_directories import scan_dataset_tree
    from services.dataset_contract import safe_extract_dataset
    base = Path(temp_directory(current_app.config['TEMP_DIR'])); base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='inspection-scan-', dir=base) as scratch:
        root = Path(safe_extract_dataset(str(source), str(Path(scratch) / 'source'))) if source.is_file() else source
        scan = scan_dataset_tree(root)
        yield root, scan


def _manifest(root, scan):
    files = set()
    for sample in scan['samples']:
        files.update(sample['modalities'].values())
        if sample.get('label'):
            files.add(sample['label'])
    for name in ('data.yaml', 'data.yml'):
        if (root / name).is_file():
            files.add(str(root / name))
    result = {}
    for item in sorted(files):
        path = Path(item).resolve(strict=True)
        if not path.is_relative_to(root.resolve()) or path.is_symlink():
            _error('数据集中存在越界文件')
        result[path.relative_to(root).as_posix()] = _hash(path)
    return result


def _yaml(root, plan):
    secondary = plan.get('secondary_modality')
    value = {'path': '.', 'train': 'images/train', 'val': 'images/val',
             'test': 'images/test', 'names': plan['class_names'], 'nc': len(plan['class_names'])}
    if secondary:
        value.update(modalities={'rgb': 'images', secondary: 'images_' + secondary},
                     modality_used=['rgb', secondary])
    (root / 'data.yaml').write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding='utf-8')


def _pack(root, files, target):
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in files:
            archive.write(root / name, name)


def _read(run_id):
    conn = connect()
    try:
        row = conn.execute('SELECT * FROM agent_inspection_run WHERE id=?', (run_id,)).fetchone()
        if row is None:
            _error('巡检批次不存在')
        result = dict(row)
        result['plan'] = loads(result.pop('plan_json'), {})
        result['state'] = loads(result.pop('state_json'), {})
        return result
    finally:
        conn.close()


def get_run(actor, run_id, project_id=None):
    run = _read(run_id)
    sessions.get_session(actor, run['session_id'])
    permissions.get_project(actor, run['project_id'])
    if actor.user_id != run['owner_id'] or (project_id and project_id != run['project_id']):
        raise AgentError(Code.FORBIDDEN, '不能访问其他账号或项目的巡检批次', http_status=403)
    return run


def task_run(task_id):
    conn = connect()
    try:
        row = conn.execute("SELECT id FROM agent_inspection_run WHERE task_id=? AND status<>'prepared' ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
    finally:
        conn.close()
    return _read(row['id']) if row else None


def _save(run, *, status=None):
    if status:
        run['status'] = status
    conn = connect()
    try:
        conn.execute('UPDATE agent_inspection_run SET status=?,state_json=?,updated_at=? WHERE id=?',
                     (run['status'], dumps(run['state']), now(), run['id']))
        conn.commit()
    finally:
        conn.close()


def _event(run, message, **detail):
    tasks.append_event(run['session_id'], run['task_id'], tasks.EVENT_PROGRESS,
                       {'message': message, 'inspection_run_id': run['id'], **detail})


def _message(run, text):
    sessions.append_message(run['session_id'], sessions.ROLE_ASSISTANT, text, task_id=run['task_id'])
    tasks.append_event(run['session_id'], run['task_id'], tasks.EVENT_ASSISTANT_MESSAGE, {'content': text})


def _wait(run, status, reason):
    task = tasks.get_task_by_id(run['task_id'])
    if task['status'] == tasks.STATUS_STOPPED:
        return
    tasks.set_waiting(run['task_id'], kind='human' if status == tasks.STATUS_AWAITING_HUMAN else 'business',
                      reason=reason, refs=[{'kind': 'inspection_run', 'id': run['id']}],
                      page_hint=view(run)['review_path'] if status == tasks.STATUS_AWAITING_HUMAN else None)
    tasks.set_status(run['task_id'], status, summary=reason, force=True)


def _check(run):
    actor = load_actor(run['owner_id'], source='inspection-worker')
    get_run(actor, run['id'])
    if tasks.get_task(actor, run['task_id'])['status'] == tasks.STATUS_STOPPED:
        raise InterruptedError('用户已停止；已完成结果保留，后续步骤未执行')
    return actor


def _call(run, key, tool, arguments):
    actor = _check(run)
    journal = run['state'].setdefault('operations', {})
    backend = get_backend()
    request_key = run['id'] + ':' + key
    previous = journal.get(key)
    if previous:
        # 本进程从不重发未知写入或付费请求；启动恢复也不执行这些记录。
        if previous.get('status') != 'succeeded':
            _error('该步骤曾开始但未取得确定结果，请核实后另起计划，不能自动重做')
        return previous['outputs']
    _event(run, '正在执行：' + tool, stage=key)
    prepared = backend.prepare_tool(actor.user_id, tool, arguments)
    if prepared['preview'].get('blocked'):
        _error('业务检查未通过：' + dumps(prepared['preview'].get('blocked_reasons'))[:600])
    if tool == 'model.publish':
        expected = ((run['state'].get('route_plan') or {}).get('publish_archives', [])
                    if key.endswith('publish-trained') else run['plan'].get('publish_archives', []))
        if sorted(prepared['preview'].get('archived_model_ids') or []) != sorted(expected):
            _error('实际发布归档范围与用户确认的计划不一致，已停止发布')
    journal[key] = {'status': 'started', 'tool': tool, 'operation_id': prepared['operation_id']}
    _save(run)
    result = backend.execute_tool(actor.user_id, prepared['operation_id'], request_key)
    journal[key] = {'status': result['status'], 'tool': tool, 'operation_id': result['operation_id'],
                    'outputs': result.get('outputs') or {}}
    _save(run)
    if result['status'] != 'succeeded':
        _error('业务操作尚未成功：' + result['status'])
    from agent.resources import register_refs
    stats = register_refs(actor.user_id, result.get('business_refs') or [],
                          project_id=run['project_id'], state_changed=tool != 'model.compare')
    if stats['failed']:
        _error('业务已成功但资源归属登记待核实；不会重新执行业务，请检查登记失败记录')
    return result['outputs']


def _attach(run, key, source):
    actor = _check(run)
    from agent.project_tools import register_local_asset
    from routes.file import _validate_project_file
    assets = run['state'].setdefault('assets', {})
    if key in assets:
        return assets[key]
    source = Path(source)
    kind, mime = _validate_project_file(source, source.suffix.lower())
    digest = _hash(source)
    # 登记本身没有外部付费；仍先记开始，崩溃后不以新键自动重做。
    markers = run['state'].setdefault('attachment_started', [])
    if key in markers:
        _error('附件登记结果待核实，不会自动重复登记')
    markers.append(key); _save(run)
    output = register_local_asset(actor, run['project_id'], source, kind, mime, digest).outputs
    assets[key] = output['asset_id']; _save(run)
    return output['asset_id']


def _review_queue(run, *, all_records=False):
    items = run['state'].get('items') or []
    # AI 一致不等于人工确认，也不强迫用户逐张确认没有疑点的图片。
    return [item['record_id'] for item in sorted(items, key=lambda item: item.get('ai_status') == 'consistent')
            if item.get('record_id') and (all_records or item.get('ai_status') != 'consistent' or item.get('error'))
            and get_backend()._read_record(item['record_id']).get('record_status') != 'deleted']


def view(run):
    value = {key: run[key] for key in ('id', 'project_id', 'session_id', 'task_id', 'status', 'plan', 'state')}
    value['plan'] = {key: item for key, item in run['plan'].items() if key != 'source_manifest'}
    value['state'] = {key: item for key, item in run['state'].items() if key not in ('operations', 'assets', 'attachment_started', 'route_preview')}
    value['record_ids'] = _review_queue(run)
    value['all_record_ids'] = _review_queue(run, all_records=True)
    value['review_path'] = '/projects/' + run['project_id'] + '/review?flow_id=' + run['id']
    return value


def _existing_inputs(actor, project_id, manifest, weight_digest):
    """同源再次巡检复用真实已登记资源，不重复创建数据集/模型。"""
    conn = connect()
    try:
        rows = conn.execute("SELECT plan_json,state_json FROM agent_inspection_run WHERE owner_id=? AND project_id=? AND status<>'prepared' ORDER BY created_at DESC LIMIT 100",
                            (actor.user_id, project_id)).fetchall()
    finally:
        conn.close()
    backend = get_backend()
    for row in rows:
        plan, state = loads(row['plan_json'], {}), loads(row['state_json'], {})
        if plan.get('source_manifest') != manifest or plan.get('weight_sha256') != weight_digest or not state.get('model_id') or not state.get('dataset_id'):
            continue
        try:
            model = backend.resolve(actor.user_id, 'model', state['model_id'], ('model:read',))
            dataset = backend.resolve(actor.user_id, 'dataset', state['dataset_id'], ('dataset:read',))
            meta = backend._model_row(model.id)
            if model.project_id != project_id or dataset.project_id != project_id or meta['status'] not in (1, 3):
                continue
            if backend._dataset_row(dataset.id)['status'] != 1:
                continue
            payload = backend.storage_client.get(meta['model_bucket'], meta['model_object_key'])
            if hashlib.sha256(payload).hexdigest() != weight_digest:
                continue
            return {'model_id': model.id, 'dataset_id': dataset.id, 'status': meta['status'],
                    'model_version': model.version, 'dataset_version': dataset.version}
        except (AgentError, PermissionError, FileNotFoundError, ValueError):
            continue
    return None


class InspectionLoopTools(ToolProvider):
    def list_tools(self, actor):
        def spec(name, title, props, required=(), write=False):
            return ToolSpec(name=name, title=title, description=title,
                parameters={'type': 'object', 'properties': {'project_id': {'type': 'string'}, **props},
                            'required': ['project_id', *required], 'additionalProperties': False},
                risk=RISK_WRITE if write else RISK_READ, billable=name == START,
                supports_preview=write)
        result = [spec('workspace_inspection_state', '读取本轮巡检闭环的真实状态、复核进度、记录 ID 与人工页面；不是重新执行',
                       {'run_id': {'type': 'string'}}, ['run_id'])]
        if not actor.is_admin:
            return result
        result += [spec('workspace_discover_inputs', '只读查找当前项目文件夹内的数据集目录、ZIP 与 PT 权重；不会逐张询问授权', {}),
            spec(START, '自动图片巡检闭环：一次确认完整计划后，自动登记文件、导入数据集与权重、发布本轮自用模型、批量检测、逐图 AI 看图核查，再打开人工复核。不要拆成逐个附件确认。必须先得到用户要求的范围和置信度。', {
                'dataset_path': {'type': 'string'}, 'weight_path': {'type': 'string'},
                'model_id': {'type': 'string', 'description': '可选，使用本项目已登记的新模型时提供真实模型ID，替代 weight_path；其关联数据集作为反馈源'},
                'confidence': {'type': 'number'}, 'sample_limit': {'type': 'integer'},
                'sample_split': {'type': 'string', 'enum': ['train', 'val', 'test', 'all']},
                'secondary_modality': {'type': 'string'},
                'class_names': {'type': 'array', 'items': {'type': 'string'}}},
                ['dataset_path', 'confidence', 'sample_limit'], True),
            spec(ROUTE, '人工复核后，按用户指定的记录分组回流：original 保留原数据集并生成修订版本；new 创建独立新数据集，须提供每条记录的 train/val/test 划分。一次确认回流计划后自动执行。不能替用户提交人工标注。', {
                'run_id': {'type': 'string'},
                'destinations': {'type': 'array', 'items': {'type': 'object'}, 'description':
                    '每组 {record_ids:[ID],destination:"original"或"new",name:"新版本名称",splits:{recordID:"train/val/test"}}；可选 training:{epochs:数字,batch_size:数字,publish:布尔值}。省略 training 不训练。'},
            }, ['run_id', 'destinations'], True)]
        return result

    @project_tool
    def prepare(self, actor, spec, arguments, context):
        project_id = arguments['project_id']
        task = tasks.get_task(actor, context['task_id'])
        if project_id != context['project_id'] or task['project_id'] != project_id:
            _error('本轮只能操作当前会话项目')
        if spec.name == ROUTE:
            run = get_run(actor, arguments['run_id'], project_id)
            route_plan = _routing_plan(actor, run, arguments['destinations'])
            fingerprint = hashlib.sha256(dumps(route_plan).encode()).hexdigest()
            run['state']['route_preview'] = {'fingerprint': fingerprint, 'plan': route_plan,
                'task_id': task['id'], 'session_id': task['session_id']}
            _save(run)
            return {'prepared_id': fingerprint, 'preview': route_plan}
        source = _path(actor, project_id, arguments['dataset_path'])
        explicit_model = arguments.get('model_id')
        if bool(explicit_model) == bool(arguments.get('weight_path')):
            _error('请提供 weight_path 或已登记的 model_id，二选一')
        weight = _path(actor, project_id, arguments['weight_path']) if not explicit_model else None
        reuse = None
        model_classes = []
        if explicit_model:
            backend = get_backend()
            registered = backend.resolve(actor.user_id, 'model', explicit_model, ('model:read',))
            meta = backend._model_row(registered.id)
            dataset = backend.resolve(actor.user_id, 'dataset', meta['dataset_id'], ('dataset:read',))
            if registered.project_id != project_id or dataset.project_id != project_id or meta['status'] not in (1, 3):
                _error('须选择当前项目的已完成模型；不能修改默认或其他项目资源')
            from utils.dataset_classes import dataset_class_names
            model_classes = dataset_class_names(backend._dataset_row(dataset.id).get('structure_info'))
            weight_digest = hashlib.sha256(backend.storage_client.get(meta['model_bucket'], meta['model_object_key'])).hexdigest()
            reuse = {'model_id': registered.id, 'dataset_id': dataset.id, 'status': meta['status'],
                     'model_version': registered.version, 'dataset_version': dataset.version}
        if (source.is_file() and source.suffix.lower() != '.zip') or (weight is not None and (not weight.is_file() or weight.suffix.lower() != '.pt')):
            _error('数据源须为数据集目录或 ZIP，权重须为 PT 文件')
        count = arguments['sample_limit']; confidence = arguments['confidence']
        if isinstance(count, bool) or not 1 <= count <= 5000 or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            _error('图片数量须在 1–5000 之间，置信度须在 0–1 之间')
        with _scan_source(source) as (root, scan):
            names = scan['class_names']
            if scan.get('class_names_derived'):
                names = arguments.get('class_names') or model_classes
            if not names or len(set(names)) != len(names):
                _error('缺少真实类别表，请提供与权重类别索引一致的 class_names')
            if model_classes and names != model_classes:
                _error('图片数据的类别索引表与所选模型关联数据集不一致，请明确核对映射')
            blockers = [issue for issue in scan['issues'] if issue['severity'] == 'blocking' and not (issue['code'] == 'CLASS_NAMES_MISSING' and names)]
            if blockers:
                _error('数据集检查失败：' + dumps(blockers[:5]))
            modalities = set().union(*(set(sample['modalities']) for sample in scan['samples']))
            secondary = arguments.get('secondary_modality') or next(iter(sorted(modalities - {'rgb'})), None)
            if modalities - {'rgb', secondary}:
                _error('当前闭环仅支持 RGB 加一种配套模态，请明确选择数据源')
            split = arguments.get('sample_split', 'all')
            selected = sorted((sample for sample in scan['samples'] if split == 'all' or sample['split'] == split), key=lambda item: item['key'])[:count]
            if len(selected) != count:
                _error('指定范围的图片不足，不能擅自缩小本轮计划')
            for sample in selected:
                if 'rgb' not in sample['modalities'] or (secondary and secondary not in sample['modalities']):
                    _error('所选图片缺少 RGB 或真实配套模态')
            samples = [{'key': sample['key'], 'split': sample['split'],
                        'modalities': {name: Path(path).relative_to(root).as_posix() for name, path in sample['modalities'].items()},
                        'label': Path(sample['label']).relative_to(root).as_posix() if sample.get('label') else None}
                       for sample in selected]
            manifest = _manifest(root, scan)
        if not explicit_model:
            weight_digest = _hash(weight)
            reuse = _existing_inputs(actor, project_id, manifest, weight_digest)
        resources = permissions.list_project_resources(actor, project_id)
        backend = get_backend()
        published = []
        for resource in resources:
            if resource.get('source_table') == 'models' and resource.get('source_id'):
                row = backend._model_row(resource['source_id'])
                if row.get('status') == 3 and (row.get('model_mode') or 'single') == ('multimodal' if secondary else 'single') and (row.get('secondary_modality') or None) == secondary:
                    published.append({'id': row['id'], 'name': row['name']})
        if reuse and reuse['status'] == 3:
            published = []
        run_id = new_id('loop')
        plan = {'project_id': project_id, 'dataset_path': str(source), 'weight_path': str(weight) if weight else None,
                'dataset_sha256': _hash(source) if source.is_file() else None, 'weight_sha256': weight_digest,
                'reuse': reuse, 'publish_archives': [item['id'] for item in published], 'publish_archive_models': published,
                'source_manifest': manifest, 'class_names': names,
                'class_mapping': [{'index': index, 'dataset_name': name, 'rule': '按权重类别索引对应；不改变权重参数'} for index, name in enumerate(names)],
                'dataset_mode': 'multimodal' if secondary else 'single', 'secondary_modality': secondary,
                'confidence': confidence, 'sample_count': count, 'ai_review_count': count, 'samples': samples,
                'dataset_name': source.stem + '-巡检-' + run_id[-8:], 'model_name': (weight.stem if weight else explicit_model) + '-巡检-' + run_id[-8:],
                'steps': ['登记并导入数据集', '导入所选权重并发布本项目自用模型', '批量检测指定图片',
                          '用本人连接逐图核查真实 RGB/配套图', '打开人工复核，等待用户提交', '等待用户指定回流去向'],
                'impact': '复制原文件，不改源数据。一次授权限于上述确切样本、置信度及 AI 调用数量；不训练、不删除，不提交人工标注。',
                'connection': {key: value for key, value in (connections.get_connection_view(actor) or {}).items()
                               if key in ('provider', 'api_url', 'model', 'key_fingerprint')},
                'capabilities': connections.capabilities(actor)}
        conn = connect()
        try:
            conn.execute('INSERT INTO agent_inspection_run(id,owner_id,project_id,session_id,task_id,status,plan_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                          (run_id, actor.user_id, project_id, task['session_id'], task['id'], 'prepared', dumps(plan), now(), now()))
            conn.commit()
        finally:
            conn.close()
        # 大目录的完整哈希清单留在服务器，卡片只列实际选择和数量。
        preview = {key: value for key, value in plan.items() if key != 'source_manifest'}
        return {'prepared_id': run_id, 'preview': {'run_id': run_id, **preview}}

    def execute(self, actor, spec, arguments, *, request_key, context, prepared_id=None):
        project_id = arguments['project_id']
        if project_id != context['project_id']:
            _error('工具只能作用于当前会话项目')
        permissions.get_project(actor, project_id)
        if spec.name == 'workspace_discover_inputs':
            root = checked_project_root(actor, permissions.get_project(actor, project_id))
            if root is None:
                _error('此项目尚未绑定本机文件夹')
            found = {'dataset_directories': [], 'archives': [], 'weights': []}
            seen = 0
            for folder, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [name for name in dirs if name not in ('.git', 'node_modules', '.venv', '__pycache__', '.cache') and not name.startswith('.dataset-export-')
                           and not (Path(folder) / name).is_symlink() and (Path(folder) / name).resolve().is_relative_to(root)]
                seen += len(files)
                if seen > 100000:
                    _error('项目文件过多，请缩小绑定目录')
                if ('data.yaml' in files or 'data.yml' in files) and 'images' in dirs:
                    found['dataset_directories'].append(str(Path(folder)))
                for name in files:
                    path = Path(folder) / name
                    if path.is_symlink() or not path.resolve().is_relative_to(root):
                        continue
                    if path.suffix.lower() in ('.pt', '.zip'):
                        found['weights' if path.suffix.lower() == '.pt' else 'archives'].append(str(path))
            from services.authorized_directories import scan_dataset_tree
            summaries = []
            for directory in found['dataset_directories'][:20]:
                report = scan_dataset_tree(directory)
                summaries.append({'path': directory, 'sample_count': report['sample_count'],
                    'class_names': report['class_names'], 'splits': report['splits'],
                    'modalities': sorted(set().union(*(set(sample['modalities']) for sample in report['samples']))),
                    'blocking_issues': [item for item in report['issues'] if item['severity'] == 'blocking'][:10]})
            return OperationResult(outputs={'project_id': project_id, 'root': str(root), **found, 'dataset_summaries': summaries,
                'next': '请从真实候选选择一份数据集和一份权重，确认置信度和数量，然后调用 workspace_start_inspection_loop 一次生成整轮确认卡'})
        if spec.name == 'workspace_inspection_state':
            run = get_run(actor, arguments['run_id'], project_id)
            reconcile(actor, run['task_id'])
            return OperationResult(outputs=view(get_run(actor, run['id'], project_id)))
        if not (context.get('authorization') or {}).get('confirmed'):
            _error('须由用户确认完整计划；模型自报同意不能代替确认')
        if spec.name == ROUTE:
            run = get_run(actor, arguments['run_id'], project_id)
            saved = run['state'].get('route_preview') or {}
            current = _routing_plan(actor, run, arguments['destinations'])
            if prepared_id != saved.get('fingerprint') or hashlib.sha256(dumps(current).encode()).hexdigest() != prepared_id:
                _error('人工标注或回流计划已变化，请重新预览')
            conn = connect()
            try:
                changed = conn.execute("UPDATE agent_inspection_run SET status='queued_route',task_id=?,session_id=?,state_json=?,updated_at=? WHERE id=? AND status IN ('awaiting_human','awaiting_destination','partial_failed','completed')",
                    (context['task_id'], tasks.get_task_by_id(context['task_id'])['session_id'], dumps({**run['state'], 'route_plan': current}), now(), run['id'])).rowcount
                conn.commit()
            finally:
                conn.close()
            if not changed:
                _error('本轮回流已经提交或仍在处理，不能重复执行')
        else:
            run = get_run(actor, prepared_id, project_id)
            if run['task_id'] != context['task_id']:
                _error('确认卡不属于此任务')
            for name in ('dataset_path', 'weight_path', 'confidence', 'sample_limit'):
                expected = run['plan']['sample_count' if name == 'sample_limit' else name]
                actual = str(_path(actor, project_id, arguments[name])) if name.endswith('_path') and arguments.get(name) else arguments.get(name)
                if actual != expected:
                    _error('确认卡参数已改变')
            connections.require_capability(actor, 'vision')
            conn = connect()
            try:
                conn.execute("UPDATE agent_inspection_run SET status='queued',request_key=?,updated_at=? WHERE id=? AND status='prepared'", (request_key, now(), run['id']))
                conn.commit()
            finally:
                conn.close()
        return OperationResult(status='running', business_refs=[{'kind': 'inspection_run', 'id': run['id']}],
                               outputs={'run_id': run['id'], 'message': '本轮计划已确认并提交；后台按真实结果推进，尚未完成'})


def start_worker(app, run_id):
    with _LOCK:
        worker_key = (app.config['DATABASE'], run_id)
        if worker_key in _WORKERS:
            return
        _WORKERS.add(worker_key)
    def work():
        with app.app_context():
            run = _read(run_id)
            g.user_id = run['owner_id']
            try:
                if run['status'] == 'queued':
                    _detect(run)
                elif run['status'] == 'queued_route':
                    _route(run)
            except InterruptedError as exc:
                run['state']['error'] = str(exc); _save(run, status='stopped')
            except Exception as exc:
                from agent.guard import redact
                run['state']['error'] = redact(str(exc))[:1200]
                _save(run, status='partial_failed')
                if tasks.get_task_by_id(run['task_id'])['status'] != tasks.STATUS_STOPPED:
                    tasks.set_status(run['task_id'], tasks.STATUS_PARTIAL_FAILED, summary='闭环中断：' + run['state']['error'][:220], force=True)
                    _message(run, '本轮已暂停，已完成结果保留，不自动重复付费或写入。原因：' + run['state']['error'])
            finally:
                with _LOCK:
                    _WORKERS.discard(worker_key)
    threading.Thread(target=work, daemon=True, name='haixun-inspection-loop').start()


@project_run
def _detect(run):
    _save(run, status='running')
    actor = _check(run); plan = run['plan']
    connections.require_capability(actor, 'vision')
    current = connections.get_connection_view(actor) or {}
    if any(current.get(key) != value for key, value in plan['connection'].items()):
        _error('模型连接已改变，需要重新确认 AI 核查使用的连接')
    source = _path(actor, run['project_id'], plan['dataset_path'])
    weight = _path(actor, run['project_id'], plan['weight_path']) if plan['weight_path'] else None
    if (weight and _hash(weight) != plan['weight_sha256']) or (source.is_file() and _hash(source) != plan['dataset_sha256']):
        _error('确认后的数据包或权重已改变，请重新预览')
    base = Path(temp_directory(current_app.config['TEMP_DIR']))
    with _scan_source(source) as (root, scan), tempfile.TemporaryDirectory(prefix='inspection-import-', dir=base) as scratch:
        if _manifest(root, scan) != plan['source_manifest']:
            _error('数据集文件已改变，不能执行旧计划')
        archive = Path(scratch) / 'dataset.zip'
        if not plan.get('reuse'):
            _pack(root, plan['source_manifest'], archive)
        reuse = plan.get('reuse')
        if reuse:
            backend = get_backend()
            live_model = backend.resolve(actor.user_id, 'model', reuse['model_id'], ('model:read',))
            live_dataset = backend.resolve(actor.user_id, 'dataset', reuse['dataset_id'], ('dataset:read',))
            if live_model.version != reuse['model_version'] or live_dataset.version != reuse['dataset_version']:
                _error('准备复用的模型或数据集已变化，请重新确认')
            meta = backend._model_row(live_model.id)
            if hashlib.sha256(backend.storage_client.get(meta['model_bucket'], meta['model_object_key'])).hexdigest() != plan['weight_sha256']:
                _error('已登记权重文件已改变，请重新确认')
            dataset = {'dataset_id': reuse['dataset_id']}; model = {'model_id': reuse['model_id']}
            _event(run, '复用已核实的同源数据集与权重，不重复导入')
        else:
            asset = _attach(run, 'dataset', archive)
            dataset = _call(run, 'import-dataset', 'dataset.import', {'asset_id': asset, 'name': plan['dataset_name'],
                'dataset_mode': plan['dataset_mode'], 'class_names': plan['class_names'],
                **({'secondary_modality': plan['secondary_modality']} if plan['secondary_modality'] else {})})
            weight_asset = _attach(run, 'weight', weight)
            model = _call(run, 'import-weight', 'model.import_weight', {'asset_id': weight_asset, 'name': plan['model_name'], 'dataset_id': dataset['dataset_id']})
        run['state']['dataset_id'] = dataset['dataset_id']; _save(run)
        run['state']['model_id'] = model['model_id']; _save(run)
        # 新导入模型的发布范围也再次核实，确认时未列出的归档一律拒绝。
        if not reuse or reuse['status'] != 3:
            _call(run, 'publish-model', 'model.publish', {'model_id': model['model_id']})
        run['state']['items'] = []; _save(run)
        for index, sample in enumerate(plan['samples']):
            _check(run)
            item = {'key': sample['key'], 'split': sample['split']}
            run['state']['items'].append(item); _save(run)
            try:
                rgb = _attach(run, f'rgb-{index}', root / sample['modalities']['rgb'])
                arguments = {'asset_id': rgb, 'model_id': model['model_id'], 'confidence': plan['confidence'], 'output_project_id': run['project_id']}
                if plan['secondary_modality']:
                    arguments['secondary_asset_id'] = _attach(run, f'x-{index}', root / sample['modalities'][plan['secondary_modality']])
                result = _call(run, f'detect-{index}', 'detection.run', arguments)
                item['record_id'] = result['record_id']; _save(run)
                ai = _call(run, f'ai-{index}', 'review.ai', {'record_ids': [result['record_id']]})
                item['ai_result'] = ai
                row = get_backend()._read_record(result['record_id'])
                item['ai_status'] = row.get('ai_review_status')
                if ai.get('failed'):
                    item['error'] = 'AI 核查失败，请人工复核；不会自动重试计费请求'
            except InterruptedError:
                raise
            except Exception as exc:
                from agent.guard import redact
                item['error'] = redact(str(exc))[:600]
            _save(run)
            _event(run, f'本轮已处理 {index + 1}/{plan["sample_count"]} 张', item=item)
    if not _review_queue(run, all_records=True):
        _error('没有生成可供人工复核的检测记录，请检查失败清单')
    if not _review_queue(run):
        failed = sum(bool(item.get('error')) for item in run['state']['items'])
        _save(run, status='partial_failed' if failed else 'completed')
        tasks.clear_waiting(run['task_id'])
        tasks.set_status(run['task_id'], tasks.STATUS_PARTIAL_FAILED if failed else tasks.STATUS_COMPLETED, summary=f'已完成项 AI 核查未报告疑点；失败 {failed} 项，没有自动回流或确认标注', force=True)
        _message(run, f'已完成项的 AI 核查未报告疑点；另有 {failed} 项失败，见逐项记录。检测记录已保留，未伪造人工确认，也未自动回流；如需回流，请先到识别记录页面人工复核指定样本。')
        return
    _save(run, status='awaiting_human')
    reason = '检测与 AI 核查阶段已结束，请人工检查本轮记录；AI 一致也不会自动记为人工确认'
    _wait(run, tasks.STATUS_AWAITING_HUMAN, reason)
    navigation = {'project_id': run['project_id'], 'path': view(run)['review_path'], 'title': '本轮人工复核'}
    tasks.append_event(run['session_id'], run['task_id'], 'workspace.navigate', navigation)
    _message(run, reason + '。提交后会自动核实进度；你可以在对话中指定哪些样本回原数据集、哪些另建数据集。')


def _routing_plan(actor, run, groups):
    if run['status'] not in ('awaiting_human', 'awaiting_destination', 'partial_failed', 'completed'):
        _error('本轮尚未进入人工复核/回流阶段，或已经回流')
    known = {item['record_id']: item for item in run['state'].get('items', []) if item.get('record_id')}
    if not isinstance(groups, list) or not groups:
        _error('请指定至少一组回流记录和去向')
    seen = set(run['state'].get('routed_records') or []); result = []
    for group in groups:
        if not isinstance(group, dict) or set(group) - {'record_ids', 'destination', 'name', 'splits', 'training'}:
            _error('回流参数格式不正确')
        ids = group.get('record_ids')
        name = str(group.get('name') or '').strip()
        destination = group.get('destination')
        if destination not in ('original', 'new') or not name or len(name) > 100 or not isinstance(ids, list) or not ids:
            _error('每组须有记录、original/new 去向和新版本名称')
        versions = {}; splits = group.get('splits') or {}
        for record_id in ids:
            if record_id not in known or record_id in seen:
                _error('记录不属于本轮，或被重复分配')
            seen.add(record_id)
            resource = permissions.resolve_resource(actor, {'source_table': 'detection_records', 'source_id': record_id})
            if resource['project_id'] != run['project_id']:
                _error('记录不属于本项目')
            row = get_backend()._read_record(record_id)
            if row.get('record_status') == 'deleted':
                _error('已删除记录不能回流，请先恢复')
            if row.get('human_review_status') != 'confirmed':
                _error('必须先在人工复核页面确认或修正标注；聊天中的“已完成”不能代替提交')
            versions[record_id] = {'version': row.get('review_version'),
                'annotations_hash': hashlib.sha256(str(row.get('corrected_annotations')).encode()).hexdigest()}
            if destination == 'new' and splits.get(record_id) not in ('train', 'val', 'test'):
                _error('新建数据集须为每条记录指定 train/val/test；不能由模型猜测用户的划分决定')
            if destination == 'original' and known[record_id].get('split') not in ('train', 'val', 'test') and splits.get(record_id) not in ('train', 'val', 'test'):
                _error('现场新增样本尚未划分，请为回原数据集的记录明确 train/val/test')
        if destination == 'new' and (set(splits) != set(ids) or not {'train', 'val'}.issubset(set(splits.values()))):
            _error('可训练的新数据集至少需要独立的 train 和 val 样本；样本不足可先保留在反馈池')
        if any(existing['name'] == name for existing in result) or get_backend().database().query('SELECT id FROM datasets WHERE name=?', (name,), fetchone=True):
            _error('回流数据集名称已经存在，请选择新名称')
        training = group.get('training')
        if training is not None:
            if len(groups) != 1 or not isinstance(training, dict) or set(training) != {'epochs', 'batch_size', 'publish'}:
                _error('自动训练一次只接受一组数据集，须明确 epochs、batch_size 和 publish')
            if any(isinstance(training[k], bool) or not isinstance(training[k], int) or training[k] <= 0 for k in ('epochs', 'batch_size')) or not isinstance(training['publish'], bool):
                _error('训练参数无效')
        result.append({'record_ids': ids, 'destination': destination, 'name': name, 'splits': splits,
                       'review_versions': versions, **({'training': training} if training else {})})
    publish_archives = []
    if any(group.get('training', {}).get('publish') for group in result):
        backend = get_backend()
        scope = backend._authorised_model_scope(actor.user_id, run['state']['model_id'])
        parent = backend._model_row(run['state']['model_id'])
        for model_id in scope:
            row = backend._model_row(model_id)
            if row['status'] == 3 and all(row.get(key) == parent.get(key) for key in ('model_mode', 'primary_modality', 'secondary_modality')):
                publish_archives.append(model_id)
    root = checked_project_root(actor, permissions.get_project(actor, run['project_id']))
    if root is None:
        _error('请先绑定项目目录，生成的数据集须保存在工作区')
    return {'run_id': run['id'], 'project_id': run['project_id'], 'destinations': result, 'publish_archives': sorted(publish_archives),
        'output_directory': str(root / 'datasets'),
        'impact': '仅采用页面真实人工确认的标注。原数据集保持不变，回原数据集表示创建其修订版本并替换本轮同源样本的标签；另建表示仅含选中样本。新版本的 ZIP、解压目录和来源清单保存到工作区 datasets/<数据集ID>，不覆盖已有文件。未选中记录留待处理。'}


@project_run
def _route(run):
    actor = _check(run)
    route_plan = run['state']['route_plan']
    # 执行时重新核对所有人工标注版本，不能执行陈旧卡片。
    old_status = run['status']; run['status'] = 'awaiting_destination'
    groups = route_plan['destinations']
    raw = [{key: value for key, value in group.items() if key != 'review_versions'} for group in groups]
    if _routing_plan(actor, run, raw) != route_plan:
        _error('人工标注在确认后发生变化，请重新确认回流')
    run['status'] = old_status; _save(run, status='routing')
    backend = get_backend()
    run['state'].setdefault('datasets', [])
    routing_number = len(run['state'].get('route_history') or [])
    prefix = f'route-{routing_number}-'
    from services.feedback_dataset_service import _yolo_label
    from services.dataset_transform_service import _dataset_scan
    resource = backend.resolve(actor.user_id, 'dataset', run['state']['dataset_id'], ('dataset:read',))
    scan, source = _dataset_scan(backend, resource)
    # _dataset_scan 的工作目录由其服务创建；TemporaryDirectory 的新副本独立清理。
    import shutil
    try:
        source_root = Path(scan['root'])
        from services.dataset_identity_service import sample_index, match_image, image_identity
        source_images = sample_index(scan)
        for index, group in enumerate(groups):
            _check(run)
            with tempfile.TemporaryDirectory(prefix='inspection-feedback-', dir=temp_directory(current_app.config['TEMP_DIR'])) as scratch:
                root = Path(scratch) / 'dataset'; root.mkdir()
                if group['destination'] == 'original':
                    for relative in _manifest(source_root, scan):
                        target = root / relative; target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source_root / relative, target)
                for record_id in group['record_ids']:
                    item = next(item for item in run['state']['items'] if item.get('record_id') == record_id)
                    sample = next(sample for sample in run['plan']['samples'] if sample['key'] == item['key'])
                    row = backend._read_record(record_id)
                    annotations = loads(row.get('corrected_annotations'), [])
                    from services.review_service import _validate_annotations
                    _validate_annotations(annotations, run['plan']['class_names'])
                    if group['destination'] == 'original':
                        payload = backend.storage_client.get(row['original_image_bucket'], row['original_image_object_key'])
                        identity = loads(row.get('input_identity_json'), {})
                        match = match_image(payload, source_images, (identity.get('rgb') or {}).get('original_name', ''))
                        if match['classification'] == 'ambiguous':
                            _error('原样本来源不唯一或只有同名线索，请先核对，不能自动覆盖或新增')
                        matching = match['candidates']
                        if matching:
                            secondary = run['plan']['secondary_modality']
                            if secondary:
                                paired = matching[0]['modalities'].get(secondary)
                                pair_bytes = backend.storage_client.get(row['secondary_image_bucket'], row['secondary_image_object_key'])
                                if not paired or match_image(pair_bytes, [{'identity': image_identity(Path(paired).read_bytes(), legacy=True)}])['classification'] != 'existing':
                                    _error('配套模态与原样本不一致，不能自动覆盖标注')
                            relative = Path(matching[0]['label']).relative_to(source_root) if matching[0].get('label') else Path('labels') / (matching[0]['key'] + '.txt')
                            label = root / relative
                        else:
                            split = group['splits'].get(record_id) or sample['split']
                            for directory, bucket, object_key in [('images', row['original_image_bucket'], row['original_image_object_key']),
                                *([('images_' + run['plan']['secondary_modality'], row['secondary_image_bucket'], row['secondary_image_object_key'])] if run['plan']['secondary_modality'] else [])]:
                                if not bucket or not object_key:
                                    _error('反馈样本缺少真实配套图片')
                                target = root / directory / split / (record_id + Path(object_key).suffix)
                                target.parent.mkdir(parents=True, exist_ok=True)
                                target.write_bytes(backend.storage_client.get(bucket, object_key))
                            label = root / 'labels' / split / (record_id + '.txt')
                        _call(run, prefix + 'feedback-' + record_id, 'feedback.add_confirmed', {'record_id': record_id, 'dataset_id': run['state']['dataset_id']})
                    else:
                        split = group['splits'][record_id]
                        for directory, bucket, key in [('images', row['original_image_bucket'], row['original_image_object_key']),
                            *([('images_' + run['plan']['secondary_modality'], row['secondary_image_bucket'], row['secondary_image_object_key'])] if run['plan']['secondary_modality'] else [])]:
                            if not bucket or not key:
                                _error('缺少真实配套图片，不能生成反馈数据集')
                            target = root / directory / split / (record_id + Path(key).suffix)
                            target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(backend.storage_client.get(bucket, key))
                        label = root / 'labels' / split / (record_id + '.txt')
                    label.parent.mkdir(parents=True, exist_ok=True)
                    label.write_text(_yolo_label(annotations), encoding='utf-8')
                _yaml(root, run['plan'])
                archive = Path(scratch) / 'feedback.zip'
                _pack(root, [path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_file()], archive)
                asset_id = _attach(run, prefix + f'feedback-archive-{index}', archive)
                result = _call(run, prefix + f'feedback-dataset-{index}', 'dataset.import', {'asset_id': asset_id, 'name': group['name'],
                    'description': '人工复核回流；源数据集 ' + run['state']['dataset_id'] + '；巡检批次 ' + run['id'],
                    'dataset_mode': run['plan']['dataset_mode'], 'class_names': run['plan']['class_names'],
                    **({'secondary_modality': run['plan']['secondary_modality']} if run['plan']['secondary_modality'] else {})})
                run['state']['datasets'].append(result); _save(run)
                from services.project_dataset_export import export_dataset_to_project
                try:
                    result['workspace_export'] = export_dataset_to_project(backend, actor, run['project_id'], result['dataset_id'])
                except (AgentError, OSError, ValueError) as exc:
                    result['workspace_export'] = {'status': 'failed', 'error': str(exc)[:500]}
                    _save(run)
                    _error('数据集已登记，但工作区导出失败；已保留数据集 ID 和错误，不会重复导入或启动训练')
                _save(run)
                _event(run, '新数据集已保存到工作区', dataset_id=result['dataset_id'], **result['workspace_export'])
                run['state'].setdefault('routed_records', []).extend(group['record_ids'])
                _save(run)
                if group.get('training'):
                    settings = group['training']
                    trained = _call(run, prefix + 'incremental-training', 'training.start', {'parent_model_id': run['state']['model_id'],
                        'dataset_id': result['dataset_id'], 'name': group['name'] + '-迭代模型',
                        'epochs': settings['epochs'], 'batch_size': settings['batch_size']})
                    run['state']['training'] = trained
                    run['state'].setdefault('route_history', []).append(route_plan)
                    _save(run, status='awaiting_training')
                    _wait(run, tasks.STATUS_AWAITING_BUSINESS, '新数据集已生成，等待真实增量训练结果')
                    return
        run['state'].setdefault('route_history', []).append(route_plan)
        _complete(run)
    finally:
        workspace = Path(scan['_workspace']).resolve()
        safe_base = Path(backend.work_dir or tempfile.gettempdir()).resolve()
        if workspace.is_relative_to(safe_base) and workspace.name.startswith('transform-src-'):
            shutil.rmtree(workspace)


def _complete(run):
    remaining = set(_review_queue(run)) - set(run['state'].get('routed_records') or []) - set(run['state'].get('rejected_records') or [])
    if remaining:
        _save(run, status='awaiting_destination')
        tasks.clear_waiting(run['task_id'])
        tasks.set_status(run['task_id'], tasks.STATUS_AWAITING_INPUT, summary=f'所选样本已回流，另有 {len(remaining)} 条记录等待指定去向', force=True)
        _message(run, f'所选样本已回流；还有 {len(remaining)} 条本轮记录未指定去向，可继续对话分配。')
        return
    failures = sum(bool(item.get('error')) for item in run['state'].get('items', []))
    _save(run, status='partial_failed' if failures else 'completed')
    if tasks.get_task_by_id(run['task_id'])['status'] != tasks.STATUS_STOPPED:
        tasks.clear_waiting(run['task_id'])
        tasks.set_status(run['task_id'], tasks.STATUS_PARTIAL_FAILED if failures else tasks.STATUS_COMPLETED,
            summary=f'所选人工确认样本已回流；本轮另有 {failures} 项处理失败' if failures else '人工确认后的所选样本已按指定去向生成数据集版本', force=True)
        message = '本轮所选人工确认样本已完成回流。新数据集：' + '、'.join(item['name'] for item in run['state']['datasets']) + '。原数据文件保留；没有把 AI 建议冒充人工标注。'
        exports = [item['workspace_export']['directory'] for item in run['state']['datasets'] if item.get('workspace_export', {}).get('status') == 'succeeded']
        if exports:
            message += ' 工作区数据集目录：' + '；'.join(exports) + '。对应 ZIP 与来源清单保存在各目录的上一级。'
        if run['state'].get('training'):
            message += ' 增量训练已完成，新模型 ID：' + run['state']['training']['model_id'] + '。对比指标及验证条件已记录；条件不同不能宣称一定更优。'
        _message(run, message)


def reconcile(actor, task_id):
    # ponytail: 单机核对串行；多租户并发量增长后改成按批次的锁。
    with _LOCK:
        return _reconcile(actor, task_id)


def _reconcile(actor, task_id):
    run = task_run(task_id)
    if not run:
        return None
    get_run(actor, run['id'])
    changed = False
    if tasks.get_task(actor, task_id)['status'] == tasks.STATUS_STOPPED:
        return {'session_id': run['session_id'], 'task': tasks.snapshot(tasks.get_task(actor, task_id)),
                'status': tasks.STATUS_STOPPED, 'changed': False, 'checks': []}
    if run['status'] == 'awaiting_human':
        queue = _review_queue(run)
        if not queue:
            _save(run, status='completed')
            tasks.clear_waiting(task_id)
            tasks.set_status(task_id, tasks.STATUS_COMPLETED, summary='本轮已无待复核记录；删除不等于人工确认，没有生成反馈', force=True)
            return {'session_id': run['session_id'], 'task': tasks.snapshot(tasks.get_task(actor, task_id)),
                    'status': tasks.STATUS_COMPLETED, 'changed': True, 'checks': []}
        states = {key: get_backend()._read_record(key).get('human_review_status') for key in queue}
        confirmed = [key for key, status in states.items() if status == 'confirmed']
        rejected = [key for key, status in states.items() if status == 'rejected']
        run['state']['rejected_records'] = rejected
        run['state']['review_progress'] = {'total': len(queue), 'confirmed': len(confirmed), 'rejected': len(rejected)}
        _save(run)
        if queue and len(confirmed) + len(rejected) == len(queue):
            if not confirmed:
                _save(run, status='completed')
                tasks.clear_waiting(task_id)
                tasks.set_status(task_id, tasks.STATUS_COMPLETED, summary='本轮人工复核全部驳回，没有生成训练样本', force=True)
                _message(run, '本轮已全部人工驳回，未生成反馈数据或训练样本。原始检测记录保留供追溯。')
                return {'session_id': run['session_id'], 'task': tasks.snapshot(tasks.get_task(actor, task_id)),
                        'status': tasks.STATUS_COMPLETED, 'changed': True, 'checks': []}
            _save(run, status='awaiting_destination')
            tasks.clear_waiting(task_id)
            tasks.set_status(task_id, tasks.STATUS_AWAITING_INPUT, summary='人工复核已核实，请在对话中指定样本回流去向', force=True)
            _message(run, f'已从业务记录核实本轮人工复核完成：确认 {len(confirmed)} 条，驳回 {len(rejected)} 条。驳回项不会回流。请告诉我：哪些确认记录回原数据集生成修订版，哪些另建数据集；新建时请指定训练/验证/测试划分。也可指定增量训练参数和是否发布。')
            changed = True
    # 训练由现有业务进程执行；此处只读取，不重发启动请求。
    if run['status'] == 'awaiting_training':
        training = run['state']['training']
        model_id = training.get('model_id') or training.get('child_model_id')
        row = get_backend()._model_row(model_id)
        if row['status'] == 2:
            _save(run, status='partial_failed')
            tasks.set_status(task_id, tasks.STATUS_PARTIAL_FAILED, summary='增量训练失败，请检查训练日志', force=True)
        elif row['status'] in (1, 3):
            prefix = 'route-' + str(len(run['state']['route_history']) - 1) + '-'
            run['state']['comparison'] = _call(run, prefix + 'compare-models', 'model.compare',
                {'parent_model_id': run['state']['model_id'], 'child_model_id': model_id})
            settings = run['state']['route_plan']['destinations'][0]['training']
            if settings['publish']:
                _call(run, prefix + 'publish-trained', 'model.publish', {'model_id': model_id})
            _complete(run); changed = True
    task = tasks.get_task(actor, task_id)
    return {'session_id': task['session_id'], 'task': tasks.snapshot(task), 'status': task['status'],
            'reply': task.get('summary', ''), 'changed': changed, 'checks': [], 'inspection_run': view(run)}


def interrupt_on_startup():
    conn = connect()
    try:
        placeholders = ','.join('?' for _ in ACTIVE)
        conn.execute(f"UPDATE agent_task SET status='interrupted',summary='后端重启，批任务已中断；保留结果，不自动重发写入或付费请求',updated_at=? WHERE id IN (SELECT task_id FROM agent_inspection_run WHERE status IN ({placeholders})) AND status<>'stopped'", (now(), *ACTIVE))
        conn.execute(f"UPDATE agent_inspection_run SET status='interrupted',updated_at=? WHERE status IN ({placeholders})", (now(), *ACTIVE))
        conn.commit()
    finally:
        conn.close()

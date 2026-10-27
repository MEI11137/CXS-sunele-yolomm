"""Fixed, authenticated management APIs; these are never model tools."""
import uuid
from flask import current_app, request
from agent.routes import agent_bp, auth_required, _body, _current, _ok
from agent.errors import AgentError, Code
from agent import connections, permissions


def _backend():
    return current_app.extensions["haixun_agent"]["business_backend"]


def _mcp():
    return _backend().mcp_store


@agent_bp.get('/directories')
@auth_required
def list_directories():
    actor = _current()
    project = permissions.get_project(actor, request.args.get('project_id') or '')
    if project['owner_id'] != actor.user_id:
        raise AgentError(Code.FORBIDDEN, '只能管理自己项目的本机目录', http_status=403)
    return _ok(_backend().directory_store.list(project['id']))


@agent_bp.post('/directories')
@auth_required
def authorize_directory():
    actor, body = _current(), _body()
    project = permissions.get_project(actor, body.get('project_id') or '')
    if project['owner_id'] != actor.user_id:
        raise AgentError(Code.FORBIDDEN, '只能为自己的项目授权目录', http_status=403)
    try:
        row = _backend().directory_store.register(project['id'], str(body.get('root') or ''), str(body.get('label') or '')[:200], actor.user_id)
        permissions.register_resource(actor, project_id=project['id'], kind='directory', display_name=row['label'] or row['root'], source_table='authorized_directories', source_id=row['id'])
        return _ok(row)
    except ValueError as exc:
        raise AgentError(Code.VALIDATION, str(exc)) from exc


@agent_bp.delete('/directories/<directory_id>')
@auth_required
def disable_directory(directory_id):
    actor = _current()
    try:
        row = _backend().directory_store.get(directory_id)
        project = permissions.get_project(actor, row['project_id'])
        if project['owner_id'] != actor.user_id:
            raise AgentError(Code.FORBIDDEN, '无权撤销该目录', http_status=403)
        _backend().directory_store.disable(directory_id)
        return _ok({'disabled': True})
    except FileNotFoundError:
        raise AgentError(Code.NOT_FOUND, '目录不存在', http_status=404)


@agent_bp.get('/mcp/connections')
@auth_required
def list_mcp():
    actor = _current()
    rows = _mcp().list(actor.user_id)
    for row in rows:
        row.pop('credential_ref', None)
        row['tools'] = _mcp().list_tools(actor.user_id, row['id'])
    return _ok(rows)


@agent_bp.post('/mcp/connections')
@auth_required
def save_mcp():
    actor, body = _current(), _body()
    # Copy only supported fields; never accept owner_id or raw environment.
    payload = {k: body[k] for k in ('id', 'name', 'transport', 'endpoint', 'command', 'arguments', 'enabled') if k in body}
    payload['id'] = payload.get('id') or str(uuid.uuid4())
    try:
        existing = _mcp().get(payload['id'], actor.user_id)
    except FileNotFoundError:
        existing = None
    except PermissionError:
        raise AgentError(Code.NOT_FOUND, '连接不存在', http_status=404)
    payload['credential_ref'] = existing.get('credential_ref') if existing else None
    secret = body.get('credential')
    if secret:
        if not isinstance(secret, str) or len(secret) > 16000:
            raise AgentError(Code.VALIDATION, '凭据格式无效')
        connections.store_credential(actor, 'mcp', payload['id'], secret)
        payload['credential_ref'] = payload['id']
    if body.get('clear_credential') is True:
        connections.delete_credential(actor, 'mcp', payload['id'])
        payload['credential_ref'] = None
    try:
        result = _mcp().save(actor.user_id, payload)
    except (ValueError, KeyError) as exc:
        raise AgentError(Code.VALIDATION, str(exc)) from exc
    result.pop('credential_ref', None)
    return _ok(result)


@agent_bp.post('/mcp/connections/<connection_id>/discover')
@auth_required
def discover_mcp(connection_id):
    actor = _current()
    try:
        return _ok(_mcp().discover(actor.user_id, connection_id))
    except (FileNotFoundError, PermissionError):
        raise AgentError(Code.NOT_FOUND, '连接不存在', http_status=404)
    except Exception as exc:
        # SDK errors may carry authorization headers or process arguments.
        raise AgentError(Code.TOOL_BACKEND_UNAVAILABLE, f'MCP 连接/发现失败（{type(exc).__name__}）；请核对地址、程序和个人凭据', http_status=502) from exc


@agent_bp.get('/documents')
@auth_required
def list_documents():
    from services.document_service import list_documents as query
    actor = _current()
    project = permissions.get_project(actor, request.args.get('project_id') or '')
    return _ok(query(_backend().database_path, actor.user_id, project['id']))


@agent_bp.post('/documents/parse')
@auth_required
def parse_document():
    actor, body = _current(), _body()
    backend = _backend()
    try:
        args = {'asset_id': body.get('asset_id'), 'title': str(body.get('title') or '')[:200]}
        prepared = backend.prepare_tool(actor.user_id, 'document.parse', args)
        result = backend.execute_tool(actor.user_id, prepared['operation_id'], 'parse:' + str(args['asset_id']))
        if result.get('status') != 'succeeded' or (result.get('outputs') or {}).get('status') == 'failed':
            raise AgentError(Code.TOOL_BACKEND_UNAVAILABLE, '文档解析未成功，请查看结果后重新处理', details=result, http_status=409)
        from agent import resources
        resources.register_refs(actor.user_id, result.get('business_refs') or [], state_changed=True)
        return _ok(result)
    except (FileNotFoundError, PermissionError):
        raise AgentError(Code.NOT_FOUND, '文档不存在或无权访问', http_status=404)
    except ValueError as exc:
        raise AgentError(Code.VALIDATION, str(exc)) from exc


@agent_bp.get('/documents/<document_id>/search')
@auth_required
def search_document(document_id):
    from services.document_service import get_document, search_document as search
    actor = _current()
    try:
        document = get_document(_backend().database_path, document_id)
        permissions.get_project(actor, document['project_id'])
        if document['owner_id'] != actor.user_id:
            raise AgentError(Code.NOT_FOUND, '文档不存在', http_status=404)
        return _ok(search(_backend().database_path, document_id, (request.args.get('q') or '')[:1000]))
    except FileNotFoundError:
        raise AgentError(Code.NOT_FOUND, '文档不存在', http_status=404)
    except ValueError as exc:
        raise AgentError(Code.VALIDATION, str(exc)) from exc

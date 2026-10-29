"""本机开发入口：Codex/Harness 与内部 OpenCode 对话；不提供业务执行/批准能力。"""
import hashlib
import secrets
import threading
import time

from flask import Blueprint, request
from agent import identity, permissions, sessions, tasks
from agent.errors import AgentError, Code
from agent.routes import auth_required, _current, _body, _ok, get_runtime
from agent.store import connect, dumps, loads, new_id, now

dev_bp = Blueprint('agent_dev_bridge', __name__, url_prefix='/api/agent/dev')
_MESSAGE_LOCK = threading.RLock()


@dev_bp.errorhandler(AgentError)
def error(exc):
    from flask import jsonify
    return jsonify({'code': exc.http_status, 'msg': exc.message, 'data': exc.to_dict()}), exc.http_status


def _local():
    if request.remote_addr not in ('127.0.0.1', '::1'):
        raise AgentError(Code.FORBIDDEN, '开发接口仅限本机', http_status=403)


@dev_bp.post('/connections')
@auth_required
def create():
    _local()
    actor = _current(); body = _body()
    if not actor.is_admin or body.get('source') not in ('codex', 'deepseekharness'):
        raise AgentError(Code.FORBIDDEN, '须由本机管理员指定 codex 或 deepseekharness 开发连接', http_status=403)
    project = permissions.get_project(actor, str(body.get('project_id') or ''))
    actor.assert_owner(project['owner_id'])
    session = sessions.create_session(actor, project['id'], '开发调试 · ' + body['source'])
    key = secrets.token_urlsafe(36); connection_id = new_id('dev')
    expires = int(time.time()) + 4 * 3600
    conn = connect()
    try:
        conn.execute('INSERT INTO agent_dev_connection(id,owner_id,project_id,session_id,source,token_hash,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (connection_id, actor.user_id, project['id'], session['id'], body['source'], hashlib.sha256(key.encode()).hexdigest(), expires, now()))
        conn.commit()
    finally:
        conn.close()
    return _ok({'connection_id': connection_id, 'project_id': project['id'], 'session_id': session['id'],
                'token': key, 'expires_at': expires, 'scope': ['read_development_session', 'message_internal_agent'],
                'notice': '令牌只返回这一次，有效期4小时；不包含模型 Key，不能批准、删除或代替人工复核'})


@dev_bp.delete('/connections/<connection_id>')
@auth_required
def revoke(connection_id):
    _local()
    conn = connect()
    try:
        conn.execute('UPDATE agent_dev_connection SET revoked_at=? WHERE id=? AND owner_id=?', (now(), connection_id, _current().user_id))
        conn.commit()
    finally:
        conn.close()
    return _ok({'revoked': True})


def _connection():
    _local()
    token = request.headers.get('Authorization', '').removeprefix('Bearer ')
    conn = connect()
    try:
        row = conn.execute('SELECT * FROM agent_dev_connection WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?',
                           (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
    finally:
        conn.close()
    if not row:
        raise AgentError(Code.UNAUTHENTICATED, '开发连接已失效或已撤销', http_status=401)
    connection = dict(row)
    actor = identity.load_actor(connection['owner_id'], source='external-dev')
    if not actor.is_admin:
        raise AgentError(Code.FORBIDDEN, '管理员权限已撤销，开发连接不可继续使用', http_status=403)
    actor.assert_owner(permissions.get_project(actor, connection['project_id'])['owner_id'])
    sessions.get_session(actor, connection['session_id'])
    return actor, connection


@dev_bp.get('/state')
def state():
    actor, connection = _connection()
    session = sessions.get_session(actor, connection['session_id'])
    task = tasks.find_active_task(actor, session['id'])
    return _ok({'project_id': connection['project_id'], 'session_id': session['id'],
                'messages': sessions.list_messages(session['id'], limit=40), 'task': tasks.snapshot(task) if task else None})


@dev_bp.post('/message')
def message():
    actor, connection = _connection(); body = _body()
    text = body.get('text'); key = body.get('request_id')
    if set(body) != {'text', 'request_id'} or not isinstance(text, str) or not 0 < len(text) <= 7000 or not isinstance(key, str) or not 0 < len(key) <= 100:
        raise AgentError(Code.VALIDATION, '仅接受 text 与唯一 request_id')
    fingerprint = hashlib.sha256(text.encode()).hexdigest()
    with _MESSAGE_LOCK:
        conn = connect()
        try:
            prior = conn.execute('SELECT * FROM agent_dev_request WHERE connection_id=? AND request_id=?', (connection['id'], key)).fetchone()
            if prior:
                if prior['text_hash'] != fingerprint or not prior['response_json']:
                    raise AgentError(Code.TASK_STATE_CONFLICT, '该请求已提交或结果待核实，不能换参数或自动重发', http_status=409)
                return _ok(loads(prior['response_json']))
            active = tasks.find_active_task(actor, connection['session_id'])
            if active and active['status'] != tasks.STATUS_AWAITING_INPUT:
                raise AgentError(Code.TASK_STATE_CONFLICT, '开发会话已有任务或确认卡，请在网站核对；外部客户端不能代替批准', http_status=409)
            conn.execute('INSERT INTO agent_dev_request VALUES(?,?,?,?,?)', (connection['id'], key, fingerprint, None, now()))
            conn.commit()
        finally:
            conn.close()
        result = get_runtime().handle_message(actor, connection['session_id'],
                    '[开发调试消息 · ' + connection['source'] + ']\n' + text)
        conn = connect()
        try:
            conn.execute('UPDATE agent_dev_request SET response_json=? WHERE connection_id=? AND request_id=?',
                          (dumps(result), connection['id'], key))
            conn.commit()
        finally:
            conn.close()
        return _ok(result)

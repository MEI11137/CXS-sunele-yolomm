"""项目业务文件的统一路径；请求/线程独立，绝不修改进程级临时目录。"""
from contextlib import contextmanager, closing
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
import re
import sqlite3

from flask import current_app, has_app_context

_scope = ContextVar('project_file_scope', default=None)
_operation = ContextVar('project_file_operation', default=False)


def strict_workspace():
    return has_app_context() and current_app.config.get('PROJECT_FILES_IN_WORKSPACE', False)


def current_scope():
    return _scope.get()


def root_for(database, project_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', str(project_id)):
        raise ValueError('项目 ID 无效')
    with closing(sqlite3.connect(str(database))) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='agent_project'").fetchone():
            return None  # 独立业务服务的旧契约；网站实际库始终有此表。
        row = conn.execute('SELECT root_directory FROM agent_project WHERE id=?', (project_id,)).fetchone()
    if not row or not row[0]:
        return None
    path = Path(row[0])
    if not path.is_absolute() or not path.is_dir() or path.resolve() != path.absolute():
        raise ValueError('项目目录已改变或不可访问，请重新绑定')
    return path.resolve()


def legacy_location(database, bucket, object_key):
    if not Path(database).is_file():
        return None
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='workspace_file_alias'").fetchone():
            return None
        row = conn.execute('SELECT bucket, object_key FROM workspace_file_alias WHERE old_bucket=? AND old_key=?',
                           (bucket, object_key)).fetchone()
    return tuple(row) if row else None


def select_project(database, project_id):
    root = root_for(database, project_id)
    if root is None and strict_workspace():
        raise ValueError('请先为项目绑定本机文件夹；项目文件不再写入后端目录')
    scope = (str(Path(database).resolve()), project_id, root) if root else None
    _scope.set(scope)
    return scope


@contextmanager
def project_scope(database, project_id):
    token = _scope.set(None)
    try:
        select_project(database, project_id)
        yield current_scope()
    finally:
        _scope.reset(token)


def scoped_operation(func):
    """业务适配层解析真实授权资源后才选定输出项目，结束即恢复。"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        token = _scope.set(None)
        operation_token = _operation.set(True)
        try:
            return func(*args, **kwargs)
        finally:
            _operation.reset(operation_token)
            _scope.reset(token)
    return wrapper


def resource_selected(database, project_id):
    if _operation.get():
        select_project(database, project_id)


def project_tool(func):
    @wraps(func)
    def wrapper(self, actor, spec, arguments, *args, **kwargs):
        from agent.permissions import get_project
        get_project(actor, arguments['project_id'])
        with project_scope(current_app.config['DATABASE'], arguments['project_id']):
            return func(self, actor, spec, arguments, *args, **kwargs)
    return wrapper


def project_run(func):
    @wraps(func)
    def wrapper(run, *args, **kwargs):
        with project_scope(current_app.config['DATABASE'], run['project_id']):
            return func(run, *args, **kwargs)
    return wrapper


def directory(section, fallback=None):
    scope = current_scope()
    if not scope:
        return fallback
    database, project_id, root = scope
    if root_for(database, project_id) != root:
        raise ValueError('工作区绑定在操作期间改变，已停止写入')
    relative = Path(section)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('项目输出相对路径无效')
    target = root / relative
    if target.resolve() != target.absolute() or not target.resolve().is_relative_to(root):
        raise ValueError('项目输出路径包含链接或越界')
    target.mkdir(parents=True, exist_ok=True)
    return str(target)


def temp_directory(fallback=None):
    return directory('.cache', fallback)


def resource_project(database, table, resource_id):
    with closing(sqlite3.connect(str(database))) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='agent_resource'").fetchone():
            return None
        row = conn.execute('SELECT project_id FROM agent_resource WHERE source_table=? AND source_id=?',
                           (table, resource_id)).fetchone()
    return row[0] if row else None


@contextmanager
def resource_scope(database, table, resource_id, project_id=None):
    actual = resource_project(database, table, resource_id)
    if actual and project_id and actual != project_id:
        raise ValueError('后台任务目标项目与真实资源归属不一致')
    project_id = actual or project_id
    if project_id:
        with project_scope(database, project_id) as scope:
            yield scope
    else:
        yield None


class ProjectTempPath:
    """为旧路由保留 PathLike 接口；路径按当前请求/任务计算。"""
    def __init__(self, fallback):
        self.fallback = str(fallback)

    def __fspath__(self):
        return temp_directory(self.fallback)

    def __str__(self):
        return self.__fspath__()

"""系统提示要求删除/修改命令独立执行，核验用独立读取工具（不扩大解析器范围）。"""
import threading
from types import SimpleNamespace

from agent import sessions, tasks
from agent.hx_engine_runtime import HxEngineRuntime
from tests.test_inspection_loop import setup


def _engine(f):
    captured = {}

    def request(method, path, payload=None):
        if method == 'POST' and path.endswith('/prompt_async'):
            captured['payload'] = payload
            return {}
        return []

    return SimpleNamespace(
        task_id=None, site_session=f.chat['id'], remote_session='remote', generation=1,
        busy=False, workspace=f.root, fingerprint='0' * 64,
        credentials=SimpleNamespace(model='fake-model'),
        lock=threading.Lock(), request=request, captured=captured,
        redact=lambda value: value, abort=lambda: None,
        turn_notes=[], turn_saved=False)


def _build_system_prompt(runtime, f, mode, monkeypatch):
    sessions.set_permission_mode(f.actor, f.chat['id'], mode)
    engine = _engine(f)
    monkeypatch.setattr(runtime, '_engine', lambda actor, session: engine)
    monkeypatch.setattr(runtime, '_resume_automatic_actions', lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, '_task_manual', lambda actor, task: None)
    monkeypatch.setattr(runtime, '_watch', lambda *args, **kwargs: None)
    monkeypatch.setattr('agent.hx_engine_runtime.context.build_messages',
                        lambda *args, **kwargs: [{'role': 'system', 'content': '基础系统提示。'},
                                                 {'role': 'user', 'content': '测试'}])
    task = tasks.get_task(f.actor, f.task['id'])
    runtime._run_loop(f.actor, sessions.get_session(f.actor, f.chat['id']), f.project, task)
    return engine.captured['payload']['system']


def test_system_prompt_requires_standalone_delete_and_separate_verification(setup, monkeypatch):
    f = setup
    runtime = HxEngineRuntime(f.app)
    for mode in ('request', 'full'):
        system = _build_system_prompt(runtime, f, mode, monkeypatch)
        assert '文件删除或修改命令必须单独一条执行' in system
        assert 'Test-Path' in system and 'read/glob' in system
        assert "Remove-Item -LiteralPath '项目内一次性测试文件' -Force" in system
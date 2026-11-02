"""A completed tool message must not end the engine's final-answer collection."""
from types import SimpleNamespace

from agent import sessions, tasks
from agent.hx_engine_runtime import HxEngineRuntime
from tests.test_inspection_loop import setup


def test_tool_completion_then_delayed_answer_is_saved_without_second_user_message(setup, monkeypatch):
    f = setup
    runtime = HxEngineRuntime(f.app)
    tool = {'info': {'id': 'tool-turn', 'role': 'assistant', 'finish': 'tool-calls',
                     'time': {'completed': 1}}, 'parts': []}
    partial = {'info': {'id': 'answer', 'role': 'assistant'},
               'parts': [{'type': 'text', 'text': '正在整理'}]}
    answer = {'info': {'id': 'answer', 'role': 'assistant', 'finish': 'stop',
                       'time': {'completed': 2}},
              'parts': [{'type': 'text', 'text': '数据集可读取，配对完整。'}]}
    snapshots = [[tool], [tool, partial], [tool, answer]]
    seen = []
    def request(method, path):
        if path.endswith('/message'):
            seen.append(path)
            assert len(seen) <= 3, 'watcher did not recognize final answer'
            return snapshots[len(seen) - 1]
        if path == '/session/status': return {}  # Idle can briefly appear between steps.
        return []
    engine = SimpleNamespace(task_id=f.task['id'], site_session=f.chat['id'], remote_session='remote',
        generation=1, busy=True, request=request, redact=lambda v: v, abort=lambda: None)
    monkeypatch.setattr(runtime, '_permissions', lambda *args: None)
    monkeypatch.setattr('agent.hx_engine_runtime.time.sleep', lambda _: None)
    runtime._watch(engine, f.actor, 1, set())
    replies = [m['content'] for m in sessions.list_messages(f.chat['id']) if m['role'] == 'assistant']
    assert replies == ['数据集可读取，配对完整。']
    assert len(seen) == 3
    assert tasks.get_task(f.actor, f.task['id'])['status'] == tasks.STATUS_COMPLETED
    assert not engine.busy

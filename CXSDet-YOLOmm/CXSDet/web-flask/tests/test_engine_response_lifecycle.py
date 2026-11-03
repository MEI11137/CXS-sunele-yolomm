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


def test_compaction_summary_is_internal_and_does_not_truncate_final_answer(setup, monkeypatch):
    f = setup
    runtime = HxEngineRuntime(f.app)
    compact = {'info': {'id': 'compact-turn', 'role': 'assistant', 'agent': 'compaction',
                        'mode': 'compaction', 'summary': True, 'finish': 'stop',
                        'time': {'completed': 1}},
               'parts': [{'type': 'reasoning', 'text': '私有压缩推理'},
                         {'type': 'text', 'text': 'Objective: 压缩摘要正文，不得展示。'}]}
    answer = {'info': {'id': 'answer', 'role': 'assistant', 'finish': 'stop',
                       'time': {'completed': 2}},
              'parts': [{'type': 'text', 'text': '最终答复：共 3 条记录。'}]}
    # 压缩行已带 finish=stop/time.completed，不能据此提前收尾；真实 final 必须照常保存。
    snapshots = [[compact], [compact], [compact, answer]]
    seen = []
    def request(method, path):
        if path.endswith('/message'):
            seen.append(path)
            assert len(seen) <= 3, 'compaction summary ended the turn early'
            return snapshots[len(seen) - 1]
        if path == '/session/status': return {}
        return []
    engine = SimpleNamespace(task_id=f.task['id'], site_session=f.chat['id'], remote_session='remote',
        generation=1, busy=True, request=request, redact=lambda v: v, abort=lambda: None)
    monkeypatch.setattr(runtime, '_permissions', lambda *args: None)
    monkeypatch.setattr('agent.hx_engine_runtime.time.sleep', lambda _: None)
    runtime._watch(engine, f.actor, 1, set())
    replies = [m['content'] for m in sessions.list_messages(f.chat['id']) if m['role'] == 'assistant']
    assert replies == ['最终答复：共 3 条记录。']
    assert len(seen) == 3
    assert tasks.get_task(f.actor, f.task['id'])['status'] == tasks.STATUS_COMPLETED
    assert not engine.busy


def test_reasoning_is_never_saved_and_final_answer_still_completes(setup, monkeypatch):
    f = setup
    runtime = HxEngineRuntime(f.app)
    reasoning = {'info': {'id': 'reasoning-turn', 'role': 'assistant', 'finish': 'tool-calls',
                          'time': {'completed': 1}},
                 'parts': [{'type': 'reasoning', 'text': '内部推理，不应保存。'}]}
    answer = {'info': {'id': 'answer', 'role': 'assistant', 'finish': 'stop',
                       'time': {'completed': 2}},
              'parts': [{'type': 'text', 'text': '最终答复：已完成。'}]}
    snapshots = [[reasoning], [reasoning, answer]]
    seen = []
    def request(method, path):
        if path.endswith('/message'):
            seen.append(path)
            assert len(seen) <= 2
            return snapshots[len(seen) - 1]
        if path == '/session/status': return {}
        return []
    engine = SimpleNamespace(task_id=f.task['id'], site_session=f.chat['id'], remote_session='remote',
        generation=1, busy=True, request=request, redact=lambda v: v, abort=lambda: None)
    monkeypatch.setattr(runtime, '_permissions', lambda *args: None)
    monkeypatch.setattr('agent.hx_engine_runtime.time.sleep', lambda _: None)
    runtime._watch(engine, f.actor, 1, set())
    replies = [m['content'] for m in sessions.list_messages(f.chat['id']) if m['role'] == 'assistant']
    assert replies == ['最终答复：已完成。']
    assert tasks.get_task(f.actor, f.task['id'])['status'] == tasks.STATUS_COMPLETED
    assert not engine.busy


def test_aborted_assistant_is_not_completed_and_ends_interrupted(setup, monkeypatch):
    f = setup
    runtime = HxEngineRuntime(f.app)
    aborted = {'info': {'id': 'aborted-turn', 'role': 'assistant', 'finish': 'stop',
                        'time': {'completed': 1},
                        'error': {'name': 'MessageAbortedError', 'message': 'aborted'}},
               'parts': [{'type': 'text', 'text': '半截回答'}]}
    calls = []
    def request(method, path):
        if path.endswith('/message'):
            calls.append(path)
            assert len(calls) <= 6, 'aborted assistant was treated as completed'
            return [aborted]
        if path == '/session/status': return {}
        return []
    engine = SimpleNamespace(task_id=f.task['id'], site_session=f.chat['id'], remote_session='remote',
        generation=1, busy=True, request=request, redact=lambda v: v, abort=lambda: None)
    monkeypatch.setattr(runtime, '_permissions', lambda *args: None)
    monkeypatch.setattr('agent.hx_engine_runtime.time.sleep', lambda _: None)
    monkeypatch.setenv('HX_ENGINE_TURN_TIMEOUT', '0')
    runtime._watch(engine, f.actor, 1, set())
    assert tasks.get_task(f.actor, f.task['id'])['status'] == tasks.STATUS_INTERRUPTED
    assert not engine.busy

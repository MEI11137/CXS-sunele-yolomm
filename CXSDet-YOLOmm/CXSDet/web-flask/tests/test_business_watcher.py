from concurrent.futures import ThreadPoolExecutor

import pytest

from agent import business_watcher as watcher, sessions, store, tasks
from tests.test_inspection_loop import setup, start


@pytest.mark.parametrize('deleted', [False, True])
def test_terminal_training_notifies_once_and_missing_model_does_not_hang(setup, deleted):
    f = setup
    run = start(f)
    before = len(sessions.list_messages(f.chat['id']))
    model_id = run['state']['model_id']
    tasks.set_waiting(f.task['id'], kind='business', reason='training', refs=[{'type':'model','id':model_id}])
    tasks.set_status(f.task['id'], tasks.STATUS_AWAITING_BUSINESS, force=True)
    if deleted:
        f.db.execute('DELETE FROM models WHERE id=?', (model_id,))
    else:
        f.db.execute('UPDATE models SET status=1 WHERE id=?', (model_id,))
    assert watcher.scan_once() == 1
    assert watcher.scan_once() == 0
    task = tasks.get_task(f.actor, f.task['id'])
    assert task['status'] == (tasks.STATUS_FAILED if deleted else tasks.STATUS_COMPLETED)
    replies = [m for m in sessions.list_messages(f.chat['id'])[before:] if m['role']=='assistant']
    assert len(replies) == 1
    assert ('不存在' if deleted else '训练完成') in replies[0]['content']


def test_background_loop_keeps_application_database(setup, monkeypatch):
    f = setup
    seen = []
    class StopLoop(Exception): pass
    monkeypatch.setattr(watcher, 'scan_once', lambda: seen.append(store.current_db_path()))
    monkeypatch.setattr(watcher, '_reap_stale_running', lambda: None)
    def stop(_): raise StopLoop()
    monkeypatch.setattr(watcher.time, 'sleep', stop)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(StopLoop):
            executor.submit(watcher._loop, f.app).result(timeout=5)
    assert seen == [str(f.database)]

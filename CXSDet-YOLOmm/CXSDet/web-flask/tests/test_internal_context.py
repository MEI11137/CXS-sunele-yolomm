"""旧内部压缩摘要的标记与过滤：SQL 健壮性、路径一致性、精确回填。"""
import hashlib
import json
import os
import sqlite3
from types import SimpleNamespace

from agent import internal_context, sessions
from agent.hx_engine_runtime import HxEngineRuntime
from tests.test_inspection_loop import setup


def test_internal_refs_sql_handles_string_values_and_invalid_json():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE agent_message (id TEXT PRIMARY KEY, refs_json TEXT)')
    rows = [
        ('internal_object', '[{"type": "internal_context"}]'),
        ('plain_string', '["hello"]'),
        ('string_marker', '["internal_context"]'),
        ('execution_note', '[{"type": "execution_note"}]'),
        ('invalid_json', '{not json'),
        ('null_refs', None),
        ('nested_array', '[{"type": ["unexpected"]}]'),
    ]
    conn.executemany('INSERT INTO agent_message VALUES (?, ?)', rows)
    clause = internal_context.internal_refs_sql()
    got = {row[0] for row in conn.execute(
        f'SELECT id FROM agent_message WHERE 1=1 AND {clause}', (internal_context.MARKER,))}
    # 只有真正的结构对象标记被排除；["string"]、非法 JSON、NULL 都要正常保留。
    assert got == {'plain_string', 'string_marker', 'execution_note', 'invalid_json', 'null_refs', 'nested_array'}
    conn.close()


def test_internal_id_clause_excludes_exact_site_ids():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE agent_message (id TEXT PRIMARY KEY)')
    conn.executemany('INSERT INTO agent_message VALUES (?)', [('oc_a',), ('oc_b',), ('visible',)])
    clause, params = internal_context.internal_id_clause({'oc_a'})
    got = {row[0] for row in conn.execute(f'SELECT id FROM agent_message WHERE 1=1{clause}', params)}
    assert got == {'oc_b', 'visible'}
    conn.close()


def test_engine_db_key_uses_the_same_raw_string_as_the_runtime(monkeypatch, tmp_path):
    raw = r'E:\智能体创新应用大赛\海巡智检v1.5\.integration-checks\ui-live\site.db'
    monkeypatch.setattr(internal_context, '_app_config',
                        lambda key: raw if key == 'DATABASE' else None)
    monkeypatch.setenv('HX_ENGINE_STATE_DIR', str(tmp_path / 'hxengine'))
    expected = hashlib.sha256(raw.encode()).hexdigest()[:12]
    assert internal_context.database_key() == expected
    path = internal_context.engine_db_path(7, 'ses_example')
    assert path.is_absolute()
    assert path.parts[0] != '' and expected in path.parts
    # 反斜杠不能被 posix 化后再哈希（否则真实 Windows 库找不到）。
    assert internal_context.database_key() != hashlib.sha256(
        raw.replace('\\', '/').encode()).hexdigest()[:12]


def test_backfill_marks_only_matched_assistant_internal_messages(setup, tmp_path):
    f = setup
    runtime = HxEngineRuntime(f.app)
    f.app.config['HX_ENGINE_STATE_DIR'] = str(tmp_path / 'hxengine')
    owner = internal_context._owner_id(f.chat['id'])
    assert owner is not None
    engine_path = internal_context.engine_db_path(owner, f.chat['id'])
    engine_path.parent.mkdir(parents=True, exist_ok=True)
    engine_rows = []
    # 引擎库里 8 条内部压缩候选，站点只保存了前 5 条。
    for index in range(8):
        info = {'role': 'assistant', 'agent': 'compaction', 'mode': 'compaction',
                'summary': True, 'id': f'msg{index}'}
        engine_rows.append((f'msg{index}', json.dumps(info)))
    # 普通助手消息不应被标记。
    engine_rows.append(('msg_normal', json.dumps({'role': 'assistant', 'id': 'msg_normal'})))
    engine = sqlite3.connect(engine_path)
    engine.execute('CREATE TABLE message (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
    engine.executemany('INSERT INTO message VALUES (?, ?)', engine_rows)
    engine.commit()
    engine.close()

    fake = SimpleNamespace(site_session=f.chat['id'], redact=lambda value: value)
    for index in range(5):
        runtime._save_message(fake, f.task['id'], f'msg{index}', f'内部压缩摘要 {index}')
    runtime._save_message(fake, f.task['id'], 'msg_normal', '正常最终答复')

    result = internal_context.backfill_session(f.chat['id'])
    assert result['engine_internal'] == 5, result  # 不把 8 条引擎候选误报成历史摘要
    assert result['matched'] == 5 and result['marked'] == 5
    again = internal_context.backfill_session(f.chat['id'])
    assert again['marked'] == 0  # 幂等

    visible = [row['content'] for row in sessions.list_messages(f.chat['id'])]
    assert '正常最终答复' in visible
    assert not any('内部压缩摘要' in text for text in visible)
    # 明文正文保留在库里供审计，只增加结构标记。
    row = f.db.query(
        "SELECT refs_json FROM agent_message WHERE content='内部压缩摘要 0'", fetchone=True)
    refs = json.loads(row['refs_json'])
    assert {'type': internal_context.MARKER} in refs
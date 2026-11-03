from flask import Flask
from agent import legacy, store
from tests.guard_support import install, resource


def test_only_unindexed_legacy_records_create_history_project(tmp_path):
    app = Flask(__name__)
    with app.app_context():
        install(app, tmp_path/'site.db', resources=[
            resource('detection_records', 'current-record', 7, project_id='current-project')])
        with store.connect() as conn:
            conn.execute('CREATE TABLE detection_records (id TEXT PRIMARY KEY, user_id INTEGER, title TEXT)')
            conn.executemany('INSERT INTO detection_records VALUES (?,?,?)', [
                ('current-record', 7, 'current'), ('old-record', 8, 'legacy')])
            conn.commit()
        assert legacy.migrate_legacy_resources()['detection_records'] == 1
        assert legacy.migrate_legacy_resources()['detection_records'] == 0
        with store.connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM agent_project WHERE owner_id=7').fetchone()[0] == 1
            assert conn.execute("SELECT project_id FROM agent_resource WHERE source_id='current-record'").fetchone()[0] == 'current-project'
            old = conn.execute("SELECT p.name,p.owner_id FROM agent_resource r JOIN agent_project p ON p.id=r.project_id WHERE r.source_id='old-record'").fetchone()
            assert tuple(old) == ('历史项目', 8)

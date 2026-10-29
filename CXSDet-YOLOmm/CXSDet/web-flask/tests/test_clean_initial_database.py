"""Publishing source must not fabricate uploaded datasets or model weights."""
from utils.db import Database


def test_default_install_and_restart_keep_business_tables_empty(tmp_path):
    path=tmp_path/'clean.sqlite3'
    db=Database(path)
    assert db.query('SELECT COUNT(*) AS n FROM user',fetchone=True)['n']==2
    for table in ['datasets','models','detection_records']:
        assert db.query('SELECT COUNT(*) AS n FROM '+table,fetchone=True)['n']==0
    Database(path)
    for table in ['datasets','models']:
        assert db.query('SELECT COUNT(*) AS n FROM '+table,fetchone=True)['n']==0


def test_explicit_test_seed_does_not_reappear_after_deletion(tmp_path):
    path=tmp_path/'demo.sqlite3'
    db=Database(path,seed_demo=True)
    assert db.query('SELECT COUNT(*) AS n FROM models',fetchone=True)['n']==1
    db.execute('DELETE FROM models');db.execute('DELETE FROM datasets')
    Database(path,seed_demo=True)
    assert db.query('SELECT COUNT(*) AS n FROM models',fetchone=True)['n']==0
    assert db.query('SELECT COUNT(*) AS n FROM datasets',fetchone=True)['n']==0

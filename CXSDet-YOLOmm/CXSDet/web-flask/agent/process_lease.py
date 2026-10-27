"""Single local backend per database; another live process must not reconcile it."""
import os
import sqlite3
import psutil


def claim(database_path):
    pid, started = os.getpid(), psutil.Process().create_time()
    with sqlite3.connect(str(database_path), timeout=30) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS agent_process_lease (id INTEGER PRIMARY KEY CHECK(id=1), pid INTEGER NOT NULL, started REAL NOT NULL)')
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT pid,started FROM agent_process_lease WHERE id=1').fetchone()
        if row and row[0] == pid and row[1] == started:
            return False
        if row:
            try:
                other = psutil.Process(row[0])
                if other.is_running() and abs(other.create_time() - row[1]) < .01:
                    raise RuntimeError('同一数据库已有后端进程运行，请先关闭旧进程；第一版仅支持单进程后端')
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied as exc:
                raise RuntimeError('无法核实旧后端进程，不会把其任务自动标为中断') from exc
        conn.execute('INSERT OR REPLACE INTO agent_process_lease VALUES(1,?,?)', (pid,started))
    return True

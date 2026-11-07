"""deploy/serve.py 的启动身份与任务保护（纯控制路径，不绑端口、不建 app）。

对应 root 的独立反例：运行记录可以被人从别的数据目录拷过来，端口也可能被陌生程序
占着；这两种情况下都不能把“记录 + 端口上的服务”当成自己的实例。另外，任务状态
读不出来时不能等同于“没有任务”，否则普通停止会误停一个可能还在跑训练的服务。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("hx17_serve", ROOT / "deploy" / "serve.py")
serve = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(serve)


def _scratch(prefix: str) -> Path:
    """夹具目录放在已被忽略的 ``var/`` 下，不往工程根目录扔临时文件夹。"""
    base = ROOT / "var"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=base))


def _dead_pid() -> int:
    """拿一个确定已经退出的 PID，用来模拟“记录里的进程不在了”。"""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    pid = proc.pid
    if serve._pid_alive(pid):
        raise unittest.SkipTest("PID 被复用，无法构造“已退出”的样本")
    return pid


class LauncherIdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = _scratch("hx17-identity-")
        self.here = self.tmp / "here"
        self.other = self.tmp / "other"
        self.here.mkdir()
        self.other.mkdir()
        self.state = {
            "pid": 4242,
            "host": "127.0.0.1",
            "port": 50999,
            "instance": "fixture-instance",
            "token": "fixture-token",
            "data_dir": str(self.here),
            "started_at": "fixture",
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _live(self, **overrides) -> dict:
        live = {
            "pid": 4242,
            "instance": "fixture-instance",
            "port": 50999,
            "data_dir": str(self.here),
        }
        live.update(overrides)
        return live

    def _args(self, *argv: str):
        return serve.build_parser().parse_args(list(argv))

    # ---------------------------------------------------------------- 身份判定

    def test_identity_matches_same_instance(self):
        self.assertIsNone(serve.identity_conflict(self.state, self._live(), self.here))

    def test_copied_record_is_not_this_directory(self):
        code, reason = serve.identity_conflict(self.state, self._live(), self.other)
        self.assertEqual(code, 4)
        self.assertIn(str(self.here), reason)

    def test_service_running_in_another_directory_is_not_this_instance(self):
        code, reason = serve.identity_conflict(
            dict(self.state, data_dir=str(self.other)),
            self._live(data_dir=str(self.other)),
            self.here,
        )
        self.assertEqual(code, 4)
        self.assertIn(str(self.other), reason)

    def test_port_serving_a_foreign_process_is_rejected(self):
        code, reason = serve.identity_conflict(self.state, self._live(instance="other"), self.here)
        self.assertEqual(code, 7)
        self.assertIn("instance", reason)
        code, reason = serve.identity_conflict(self.state, self._live(pid=7), self.here)
        self.assertEqual(code, 7)
        self.assertIn("PID", reason)

    # ---------------------------------------------------------------- 命令分支

    def test_status_does_not_report_copied_record_as_running(self):
        serve.write_state(self.other, self.state)
        with patch.object(serve, "probe", return_value=self._live()):
            code = serve.cmd_status(self._args("status", "--data-dir", str(self.other)))
        self.assertEqual(code, 4)

    def test_start_refuses_copied_record_before_binding(self):
        serve.write_state(self.other, self.state)
        binding = patch.object(serve, "port_free", return_value=True)
        with patch.object(serve, "probe", return_value=self._live()), binding as bound:
            code = serve.cmd_start(
                self._args("start", "--data-dir", str(self.other), "--port", "50999")
            )
        self.assertEqual(code, 4)
        self.assertFalse(bound.called, "异目录记录不能往下走去绑新端口")

    def test_start_refuses_while_recorded_pid_still_alive(self):
        """PID 活着但服务不响应：拒绝同目录重起，且不去碰端口。"""
        serve.write_state(self.here, dict(self.state, pid=os.getpid()))
        binding = patch.object(serve, "port_free", return_value=True)
        with patch.object(serve, "probe", return_value=None), binding as bound:
            code = serve.cmd_start(self._args("start", "--data-dir", str(self.here), "--port", "50998"))
        self.assertEqual(code, 6)
        self.assertFalse(bound.called)
        self.assertTrue(serve.state_path(self.here).is_file(), "不确定时不能删记录")

    def test_start_clears_record_only_after_pid_is_gone(self):
        serve.write_state(self.here, dict(self.state, pid=_dead_pid()))
        with (
            patch.object(serve, "probe", return_value=None),
            patch.object(serve, "check_backend", return_value=[]),
            patch.object(serve, "check_waitress", return_value=[]),
            patch.object(serve, "check_dist", return_value=[]),
            patch.object(serve, "port_free", return_value=False),
        ):
            code = serve.cmd_start(self._args("start", "--data-dir", str(self.here), "--port", "50997"))
        self.assertEqual(code, 5, "进程确实不在了：清掉旧记录，继续走到端口检查")
        self.assertFalse(serve.state_path(self.here).is_file())

    def test_stop_refuses_when_work_state_is_unknown(self):
        serve.write_state(self.here, self.state)
        with (
            patch.object(serve, "probe", return_value=self._live()),
            patch.object(
                serve, "active_work", side_effect=serve.WorkStateUnknown("db corrupt")
            ),
            patch.object(serve, "request_shutdown", return_value=True) as shutdown,
        ):
            code = serve.cmd_stop(self._args("stop", "--data-dir", str(self.here)))
        self.assertEqual(code, 10)
        self.assertFalse(shutdown.called, "状态未知时不能发停止请求")

    def test_force_still_stops_when_work_state_is_unknown(self):
        serve.write_state(self.here, self.state)
        with (
            patch.object(
                serve, "active_work", side_effect=serve.WorkStateUnknown("db corrupt")
            ),
            patch.object(serve, "request_shutdown", return_value=True) as shutdown,
            patch.object(serve, "_pid_alive", return_value=False),
            patch.object(serve, "probe", side_effect=[self._live(), None]),
        ):
            code = serve.cmd_stop(
                self._args("stop", "--data-dir", str(self.here), "--force")
            )
        self.assertEqual(code, 0)
        self.assertTrue(shutdown.called, "显式 --force 的语义要保留")


class LauncherWorkStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = _scratch("hx17-work-")
        self.data = self.tmp / "data"
        self.data.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_database_means_idle(self):
        self.assertEqual(serve.active_work(self.data), {})

    def test_corrupt_database_is_unknown_not_idle(self):
        (self.data / "haixun.db").write_bytes(b"not a sqlite database")
        with self.assertRaises(serve.WorkStateUnknown):
            serve.active_work(self.data)

    def test_missing_table_is_unknown_not_idle(self):
        conn = sqlite3.connect(self.data / "haixun.db")
        try:
            conn.execute("CREATE TABLE unrelated (id INTEGER)")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(serve.WorkStateUnknown):
            serve.active_work(self.data)

    def test_counts_in_flight_work_read_only(self):
        conn = sqlite3.connect(self.data / "haixun.db")
        try:
            conn.executescript(
                "CREATE TABLE turns (status TEXT);"
                "CREATE TABLE jobs (status TEXT);"
                "CREATE TABLE training_runs (status TEXT, launch_state TEXT);"
                "INSERT INTO turns VALUES ('running'),('completed');"
                "INSERT INTO jobs VALUES ('queued');"
                "INSERT INTO training_runs VALUES ('succeeded','done');"
            )
            conn.commit()
        finally:
            conn.close()
        before = json.dumps(sorted(serve.active_work(self.data).items()))
        self.assertEqual(
            serve.active_work(self.data), {"对话轮次": 1, "业务作业": 1}
        )
        self.assertEqual(json.dumps(sorted(serve.active_work(self.data).items())), before)


if __name__ == "__main__":
    unittest.main()

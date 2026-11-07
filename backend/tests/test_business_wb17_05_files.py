"""WB17-05：文件治理（写后回读、合并、去重）与受控命令。

真实性说明：

* 回读校验不是"看返回值"，用例会故意让第二次指纹算错（``mock`` 掉 ``fsops.digest``），
  确认 ``FS_VERIFY_FAILED`` 真的会抛出来，而不是"写成功就算成功"；
* 受控命令跑的是**真的 PowerShell**，检查用的是 PowerShell 自己的 AST 解析器；
* 越权用例覆盖变量拼接、动态调用、重定向、切目录、绝对路径、`..`、拉新进程。

一条必须成立的结构性事实：``full`` 权限不会让"范围无法证明"的命令变得可执行。
"""

from __future__ import annotations

import json
import secrets
import unittest
from pathlib import Path
from unittest import mock

from backend.agent import fsops, shell
from backend.errors import ApiError
from backend.agent.context import ToolError
from backend.services import storage

from backend.tests.test_business_wb17_04 import ReviewBase


class FileBase(ReviewBase):
    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _ctx(self, conn, args: dict):
        from backend.agent import identity as agent_identity
        from backend.agent.context import Actor
        from backend.agent.tools import ToolContext

        connection = agent_identity.connection_for(conn, self.alice["id"])
        actor = Actor(
            user_id=int(self.alice["id"]),
            username=str(self.alice["username"]),
            project_id=str(self.project["id"]),
            project_name=str(self.project["name"]),
            session_id=str(self.session["id"]),
            turn_id=f"trn_{secrets.token_hex(8)}",
            mode=agent_identity.effective_mode(conn, self.alice["id"]),
            root_path=self.root,
            connection_fingerprint=agent_identity.connection_fingerprint(connection),
            provider=str(connection["provider"]),
            model=agent_identity.effective_model(conn, self.alice["id"], connection),
        )
        return ToolContext(conn=conn, actor=actor, args=args, emit=lambda *_args: None, jobs={})


# --------------------------------------------------------------------------- 回读校验


class VerifyTest(FileBase):
    def test_write_reports_readback_fingerprint(self):
        result = fsops.write_file(self.root, "notes/a.txt", "热斑阈值 70C\n")
        self.assertTrue(result["verification"]["verified"])
        self.assertEqual(result["verification"]["method"], "exact")
        self.assertEqual((self.root / "notes/a.txt").read_text(encoding="utf-8"), "热斑阈值 70C\n")

    def test_append_verifies_size_and_tail(self):
        fsops.write_file(self.root, "log.txt", "第一行\n", mode="create")
        result = fsops.write_file(self.root, "log.txt", "第二行\n", mode="append")
        self.assertTrue(result["verification"]["verified"])
        self.assertEqual(result["verification"]["bytes"], len("第一行\n第二行\n".encode("utf-8")))
        self.assertEqual((self.root / "log.txt").read_text(encoding="utf-8"), "第一行\n第二行\n")

    def test_write_fails_loudly_when_readback_disagrees(self):
        real = fsops.digest
        calls: list[int] = []

        def flaky(path):
            calls.append(1)
            return {"exists": True, "is_dir": False, "files": 1, "size": 1, "sha256": "deadbeef"}

        with mock.patch.object(fsops, "digest", side_effect=flaky):
            with self.assertRaises(ToolError) as caught:
                fsops.verify_written(self.root / "x.txt", expected_size=10, expected_sha="abc")
        self.assertEqual(caught.exception.code, "FS_VERIFY_FAILED")
        self.assertIs(fsops.digest, real)

    def test_move_and_copy_compare_content_fingerprints(self):
        self.write("a.txt", "同样的内容")
        moved = fsops.move_path(self.root, "a.txt", "b.txt")
        self.assertTrue(moved["verification"]["verified"])
        self.assertFalse((self.root / "a.txt").exists())
        self.assertTrue((self.root / "b.txt").exists())

        copied = fsops.copy_path(self.root, "b.txt", "c.txt")
        self.assertTrue(copied["verification"]["verified"])
        self.assertEqual(
            (self.root / "b.txt").read_bytes(), (self.root / "c.txt").read_bytes()
        )

        # 目录复制也要比整棵树的指纹
        self.write("dir/one.txt", "1")
        self.write("dir/sub/two.txt", "2")
        tree = fsops.copy_path(self.root, "dir", "dir-copy")
        self.assertTrue(tree["verification"]["verified"])
        self.assertEqual(tree["verification"]["files"], 2)

    def test_move_reports_failure_when_verification_cannot_confirm(self):
        self.write("a.txt", "内容")
        real = fsops.digest
        state = {"calls": 0}

        def drift(path):
            state["calls"] += 1
            detail = real(path)
            if state["calls"] >= 2:  # 第二次（目标回读）故意给不同指纹
                detail = {**detail, "sha256": "0" * 64}
            return detail

        with mock.patch.object(fsops, "digest", side_effect=drift):
            with self.assertRaises(ToolError) as caught:
                fsops.move_path(self.root, "a.txt", "b.txt")
        self.assertEqual(caught.exception.code, "FS_VERIFY_FAILED")

    def test_delete_keeps_a_verifiable_copy_in_trash(self):
        path = self.write("junk/one.txt", "要删掉的内容")
        original = path.read_bytes()
        result = fsops.delete_path(self.root, "junk", recursive=True)
        self.assertTrue(result["verification"]["verified"])
        self.assertFalse((self.root / "junk").exists())
        trash = self.root / ".hx17-trash" / result["trash_id"]
        self.assertTrue((trash / "payload" / "one.txt").exists())
        self.assertEqual((trash / "payload" / "one.txt").read_bytes(), original)
        manifest = json.loads((trash / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["original_relative"], "junk")


# --------------------------------------------------------------------------- 合并


class MergeTest(FileBase):
    def test_merge_order_separator_and_source_untouched(self):
        self.write("parts/1.md", "# 一\n")
        self.write("parts/2.md", "# 二\n")
        result = fsops.merge_files(self.root, ["parts/1.md", "parts/2.md"], "merged.md")
        self.assertEqual(result["sources"], ["parts/1.md", "parts/2.md"])
        self.assertTrue(result["verification"]["verified"])
        merged = (self.root / "merged.md").read_text(encoding="utf-8")
        self.assertEqual(merged, "# 一\n\n\n# 二\n")
        self.assertTrue((self.root / "parts/1.md").exists())
        self.assertTrue((self.root / "parts/2.md").exists())

    def test_merge_refuses_overwrite_by_default(self):
        self.write("a.txt", "a")
        self.write("b.txt", "b")
        self.write("out.txt", "旧内容")
        with self.assertRaises(ToolError) as caught:
            fsops.merge_files(self.root, ["a.txt", "b.txt"], "out.txt")
        self.assertEqual(caught.exception.code, "DEST_EXISTS")
        self.assertEqual((self.root / "out.txt").read_text(encoding="utf-8"), "旧内容")

    def test_merge_rejects_binary_and_single_source(self):
        (self.root / "bin.dat").write_bytes(b"\xff\xfe\x00\x01")
        self.write("a.txt", "a")
        with self.assertRaises(ToolError) as caught:
            fsops.merge_files(self.root, ["a.txt", "bin.dat"], "out.txt")
        self.assertEqual(caught.exception.code, "NOT_TEXT")
        with self.assertRaises(ToolError) as single:
            fsops.merge_files(self.root, ["a.txt"], "out.txt")
        self.assertEqual(single.exception.code, "VALIDATION")


# --------------------------------------------------------------------------- 去重


class DuplicateTest(FileBase):
    def setUp(self):
        super().setUp()
        body = "同样的巡检记录内容\n" * 4
        self.write("set/a.txt", body)
        self.write("set/copy-of-a.txt", body)
        self.write("set/other.txt", "不一样的内容\n")
        self.write("set/nested/again.txt", body)

    def test_find_duplicates_groups_by_content(self):
        result = fsops.find_duplicates(self.root, "set")
        self.assertEqual(result["total_groups"], 1)
        group = result["groups"][0]
        self.assertEqual(group["count"], 3)
        self.assertEqual(
            sorted(group["files"]),
            ["set/a.txt", "set/copy-of-a.txt", "set/nested/again.txt"],
        )
        self.assertGreater(result["reclaimable_bytes"], 0)
        self.assertIn("不删除任何文件", result["note"])
        # 只报告：文件都还在
        self.assertEqual(len(list((self.root / "set").rglob("*.txt"))), 4)

    def test_dry_run_moves_nothing(self):
        result = fsops.remove_duplicates(self.root, "set", dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(len(result["kept"]), 1)
        self.assertEqual(len(result["removed"]), 0)
        self.assertEqual(len(list((self.root / "set").rglob("*.txt"))), 4)

    def test_remove_keeps_one_and_moves_the_rest_to_trash(self):
        result = fsops.remove_duplicates(self.root, "set")
        self.assertEqual(len(result["kept"]), 1)
        kept = self.root / result["kept"][0]
        self.assertTrue(kept.exists())
        self.assertEqual(len(result["removed"]), 2)
        for item in result["removed"]:
            self.assertFalse((self.root / item["relative"]).exists())
            moved = self.root / item["trash_relative"]
            self.assertTrue(moved.exists())
            self.assertEqual(moved.read_bytes(), kept.read_bytes())
        self.assertTrue(result["trash_relative"].startswith(".hx17-trash/dedup-"))
        self.assertTrue((self.root / result["trash_relative"] / "manifest.json").exists())
        remaining = sorted(item.name for item in (self.root / "set").rglob("*.txt"))
        # 唯一内容的那份不受影响；重复的三份只剩保留的一份
        self.assertEqual(remaining, ["a.txt", "other.txt"])


# --------------------------------------------------------------------------- 受控命令


class ShellCheckTest(FileBase):
    def test_allowed_read_pipeline_runs_for_real(self):
        self.write("notes.txt", "热斑 阈值 70C\n")
        report = shell.run(self.root, 'Get-Content notes.txt | Select-String -Pattern "热斑"')
        self.assertEqual(report["exit_code"], 0)
        self.assertIn("热斑", report["stdout"])
        self.assertIn("不是沙箱", report["note"])
        self.assertEqual(report["paths"], ["notes.txt"])

    def test_absolute_and_parent_paths_are_refused(self):
        outside = self.harness.workspace_dir / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        for script, code in [
            (f'Get-Content "{outside}"', "SHELL_SCOPE_UNPROVEN"),
            ("Get-Content ../outside.txt", "SHELL_SCOPE_UNPROVEN"),
            ("Get-ChildItem -Path C:/Windows", "SHELL_SCOPE_UNPROVEN"),
        ]:
            with self.assertRaises(ApiError) as caught:
                shell.run(self.root, script)
            error = caught.exception
            self.assertEqual(error.code, code, script)

    def test_variables_and_dynamic_invocation_are_refused(self):
        self.write("a.txt", "x")
        cases = [
            '$p = "a.txt"; Get-Content $p',
            'Invoke-Expression "Get-Content a.txt"',
            '& ("Get-" + "Content") a.txt',
            "Get-Content a.txt | ForEach-Object { Get-Content a.txt }",
            'Get-Content (Get-Content a.txt)',
        ]
        for script in cases:
            with self.assertRaises(ApiError) as caught:
                shell.run(self.root, script)
            self.assertEqual(getattr(caught.exception, "code", ""), "SHELL_SCOPE_UNPROVEN", script)

    def test_redirection_and_state_changing_cmdlets_are_refused(self):
        self.write("a.txt", "x")
        for script, hint in [
            ("Get-Content a.txt > out.txt", "重定向"),
            ("Set-Location ..", "切换工作目录"),
            ("Remove-Item a.txt", "hx_delete_path"),
            ("Start-Process calc.exe", "拉新进程"),
            ("Invoke-WebRequest https://example.com", "访问网络"),
            ("Set-ExecutionPolicy Bypass", "执行策略"),
        ]:
            with self.assertRaises(ApiError) as caught:
                shell.run(self.root, script)
            self.assertEqual(getattr(caught.exception, "code", ""), "SHELL_SCOPE_UNPROVEN", script)
            self.assertIn(hint, str(caught.exception))

    def test_full_mode_does_not_bypass_scope_check(self):
        from backend.agent import store as agent_store

        conn = self.conn()
        try:
            agent_store.save_prefs(conn, self.alice["id"], mode="full", model=None)
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(Exception) as caught:
            shell.run(self.root, "Get-ChildItem -Path C:/Windows")
        self.assertEqual(getattr(caught.exception, "code", ""), "SHELL_SCOPE_UNPROVEN")

    def test_timeout_kills_the_process(self):
        with self.assertRaises(Exception) as caught:
            shell.run(self.root, "Start-Sleep -Seconds 30", timeout=1.0)
        self.assertEqual(getattr(caught.exception, "code", ""), "SHELL_TIMEOUT")

    def test_missing_interpreter_is_reported(self):
        with mock.patch.object(shell, "_powershell", return_value=None):
            with self.assertRaises(ApiError) as caught:
                shell.analyze(self.root, "Get-ChildItem")
        self.assertEqual(getattr(caught.exception, "code", ""), "SHELL_UNAVAILABLE")

    def test_write_cmdlets_inside_the_project_are_allowed(self):
        self.write("a.txt", "一")
        report = shell.run(self.root, 'Copy-Item -Path a.txt -Destination b.txt')
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual((self.root / "b.txt").read_text(encoding="utf-8"), "一")


# --------------------------------------------------------------------------- 工具层与原生能力


class ToolScopeTest(FileBase):
    def test_file_tools_carry_verification(self):
        from backend.agent import tools as agent_tools

        conn = self.conn()
        try:
            written = agent_tools.TOOLS["hx_write_file"].handler(
                self._ctx(conn, {"path": "memo.txt", "content": "热斑 3 处", "mode": "create"})
            )
            self.assertTrue(written["verification"]["verified"])
            merged = agent_tools.TOOLS["hx_merge_files"].handler(
                self._ctx(conn, {"sources": ["memo.txt", "memo.txt"], "dest": "merged.txt"})
            )
            self.assertEqual(merged["sources"], ["memo.txt", "memo.txt"])
            self.assertTrue(merged["verification"]["verified"])
            found = agent_tools.TOOLS["hx_find_duplicates"].handler(self._ctx(conn, {"path": "."}))
            self.assertGreaterEqual(found["total_groups"], 0)
            command = agent_tools.TOOLS["hx_run_command"].handler(
                self._ctx(conn, {"script": "Get-ChildItem -Path . | Measure-Object"})
            )
            self.assertEqual(command["exit_code"], 0)
            self.assertIn("Count", command["stdout"])
        finally:
            conn.close()

    def test_command_tool_refuses_unproven_scope(self):
        from backend.agent import tools as agent_tools

        conn = self.conn()
        try:
            with self.assertRaises(ToolError) as caught:
                agent_tools.TOOLS["hx_run_command"].handler(
                    self._ctx(conn, {"script": "Get-Content C:/Windows/win.ini"})
                )
            self.assertEqual(caught.exception.code, "SHELL_SCOPE_UNPROVEN")
        finally:
            conn.close()

    def test_no_arbitrary_sql_or_raw_shell_in_catalogue(self):
        from backend.agent import tools as agent_tools

        names = sorted(agent_tools.TOOLS)
        self.assertFalse([name for name in names if "sql" in name.lower()])
        self.assertFalse([name for name in names if name in {"hx_exec", "hx_shell", "hx_bash"}])
        # 唯一能跑命令的入口就是受控的那一个
        self.assertEqual([name for name in names if "command" in name], ["hx_run_command"])
        # 描述里不能出现"无限制/任意目录/任意命令"这类承诺
        for spec in agent_tools.TOOLS.values():
            for word in ("无限制", "任意目录", "任意命令"):
                self.assertNotIn(word, spec.description, spec.name)

    def test_native_engine_tools_are_denied_and_only_bridge_is_allowed(self):
        from backend.agent import engine

        denied = engine._DENIED_PERMISSIONS
        for name in ("bash", "edit", "read", "glob", "grep", "webfetch", "external_directory"):
            self.assertEqual(denied.get(name), "deny", name)
        self.assertEqual(denied.get("hx17_*"), "allow")
        # 原生能力被关掉之后，能用的就只有注册过的工具：数量对得上清单。
        self.assertGreater(len(denied), 10)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

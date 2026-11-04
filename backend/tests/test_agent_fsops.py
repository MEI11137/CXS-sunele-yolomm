"""受控文件操作：越界、链接逃逸、二进制、分页、覆盖与回收。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from backend.agent import fsops
from backend.agent.context import ToolError


class FileOpsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hx17-fs-")
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir(parents=True)
        self.outside = Path(self._tmp.name) / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("top-secret", encoding="utf-8")
        self._private_backup = fsops._private_roots()

    def tearDown(self):
        fsops.configure_private(self._private_backup)
        self._tmp.cleanup()

    # ------------------------------------------------------------------ 路径

    def test_rejects_parent_traversal(self):
        with self.assertRaises(ToolError) as ctx:
            fsops.resolve(self.root, "../outside/secret.txt")
        self.assertEqual(ctx.exception.code, "PATH_OUTSIDE_PROJECT")

    def test_rejects_absolute_path_outside(self):
        with self.assertRaises(ToolError) as ctx:
            fsops.resolve(self.root, str(self.outside / "secret.txt"))
        self.assertEqual(ctx.exception.code, "PATH_OUTSIDE_PROJECT")

    def test_rejects_unc_and_device_paths(self):
        for raw in ("\\\\server\\share\\x", "\\\\.\\C:\\Windows", "//server/share"):
            with self.assertRaises(ToolError) as ctx:
                fsops.resolve(self.root, raw)
            self.assertIn(ctx.exception.code, {"PATH_UNC", "PATH_DEVICE"})

    def test_rejects_drive_relative(self):
        with self.assertRaises(ToolError) as ctx:
            fsops.resolve(self.root, "C:temp")
        self.assertEqual(ctx.exception.code, "PATH_RELATIVE_DRIVE")

    def test_symlink_escape_is_rejected(self):
        link = self.root / "escape"
        try:
            os.symlink(str(self.outside), str(link), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("当前环境不允许创建符号链接")
        with self.assertRaises(ToolError) as ctx:
            fsops.read_file(self.root, "escape/secret.txt")
        self.assertEqual(ctx.exception.code, "PATH_OUTSIDE_PROJECT")

    def test_junction_escape_is_rejected(self):
        if os.name != "nt":
            self.skipTest("仅在 Windows 上验证 NTFS 联接")
        import subprocess

        link = self.root / "junction"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(self.outside)],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest("当前环境无法创建目录联接")
        with self.assertRaises(ToolError) as ctx:
            fsops.resolve(self.root, "junction/secret.txt")
        self.assertEqual(ctx.exception.code, "PATH_OUTSIDE_PROJECT")
        listing = fsops.list_dir(self.root, ".")
        self.assertNotIn("junction", {item["name"] for item in listing["entries"]})

    def test_listing_hides_links_pointing_outside(self):
        link = self.root / "escape"
        created = True
        try:
            os.symlink(str(self.outside), str(link), target_is_directory=True)
        except (OSError, NotImplementedError):
            created = False
        result = fsops.list_dir(self.root, ".")
        names = {item["name"] for item in result["entries"]}
        if created:
            self.assertNotIn("escape", names)
            self.assertGreaterEqual(result["blocked_links"], 1)

    # ------------------------------------------------------------------ 读

    def test_read_text_and_paginate(self):
        target = self.root / "note.txt"
        # 显式 newline="" 才是真字节内容；write_text 默认会把 \n 转成 \r\n。
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write("第一行\n第二行\n第三行\n")
        first = fsops.read_file(self.root, "note.txt", offset=0, limit=10)
        self.assertEqual(first["kind"], "text")
        self.assertFalse(first["eof"])
        second = fsops.read_file(self.root, "note.txt", offset=first["next_offset"], limit=100)
        self.assertTrue(second["eof"])
        self.assertEqual(first["text"] + second["text"], target.read_text(encoding="utf-8"))

    def test_default_read_of_large_file_returns_metadata_only(self):
        target = self.root / "big.txt"
        target.write_text("a" * (fsops.MAX_TEXT_BYTES + 10), encoding="utf-8")
        result = fsops.read_file(self.root, "big.txt")
        self.assertEqual(result["kind"], "large")
        self.assertEqual(result["text"], "")
        self.assertIn("offset", result["hint"])

    def test_binary_file_returns_metadata_not_content(self):
        target = self.root / "image.png"
        target.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00")
        result = fsops.read_file(self.root, "image.png")
        self.assertEqual(result["kind"], "binary")
        self.assertEqual(result["text"], "")

    def test_read_missing_file(self):
        with self.assertRaises(ToolError) as ctx:
            fsops.read_file(self.root, "nope.txt")
        self.assertEqual(ctx.exception.code, "PATH_NOT_FOUND")

    # ------------------------------------------------------------------ 写

    def test_write_create_then_refuse_overwrite(self):
        created = fsops.write_file(self.root, "a.txt", "hello", mode="create")
        self.assertTrue(created["created"])
        with self.assertRaises(ToolError) as ctx:
            fsops.write_file(self.root, "a.txt", "again", mode="create")
        self.assertEqual(ctx.exception.code, "FILE_EXISTS")
        updated = fsops.write_file(self.root, "a.txt", "again", mode="overwrite")
        self.assertFalse(updated["created"])
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "again")

    def test_write_creates_parent_directories_inside_root(self):
        fsops.write_file(self.root, "sub/dir/file.txt", "x")
        self.assertTrue((self.root / "sub" / "dir" / "file.txt").is_file())

    def test_write_append(self):
        fsops.write_file(self.root, "a.txt", "1", mode="create")
        fsops.write_file(self.root, "a.txt", "2", mode="append")
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "12")

    def test_write_outside_root_is_refused(self):
        with self.assertRaises(ToolError):
            fsops.write_file(self.root, "../outside/new.txt", "x")
        self.assertFalse((self.outside / "new.txt").exists())

    # ------------------------------------------------------------------ 移动 / 删除

    def test_move_requires_explicit_overwrite(self):
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        (self.root / "b.txt").write_text("b", encoding="utf-8")
        with self.assertRaises(ToolError) as ctx:
            fsops.move_path(self.root, "a.txt", "b.txt")
        self.assertEqual(ctx.exception.code, "DEST_EXISTS")
        moved = fsops.move_path(self.root, "a.txt", "b.txt", overwrite=True)
        self.assertTrue(moved["overwrote"])
        self.assertEqual((self.root / "b.txt").read_text(encoding="utf-8"), "a")

    def test_move_into_own_subdirectory_is_refused(self):
        (self.root / "dir").mkdir()
        with self.assertRaises(ToolError) as ctx:
            fsops.move_path(self.root, "dir", "dir/inner")
        self.assertIn(ctx.exception.code, {"MOVE_INTO_SELF", "DEST_PARENT_MISSING"})

    def test_delete_moves_to_trash_not_unlink(self):
        target = self.root / "gone.txt"
        target.write_text("keep me", encoding="utf-8")
        result = fsops.delete_path(self.root, "gone.txt")
        self.assertFalse(target.exists())
        self.assertTrue(result["recoverable"])
        # 回收区在项目工作区内，且带清单；不是把业务文件丢到代码目录。
        entry = self.root / ".hx17-trash" / result["trash_id"]
        self.assertTrue((entry / "payload").is_file())
        self.assertEqual((entry / "payload").read_text(encoding="utf-8"), "keep me")
        manifest = (entry / "manifest.json").read_text(encoding="utf-8")
        self.assertIn("gone.txt", manifest)

    def test_delete_root_is_refused(self):
        with self.assertRaises(ToolError) as ctx:
            fsops.delete_path(self.root, ".")
        self.assertEqual(ctx.exception.code, "CANNOT_DELETE_ROOT")

    def test_delete_non_empty_directory_requires_recursive(self):
        (self.root / "dir").mkdir()
        (self.root / "dir" / "x.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(ToolError) as ctx:
            fsops.delete_path(self.root, "dir")
        self.assertEqual(ctx.exception.code, "DIRECTORY_NOT_EMPTY")
        fsops.delete_path(self.root, "dir", recursive=True)
        self.assertFalse((self.root / "dir").exists())

    # ------------------------------------------------------------------ 搜索

    def test_search_by_name_is_bounded_to_root(self):
        (self.root / "a").mkdir()
        (self.root / "a" / "needle.txt").write_text("x", encoding="utf-8")
        result = fsops.search(self.root, "needle")
        self.assertEqual(len(result["hits"]), 1)
        self.assertTrue(result["hits"][0]["relative"].endswith("needle.txt"))

    def test_search_query_too_short(self):
        with self.assertRaises(ToolError) as ctx:
            fsops.search(self.root, "a")
        self.assertEqual(ctx.exception.code, "QUERY_TOO_SHORT")


class CursorAndSafetyTest(unittest.TestCase):
    """分页一致性、目标语义、复制与私有目录保护。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hx17-fs2-")
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir(parents=True)
        self._private_backup = fsops._private_roots()

    def tearDown(self):
        fsops.configure_private(self._private_backup)
        self._tmp.cleanup()

    def test_cursor_pagination_does_not_skip_mixed_entries(self):
        # 目录排在前、文件排在后：只看 name 的旧游标会在这里丢项。
        for name in ("zdir", "adir"):
            (self.root / name).mkdir()
        for name in ("afile.txt", "bfile.txt", "cfile.txt"):
            (self.root / name).write_text("x", encoding="utf-8")
        seen: list[str] = []
        cursor = ""
        for _ in range(10):
            page = fsops.list_dir(self.root, ".", limit=2, cursor=cursor)
            seen.extend(item["name"] for item in page["entries"])
            if not page["truncated"] or not page["next_cursor"]:
                break
            cursor = page["next_cursor"]
        self.assertEqual(
            sorted(seen), sorted(["zdir", "adir", "afile.txt", "bfile.txt", "cfile.txt"])
        )
        self.assertEqual(len(seen), len(set(seen)))

    def test_bad_cursor_is_rejected(self):
        with self.assertRaises(ToolError) as ctx:
            fsops.list_dir(self.root, ".", cursor="not-a-cursor")
        self.assertEqual(ctx.exception.code, "BAD_CURSOR")

    def test_move_into_existing_directory_is_explicit_error(self):
        (self.root / "dst").mkdir()
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        with self.assertRaises(ToolError) as ctx:
            fsops.move_path(self.root, "a.txt", "dst")
        self.assertEqual(ctx.exception.code, "DEST_IS_DIRECTORY")
        # 不能悄悄变成 dst/a.txt，也不能报告一个不存在的路径。
        self.assertTrue((self.root / "a.txt").is_file())
        self.assertEqual(list((self.root / "dst").iterdir()), [])

    def test_move_root_is_refused(self):
        with self.assertRaises(ToolError) as ctx:
            fsops.move_path(self.root, ".", "moved")
        self.assertEqual(ctx.exception.code, "CANNOT_MOVE_ROOT")

    def test_copy_file_and_directory(self):
        (self.root / "a.txt").write_text("hello", encoding="utf-8")
        (self.root / "dir").mkdir()
        (self.root / "dir" / "inner.txt").write_text("inner", encoding="utf-8")
        copied = fsops.copy_path(self.root, "a.txt", "b.txt")
        self.assertEqual(copied["files"], 1)
        self.assertEqual((self.root / "b.txt").read_text(encoding="utf-8"), "hello")
        tree = fsops.copy_path(self.root, "dir", "dir2")
        self.assertEqual(tree["files"], 1)
        self.assertEqual((self.root / "dir2" / "inner.txt").read_text(encoding="utf-8"), "inner")

    def test_private_roots_are_invisible_and_unwritable(self):
        private = self.root / "server-private"
        private.mkdir()
        (private / "haixun.db").write_text("secret", encoding="utf-8")
        fsops.configure_private([private])
        listing = fsops.list_dir(self.root, ".")
        self.assertNotIn("server-private", {item["name"] for item in listing["entries"]})
        with self.assertRaises(ToolError) as ctx:
            fsops.read_file(self.root, "server-private/haixun.db")
        self.assertEqual(ctx.exception.code, "PATH_PRIVATE")
        with self.assertRaises(ToolError) as ctx:
            fsops.write_file(self.root, "server-private/new.txt", "x")
        self.assertEqual(ctx.exception.code, "PATH_PRIVATE")

    def test_private_root_cannot_be_project_root(self):
        private = Path(self._tmp.name) / "private-root"
        private.mkdir()
        fsops.configure_private([private])
        with self.assertRaises(ToolError) as ctx:
            fsops.require_root(private)
        self.assertEqual(ctx.exception.code, "ROOT_PRIVATE")

    def test_windows_ads_reserved_and_trailing_are_rejected(self):
        cases = {
            "file.txt:secret": "PATH_ADS",
            "CON": "PATH_RESERVED",
            "sub/nul.txt": "PATH_RESERVED",
            "name.": "PATH_TRAILING",
            "name ": "PATH_TRAILING",
        }
        for raw, code in cases.items():
            with self.assertRaises(ToolError) as ctx:
                fsops.resolve(self.root, raw)
            self.assertEqual(ctx.exception.code, code, raw)

    def test_failed_overwrite_keeps_old_content(self):
        target = self.root / "keep.txt"
        target.write_text("original", encoding="utf-8")
        original_open = fsops.os.replace
        fsops.os.replace = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom"))
        try:
            with self.assertRaises(ToolError) as ctx:
                fsops.write_file(self.root, "keep.txt", "new", mode="overwrite")
        finally:
            fsops.os.replace = original_open
        self.assertEqual(ctx.exception.code, "WRITE_FAILED")
        self.assertEqual(target.read_text(encoding="utf-8"), "original")

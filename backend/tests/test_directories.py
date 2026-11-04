"""目录授权与绑定边界。

验证点：越界拒绝、.. 穿越、链接逃逸、替换需显式确认、浏览只在范围内。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from backend.tests.support import Harness, api, body, login


class DirectoryBindingTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.admin = self.harness.create_user("admin", role="admin")
        self.alice = self.harness.create_user("alice")
        self.root = self.harness.make_dir("allowed")
        (self.root / "site-a").mkdir()
        (self.root / "site-a" / "rgb.jpg").write_bytes(b"fake")
        self.outside = self.harness.make_dir("outside")
        self.harness.add_root(self.root, "测试数据", grant_to=["alice"])
        self.project = self.harness.create_project(self.alice["id"], "巡检")
        self.client = self.harness.client()
        login(self.client, "alice")

    def tearDown(self):
        self.harness.close()

    def _bind(self, path: str, replace: bool = False):
        return api(
            self.client,
            "PUT",
            f"/api/projects/{self.project['id']}/root",
            {"path": path, "replace": replace},
        )

    def test_bind_inside_allowed_root(self):
        target = self.root / "site-a"
        response = self._bind(str(target))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        project = body(response)["data"]["project"]
        self.assertEqual(Path(project["root_path"]), target.resolve())

    def test_bind_outside_allowed_root_rejected(self):
        response = self._bind(str(self.outside))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "OUTSIDE_ALLOWED_ROOTS")

    def test_dotdot_traversal_rejected(self):
        crafted = str(self.root / "site-a" / ".." / ".." / "outside")
        response = self._bind(crafted)
        self.assertEqual(response.status_code, 403)

    def test_drive_relative_path_rejected(self):
        response = self._bind("C:windows")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "BAD_PATH")

    def test_relative_path_rejected(self):
        response = self._bind("site-a")
        self.assertEqual(response.status_code, 400)

    def test_missing_directory_rejected(self):
        response = self._bind(str(self.root / "not-exists"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "PATH_NOT_FOUND")

    def test_file_path_rejected(self):
        response = self._bind(str(self.root / "site-a" / "rgb.jpg"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "NOT_A_DIRECTORY")

    def test_rebind_requires_explicit_replace(self):
        self.assertEqual(self._bind(str(self.root / "site-a")).status_code, 200)
        other = self.root / "site-b"
        other.mkdir()
        conflict = self._bind(str(other))
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()["error"], "ROOT_ALREADY_BOUND")
        # 冲突请求不能改动原绑定。
        project = body(api(self.client, "GET", f"/api/projects/{self.project['id']}"))["data"]["project"]
        self.assertEqual(Path(project["root_path"]), (self.root / "site-a").resolve())

    def test_rebind_with_replace_updates_binding(self):
        self._bind(str(self.root / "site-a"))
        other = self.root / "site-b"
        other.mkdir()
        response = self._bind(str(other), replace=True)
        self.assertEqual(response.status_code, 200)
        project = body(response)["data"]["project"]
        self.assertEqual(Path(project["root_path"]), other.resolve())

    def test_unbind(self):
        self._bind(str(self.root / "site-a"))
        response = api(self.client, "DELETE", f"/api/projects/{self.project['id']}/root")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(body(response)["data"]["project"]["root_path"])

    def _make_escape_link(self):
        """在授权目录里造一个指向外部的链接/联接；不支持时返回 None。"""
        link = self.root / "escape"
        try:
            os.symlink(self.outside, link, target_is_directory=True)
            return link
        except (OSError, NotImplementedError):
            pass
        if os.name == "nt":
            # NTFS 联接不需要管理员权限，但同一台机器上也可能被策略禁掉。
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(self.outside)],
                capture_output=True,
                errors="replace",
            )
            if result.returncode == 0 and link.exists():
                return link
        return None

    def test_link_escape_rejected(self):
        link = self._make_escape_link()
        if link is None:
            self.skipTest("当前环境无法创建符号链接或 NTFS 联接")
        # 真实路径已被解析到授权范围之外，绑定必须失败。
        response = self._bind(str(link))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "OUTSIDE_ALLOWED_ROOTS")

    def test_escape_link_is_hidden_when_browsing(self):
        link = self._make_escape_link()
        if link is None:
            self.skipTest("当前环境无法创建符号链接或 NTFS 联接")
        self._bind(str(self.root))
        data = body(
            api(self.client, "GET", f"/api/projects/{self.project['id']}/root/entries")
        )["data"]
        self.assertNotIn("escape", [item["name"] for item in data["entries"]])
        self.assertGreaterEqual(data["blocked"], 1)

    def test_browse_inside_bound_root(self):
        self._bind(str(self.root))
        response = api(
            self.client,
            "GET",
            f"/api/projects/{self.project['id']}/root/entries",
        )
        self.assertEqual(response.status_code, 200)
        names = [item["name"] for item in body(response)["data"]["entries"]]
        self.assertIn("site-a", names)
        # 授权根之外的目录不会出现在列表里。
        self.assertNotIn("outside", names)

    def test_browse_outside_bound_root_rejected(self):
        self._bind(str(self.root / "site-a"))
        response = api(
            self.client,
            "GET",
            f"/api/projects/{self.project['id']}/root/entries?path={self.root}",
        )
        self.assertEqual(response.status_code, 403)

    def test_browse_without_binding_is_409(self):
        response = api(self.client, "GET", f"/api/projects/{self.project['id']}/root/entries")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "NO_PROJECT_ROOT")


class DirectoryBrowseTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.harness.create_user("alice")
        self.root = self.harness.make_dir("data")
        (self.root / "sub").mkdir()
        self.outside = self.harness.make_dir("elsewhere")
        self.client = self.harness.client()
        login(self.client, "alice")

    def tearDown(self):
        self.harness.close()

    def test_no_root_configured_is_explicit(self):
        response = api(self.client, "GET", "/api/directories/browse")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "NO_ALLOWED_ROOTS")
        self.assertEqual(api(self.client, "GET", "/api/directories/roots").status_code, 200)

    def test_roots_only_lists_configured_directories(self):
        self.harness.add_root(self.root, grant_to=["alice"])
        data = body(api(self.client, "GET", "/api/directories/roots"))["data"]["roots"]
        self.assertEqual([Path(item["path"]) for item in data], [self.root.resolve()])

    def test_browse_roots_then_inside(self):
        self.harness.add_root(self.root, grant_to=["alice"])
        top = body(api(self.client, "GET", "/api/directories/browse"))["data"]
        self.assertEqual(len(top["entries"]), 1)
        inside = body(
            api(self.client, "GET", f"/api/directories/browse?path={self.root}")
        )["data"]
        self.assertIn("sub", [item["name"] for item in inside["entries"]])

    def test_browse_outside_allowed_roots_rejected(self):
        self.harness.add_root(self.root, grant_to=["alice"])
        response = api(self.client, "GET", f"/api/directories/browse?path={self.outside}")
        self.assertEqual(response.status_code, 403)

    def test_browse_traversal_rejected(self):
        self.harness.add_root(self.root, grant_to=["alice"])
        crafted = f"{self.root}{os.sep}..{os.sep}{self.outside.name}"
        response = api(self.client, "GET", f"/api/directories/browse?path={crafted}")
        self.assertEqual(response.status_code, 403)


class PathGuardTest(unittest.TestCase):
    """直接针对路径守卫的边界用例，不依赖 HTTP。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hx17-guard-")
        self.root = Path(self._tmp.name) / "root"
        self.root.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_unc_path_outside_roots_rejected(self):
        if os.name != "nt":
            self.skipTest("UNC 仅 Windows 有效")
        from backend import paths
        from backend.errors import ApiError

        with self.assertRaises(ApiError) as caught:
            paths.validate_binding(r"\\server\share\data", [self.root])
        # 统一拒绝 UNC：范围证明只能基于本地盘符，避免远程路径绕过授权。
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.code, "UNC_NOT_SUPPORTED")

    def test_root_cannot_be_unc(self):
        if os.name != "nt":
            self.skipTest("UNC 仅 Windows 有效")
        from backend import paths
        from backend.errors import ApiError

        with self.assertRaises(ApiError) as caught:
            paths.validate_root(r"\\server\share")
        self.assertEqual(caught.exception.code, "UNC_NOT_SUPPORTED")

    def test_device_prefix_rejected(self):
        from backend import paths
        from backend.errors import ApiError

        with self.assertRaises(ApiError) as caught:
            paths.validate_binding("\\\\?\\C:\\Windows", [self.root])
        self.assertEqual(caught.exception.code, "BAD_PATH")

    def test_drive_root_cannot_be_binding_target(self):
        from backend import paths
        from backend.errors import ApiError

        # 即使有人把磁盘根写成授权根，绑定也会因为根目录不可能是合法授权根而被挡住。
        with self.assertRaises(ApiError) as caught:
            paths.validate_root(Path(self.root.resolve().anchor))
        self.assertEqual(caught.exception.code, "TOO_BROAD")

    def test_commonpath_across_drives_is_false(self):
        from backend import paths

        if os.name != "nt":
            self.skipTest("跨盘判断仅 Windows 有效")
        self.assertFalse(paths.is_within(r"C:\Windows", r"D:\data"))


if __name__ == "__main__":
    unittest.main()

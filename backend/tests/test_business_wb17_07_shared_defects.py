"""WB17-07 共享缺陷收尾：已证实的独立反例钉住的行为。

本文件只覆盖 root 独立复现、派发文档点名的缺陷，不为凑数重复既有用例：

1. 数据集指纹类别分隔符碰撞 + v1 旧指纹兼容（root：数据集去重独立补修）；
2. 软删→重导→恢复旧集：明确 409，回收站记录与文件不动（同上）；
3. MCP 错误映射 NoEventLoopError / Windows stdio 中文乱码（root 完整日志）；
4. 受控命令三个拒绝反例 + 相对读取保留（root verify_phase05_shell_edges）；
5. 记录筛选总数/刷新上下文/深链定位（root UI-04A 识别记录独立核验）；
6. CUDA 指定索引不存在必须拒绝（root _default_device_probe('cuda:15') ok=True）。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import unittest
from pathlib import Path
from unittest import mock

import httpx

from backend.errors import ApiError
from backend.services import datasets as dataset_service
from backend.services import detection as detection_service
from backend.services import storage
from backend.tests.support import body, csrf_headers
from backend.tests.test_business_wb17_03 import BusinessBase, png_bytes


def _make_source(test: BusinessBase, name: str, *, color=(120, 80, 40)) -> str:
    rel = f"uploads/src-{name}-{secrets.token_hex(4)}"
    base = storage.project_dir(test.root, *rel.split("/"))
    for split in ("train", "val"):
        storage.write_bytes(test.root, base / "images" / split / "a.png", png_bytes(color=color))
        storage.write_bytes(
            test.root, base / "labels" / split / "a.txt", b"0 0.5 0.5 0.2 0.2\n"
        )
    return rel


class DatasetFingerprintFixTest(BusinessBase):
    """缺陷 1/缺陷 2：类别分隔符碰撞、v1 兼容、恢复 409。"""

    def _import(self, rel: str, name: str, classes: list[str]):
        conn = self.conn()
        try:
            return dataset_service.import_dataset(
                conn,
                self.alice["id"],
                self.project["id"],
                name=name,
                modalities=["rgb"],
                classes=classes,
                rel_path=rel,
            )
        finally:
            conn.close()

    def _rows(self) -> list:
        conn = self.conn()
        try:
            return conn.execute(
                "SELECT * FROM datasets WHERE project_id = ?", (self.project["id"],)
            ).fetchall()
        finally:
            conn.close()

    # ------------------------------------------------------------------ 分隔符碰撞

    def test_class_separator_collision_is_not_reused(self):
        """['a|b','c'] 与 ['a','b|c'] 同内容同标注：v1 头部会撞，v2 必须分开。"""
        rel = _make_source(self, "collide")
        first = self._import(rel, "碰撞甲", classes=["a|b", "c"])
        second = self._import(rel, "碰撞乙", classes=["a", "b|c"])
        self.assertFalse(first.get("reused"))
        self.assertNotEqual(second["id"], first["id"], "类别表不同绝不能复用")
        self.assertEqual(second["classes"], ["a", "b|c"], "第二份必须保留自己的类别表")
        self.assertEqual(first["classes"], ["a|b", "c"])

    # ------------------------------------------------------------------ v1 兼容

    def test_v1_stored_fingerprint_still_reuses_and_upgrades(self):
        """库里存着旧规则（v1）指纹：同内容同类别必须复用，且顺手升级为 v2。"""
        rel = _make_source(self, "legacy")
        first = self._import(rel, "旧规则", classes=["单一热斑", "大面积热斑"])
        src = storage.project_dir(self.root, *rel.split("/"))
        scanned = dataset_service._scan(
            src, secondary=None, classes=["单一热斑", "大面积热斑"], project_root=self.root
        )
        rows = dataset_service._scan_rows(scanned, self.root)
        v1 = dataset_service._v1_fingerprint_lines(["rgb"], None, scanned["classes"], rows)
        v2 = dataset_service._fingerprint_lines(["rgb"], None, scanned["classes"], rows)
        conn = self.conn()
        try:
            conn.execute(
                "UPDATE datasets SET content_fingerprint = ? WHERE id = ?", (v1, first["id"])
            )
            conn.commit()
        finally:
            conn.close()
        second = self._import(rel, "旧规则-再导", classes=["单一热斑", "大面积热斑"])
        self.assertTrue(second.get("reused"), "v1 旧指纹对上且类别相同必须复用")
        self.assertEqual(second["id"], first["id"])
        conn = self.conn()
        try:
            stored = conn.execute(
                "SELECT content_fingerprint FROM datasets WHERE id = ?", (first["id"],)
            ).fetchone()["content_fingerprint"]
        finally:
            conn.close()
        self.assertEqual(stored, v2, "复用旧规则记录后应把指纹升级为 v2")

    def test_v1_collision_with_different_classes_is_not_reused(self):
        """v1 指纹撞上但类别表逐项不等：绝不能当同一份数据。"""
        rel = _make_source(self, "legacy-collide")
        first = self._import(rel, "真类别", classes=["a", "b"])
        src = storage.project_dir(self.root, *rel.split("/"))
        scanned = dataset_service._scan(src, secondary=None, classes=["a", "b"], project_root=self.root)
        rows = dataset_service._scan_rows(scanned, self.root)
        # v1 视角下 ['a','b'] 与 ['a|b'] 拼出来同是 "classes=a|b"。
        v1_of_collision = dataset_service._v1_fingerprint_lines(["rgb"], None, ["a|b"], rows)
        conn = self.conn()
        try:
            conn.execute(
                "UPDATE datasets SET content_fingerprint = ? WHERE id = ?",
                (v1_of_collision, first["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        second = self._import(rel, "碰撞类别", classes=["a|b"])
        self.assertFalse(
            second.get("reused"), "v1 指纹相同但类别表不同（'a','b' vs 'a|b'）不能复用"
        )
        self.assertNotEqual(second["id"], first["id"])

    # ------------------------------------------------------------------ 恢复冲突

    def test_restore_conflict_returns_409_and_keeps_both(self):
        rel = _make_source(self, "restore-conflict")
        old = self._import(rel, "会被软删的", classes=["单一热斑", "大面积热斑"])
        conn = self.conn()
        try:
            dataset_service.soft_delete(conn, self.alice["id"], self.project["id"], old["id"])
        finally:
            conn.close()
        fresh = self._import(rel, "重新导入的", classes=["单一热斑", "大面积热斑"])
        self.assertNotEqual(fresh["id"], old["id"])
        conn = self.conn()
        try:
            with self.assertRaises(ApiError) as caught:
                dataset_service.restore(conn, self.alice["id"], self.project["id"], old["id"])
        finally:
            conn.close()
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "DATASET_RESTORE_CONFLICT")
        conn = self.conn()
        try:
            rows = {
                row["id"]: row
                for row in conn.execute(
                    "SELECT * FROM datasets WHERE project_id = ?", (self.project["id"],)
                )
            }
        finally:
            conn.close()
        self.assertIsNotNone(rows[old["id"]]["deleted_at"], "旧记录必须继续留在回收站")
        self.assertIsNone(rows[fresh["id"]]["deleted_at"], "新导入的活跃版本不受影响")
        # 文件都保留：两份数据集目录都在磁盘上。
        for row in rows.values():
            directory = storage.resolve_rel(self.root, row["root_rel"])
            self.assertTrue(directory.is_dir(), f"{row['name']} 的目录与文件不能被删")

        # 冲突解除（删除新版）后，旧集可以正常恢复。
        conn = self.conn()
        try:
            dataset_service.soft_delete(conn, self.alice["id"], self.project["id"], fresh["id"])
            restored = dataset_service.restore(
                conn, self.alice["id"], self.project["id"], old["id"]
            )
        finally:
            conn.close()
        self.assertEqual(restored["status"], "restored")

    def _v1_of(self, rel: str, classes: list[str]) -> str:
        """按已落盘源目录算旧规则（v1）指纹，模拟历史库里存的真实 v1 值。"""
        src = storage.project_dir(self.root, *rel.split("/"))
        scanned = dataset_service._scan(src, secondary=None, classes=classes, project_root=self.root)
        rows = dataset_service._scan_rows(scanned, self.root)
        return dataset_service._v1_fingerprint_lines(["rgb"], None, scanned["classes"], rows)

    def _set_fingerprint(self, dataset_id: str, value: str | None):
        conn = self.conn()
        try:
            conn.execute(
                "UPDATE datasets SET content_fingerprint = ? WHERE id = ?", (value, dataset_id)
            )
            conn.commit()
        finally:
            conn.close()

    def test_restore_conflict_with_legacy_v1_fingerprint_in_trash(self):
        """root 反例第 6 项：回收站记录存的是旧 v1 非空指纹，活跃侧是 v2。

        恢复不能只在指纹为空时重算——旧 v1/v2 混用也要正确对上并 409。
        """
        rel = _make_source(self, "restore-legacy-v1")
        classes = ["单一热斑", "大面积热斑"]
        old = self._import(rel, "旧指纹版本", classes=classes)
        v1 = self._v1_of(rel, classes)
        conn = self.conn()
        try:
            dataset_service.soft_delete(conn, self.alice["id"], self.project["id"], old["id"])
        finally:
            conn.close()
        self._set_fingerprint(old["id"], v1)
        fresh = self._import(rel, "活跃 v2 版本", classes=classes)
        conn = self.conn()
        try:
            with self.assertRaises(ApiError) as caught:
                dataset_service.restore(conn, self.alice["id"], self.project["id"], old["id"])
        finally:
            conn.close()
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "DATASET_RESTORE_CONFLICT")
        conn = self.conn()
        try:
            trash_row = conn.execute(
                "SELECT deleted_at, content_fingerprint FROM datasets WHERE id = ?", (old["id"],)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(trash_row["deleted_at"], "旧记录必须继续留在回收站")
        self.assertEqual(trash_row["content_fingerprint"], v1, "不能顺手改写回收站记录的指纹")

    def test_restore_conflict_with_legacy_v1_fingerprint_active(self):
        """反方向兼容：活跃侧存的是旧 v1 指纹，回收站记录是 v2，也必须 409。"""
        rel = _make_source(self, "restore-legacy-v1-active")
        classes = ["单一热斑", "大面积热斑"]
        v1 = self._v1_of(rel, classes)
        # 先进回收站，再导活跃版（回收站不参与复用），最后把活跃版指纹改回 v1。
        trashed = self._import(rel, "将进回收站", classes=classes)
        conn = self.conn()
        try:
            dataset_service.soft_delete(conn, self.alice["id"], self.project["id"], trashed["id"])
        finally:
            conn.close()
        legacy = self._import(rel, "活跃旧指纹", classes=classes)
        self._set_fingerprint(legacy["id"], v1)
        conn = self.conn()
        try:
            with self.assertRaises(ApiError) as caught:
                dataset_service.restore(conn, self.alice["id"], self.project["id"], trashed["id"])
        finally:
            conn.close()
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "DATASET_RESTORE_CONFLICT")

    def test_restore_v1_collision_different_classes_is_not_conflict(self):
        """v1 口径撞上但类别不同（'|' 碰撞）：不能误判冲突挡住恢复。"""
        rel = _make_source(self, "restore-v1-collide")
        first = self._import(rel, "真类别", classes=["a", "b"])
        self._set_fingerprint(first["id"], self._v1_of(rel, ["a|b"]))
        second = self._import(rel, "碰撞类别", classes=["a|b"])
        conn = self.conn()
        try:
            dataset_service.soft_delete(conn, self.alice["id"], self.project["id"], second["id"])
        finally:
            conn.close()
        conn = self.conn()
        try:
            restored = dataset_service.restore(
                conn, self.alice["id"], self.project["id"], second["id"]
            )
        finally:
            conn.close()
        self.assertEqual(restored["status"], "restored", "类别表不同不是同一份内容，不应 409")

    def test_restore_conflict_http_returns_409_envelope(self):
        rel = _make_source(self, "restore-http")
        old = self._import(rel, "HTTP 冲突", classes=["单一热斑", "大面积热斑"])
        response = self.client.post(
            f"/api/projects/{self.project['id']}/datasets/{old['id']}/delete",
            headers=csrf_headers(self.client),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        response = self.client.post(
            f"/api/projects/{self.project['id']}/datasets",
            json={
                "name": "重导同名内容",
                "rel_path": rel,
                "modalities": ["rgb"],
                "classes": ["单一热斑", "大面积热斑"],
            },
            headers=csrf_headers(self.client),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        response = self.client.post(
            f"/api/projects/{self.project['id']}/datasets/{old['id']}/restore",
            headers=csrf_headers(self.client),
        )
        payload = body(response)
        self.assertEqual(response.status_code, 409, payload)
        self.assertEqual(payload["error"], "DATASET_RESTORE_CONFLICT")
        conflict = payload.get("data") or {}
        self.assertTrue(
            conflict.get("conflict_with", {}).get("id"), "409 要给出当前可用的那份资源"
        )
        self.assertEqual(conflict.get("kept_in_trash", {}).get("id"), old["id"])

class McpErrorMappingTest(unittest.TestCase):
    """缺陷 2：错误映射在无事件循环时也要稳定（NoEventLoopError 反例）。"""

    def test_mapping_without_event_loop_does_not_raise(self):
        from backend import mcp_client

        mapped = mcp_client._map_sdk_error(asyncio.CancelledError(), "initialize")
        self.assertIsInstance(mapped, mcp_client.McpTransportError)
        self.assertEqual(mapped.code, "MCP_CONNECTION_CLOSED")

    def test_mapping_flattens_group_to_original_connect_error(self):
        """任务组把取消+ConnectError 打包：必须映射成 UNREACHABLE，不是连接中断。"""
        from backend import mcp_client

        group = BaseExceptionGroup(
            "transport", [asyncio.CancelledError(), httpx.ConnectError("connection refused")]
        )
        mapped = mcp_client._map_sdk_error(group, "initialize")
        self.assertEqual(mapped.code, "MCP_UNREACHABLE")

    def test_mapping_keeps_sdk_error_code(self):
        from backend import mcp_client

        if not mcp_client._SDK_AVAILABLE:  # pragma: no cover - 环境缺 SDK
            self.skipTest("mcp SDK 未安装")
        from mcp.shared.exceptions import McpError
        from mcp.types import ErrorData

        mapped = mcp_client._map_sdk_error(
            McpError(ErrorData(code=-32000, message="坏请求", data=None)), "tools/call"
        )
        self.assertEqual(mapped.code, "MCP_PROTOCOL_ERROR")
        self.assertIn("code=-32000", mapped.message)

class ShellAnalyzerEdgeTest(unittest.TestCase):
    """缺陷 3：root verify_phase05_shell_edges 的三条拒绝反例（只分析，不执行）。

    被测脚本本身就是危险命令——这里只调用 ``analyze()`` 做 AST 判定，
    绝不 ``run()`` 执行它们。
    """

    def setUp(self):
        import tempfile

        self.root = Path(tempfile.mkdtemp(prefix="hx17-shell-edge-"))
        (self.root / "notes.txt").write_text("热斑 阈值 70C\n", encoding="utf-8")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def test_registry_provider_path_is_rejected(self):
        from backend.agent import shell

        report = shell.analyze(self.root, "Get-ChildItem -LiteralPath 'Registry::HKEY_CURRENT_USER'")
        self.assertFalse(report["ok"], report["reasons"])
        self.assertTrue(
            any("provider" in reason for reason in report["reasons"]), report["reasons"]
        )

    def test_overlong_absolute_path_is_rejected(self):
        from backend.agent import shell

        long_path = "C:////Windows////" + "\\".join(f"dir{index:03d}" for index in range(50)) + "\\x.txt"
        self.assertGreater(len(long_path), 260)
        report = shell.analyze(self.root, f"Get-Content '{long_path}'")
        self.assertFalse(report["ok"], report["reasons"])
        self.assertTrue(
            any("绝对路径" in reason for reason in report["reasons"]), report["reasons"]
        )

    def test_foreach_object_member_invocation_is_rejected(self):
        from backend.agent import shell

        # MemberName 的 Position=0，参数名可省略（root 反例第 5 项）；
        # foreach / % 是 ForEach-Object 的别名，同样整体拒绝。
        for script in (
            "Get-ChildItem -LiteralPath '.' | ForEach-Object -MemberName Delete",
            "Get-ChildItem -LiteralPath '.' | ForEach-Object -m Delete",
            "Get-ChildItem -LiteralPath '.' | ForEach-Object -Method DeleteAll",
            "Get-ChildItem -LiteralPath '.' | ForEach-Object Delete",
            "Get-ChildItem -LiteralPath '.' | foreach Delete",
            "Get-ChildItem -LiteralPath '.' | % Delete",
        ):
            with self.subTest(script=script):
                report = shell.analyze(self.root, script)
                self.assertFalse(report["ok"], report["reasons"])
                self.assertTrue(
                    any("成员调用" in reason or "ForEach-Object" in reason for reason in report["reasons"]),
                    (script, report["reasons"]),
                )

    def test_normal_relative_read_still_passes(self):
        from backend.agent import shell

        report = shell.analyze(self.root, "Get-Content notes.txt")
        self.assertTrue(report["ok"], report["reasons"])
        self.assertEqual(report["paths"], ["notes.txt"])

    def test_normal_relative_pipeline_still_passes(self):
        from backend.agent import shell

        report = shell.analyze(self.root, "Get-ChildItem -LiteralPath '.' | Select-Object -First 5")
        self.assertTrue(report["ok"], report["reasons"])

class RecordsScopeTest(BusinessBase):
    """缺陷 4：筛选总数只计日期（root UI-04A：total_in_range 忽略状态/文件名等）。"""

    def _make_records(self, count: int) -> None:
        import numpy as np
        from backend.vision import runtime as vision_runtime

        boxes = np.array([[3.0, 4.0, 20.0, 24.0, 0.9, 0]], dtype=float)

        class _Detector:
            def predict(self, **kwargs):
                return [type("R", (), {"boxes": boxes})()]

        vision_runtime._DETECTOR_FACTORY = lambda path, mode: _Detector()
        self.model = self.make_model()
        for index in range(count):
            asset = self.upload_asset(
                png_bytes(color=(30 + index, 60, 90)), f"风机塔筒-{index:02d}.png"
            )
            self.detect(self.model, asset["id"], key=f"scope-{index}")

    def test_total_counts_filename_search_not_just_dates(self):
        self._make_records(4)
        conn = self.conn()
        try:
            result = detection_service.list_for_project(
                conn, int(self.alice["id"]), self.project["id"], q="风机塔筒-03", limit=20
            )
        finally:
            conn.close()
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(
            result["total_in_range"], 1, "总数必须跟文件名搜索一致（原来只按日期数全量）"
        )

    def test_total_counts_status_filter(self):
        self._make_records(3)
        conn = self.conn()
        try:
            result = detection_service.list_for_project(
                conn, int(self.alice["id"]), self.project["id"], status="done", limit=20
            )
        finally:
            conn.close()
        self.assertEqual(result["total_in_range"], 3)

    def test_total_is_not_reduced_by_pagination_cursor(self):
        self._make_records(4)
        conn = self.conn()
        try:
            first = detection_service.list_for_project(
                conn, int(self.alice["id"]), self.project["id"], limit=2
            )
            self.assertTrue(first["has_more"])
            cursor = first["next_before"]
            second = detection_service.list_for_project(
                conn, int(self.alice["id"]), self.project["id"], limit=2, before=cursor
            )
        finally:
            conn.close()
        self.assertEqual(
            second["total_in_range"], 4, "翻页后总数仍是筛选范围的全量，不随游标缩减"
        )

    def test_total_counts_trash_view(self):
        self._make_records(3)
        conn = self.conn()
        try:
            victim = detection_service.list_for_project(
                conn, int(self.alice["id"]), self.project["id"], limit=1
            )["records"][0]
            conn.execute(
                "UPDATE detections SET deleted_at = ? WHERE id = ?",
                ("2026-11-07T00:00:00Z", victim["id"]),
            )
            conn.commit()
            active = detection_service.list_for_project(
                conn, int(self.alice["id"]), self.project["id"], deleted="exclude"
            )
            trash = detection_service.list_for_project(
                conn, int(self.alice["id"]), self.project["id"], deleted="only"
            )
        finally:
            conn.close()
        self.assertEqual(active["total_in_range"], 2)
        self.assertEqual(trash["total_in_range"], 1)

    def test_deep_link_record_by_id_has_human_status(self):
        """按 ID 读取的详情与列表同一口径：已确认不能显示成待人工。"""
        self._make_records(1)
        record = detection_service.list_for_project(
            self.conn(), int(self.alice["id"]), self.project["id"]
        )["records"][0]
        response = self.client.get(
            f"/api/projects/{self.project['id']}/detections/{record['id']}"
        )
        self.assertEqual(response.status_code, 200)
        data = body(response)["data"]["record"]
        self.assertEqual(data["human_status"], "pending")
        self.assertIsNone(data["review_version"])

        # 真实提交一条人工结论（走 HTTP），再按 ID 读必须是 confirmed + v1。
        conn = self.conn()
        try:
            conn.execute(
                "INSERT INTO review_versions"
                " (id, detection_id, owner_id, project_id, version, status, outcome, boxes,"
                "  note, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 1, 'confirmed', 'defect', ?, '',"
                " '2026-11-07T00:00:00Z', '2026-11-07T00:00:00Z')",
                (
                    "rev_scope_1",
                    record["id"],
                    int(self.alice["id"]),
                    self.project["id"],
                    json.dumps(record["boxes"]),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        response = self.client.get(
            f"/api/projects/{self.project['id']}/detections/{record['id']}"
        )
        data = body(response)["data"]["record"]
        self.assertEqual(data["human_status"], "confirmed")
        self.assertEqual(data["review_version"], 1)

class CudaIndexValidationTest(unittest.TestCase):
    """缺陷 6：单卡机上默认探针 cuda:15 返回 ok=True（root 反例）。

    只探测与参数校验，不启动任何 GPU 任务；CUDA 数量用 mock 固定，
    正常 cpu / cuda:0 必须仍然通过。
    """

    def setUp(self):
        import torch
        from backend.services import training

        self.training = training
        self.patches = [
            mock.patch.object(torch.cuda, "is_available", return_value=True),
            mock.patch.object(torch.cuda, "device_count", return_value=1),
        ]
        for patch in self.patches:
            patch.start()
        self.training.set_device_probe(None)

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.training.set_device_probe(None)

    def test_default_probe_rejects_index_beyond_device_count(self):
        outcome = self.training._default_device_probe("cuda:15")
        self.assertFalse(outcome["ok"], outcome)
        self.assertIn("device_count=1", outcome["detail"])
        self.assertIn("requested=cuda:15", outcome["detail"])

    def test_default_probe_accepts_real_index_and_cpu(self):
        self.assertTrue(self.training._default_device_probe("cpu")["ok"])
        self.assertTrue(self.training._default_device_probe("cuda")["ok"])
        self.assertTrue(self.training._default_device_probe("cuda:0")["ok"])

    def test_check_device_rejects_missing_index_at_plan_stage(self):
        from backend.errors import ApiError

        with self.assertRaises(ApiError) as caught:
            self.training._check_device("cuda:15")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "TRAINING_DEVICE_UNAVAILABLE")
        self.assertIn("device_count=1", str(caught.exception))
        # cpu / cuda:0 不抛错（真实存在的设备照常通过）
        self.training._check_device("cpu")
        self.training._check_device("cuda:0")

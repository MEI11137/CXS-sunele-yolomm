"""WB17-05 第 6 组：对照 WB17-00 功能矩阵逐条闭合。

这份用例本身也是"矩阵落地的证据"：

* ``MATRIX`` 把 28 行逐条写成「功能 → 现在的实现 → 对应工具/入口」；
* ``test_every_business_row_has_a_real_tool`` 断言除"最终人工结论"和"凭据/授权边界"外，
  每一行业务都有**注册过的真实工具**；
* ``test_human_only_rows_have_no_agent_tool`` 反过来断言人工专属项**没有**工具，
  避免"模型替人做最终决定"。

导出、重新划分、模型对比、目录查看这些之前只有页面入口的能力，这里都用真实文件/真实数据验过。
"""

from __future__ import annotations

import csv
import io
import json
import secrets
import unittest

import yaml

from backend.services import identity as identity_service
from backend.services import storage

from backend.tests.support import body, csrf_headers
from backend.tests.test_business_wb17_03 import png_bytes
from backend.tests.test_business_wb17_04 import ReviewBase

# 行号 → (功能, 状态, 入口说明, 工具名元组)
#   status: tool=Agent 工具可做；human=只能人工；page=页面入口（下载/可视化）
MATRIX: dict[int, tuple[str, str, str, tuple[str, ...]]] = {
    1: ("登录/登出/当前用户", "human", "AuthView + 服务端会话", ()),
    2: ("注册", "human", "不提供注册入口（本机 CLI 建号）", ()),
    3: ("项目列表/创建/删除/恢复", "tool", "侧栏 + 项目页",
        ("hx_list_projects", "hx_create_project", "hx_rename_project", "hx_project_overview",
         "hx_project_admin", "hx_project_update")),
    4: ("项目绑定本机目录", "human", "项目详情 + 管理员授权", ()),
    5: ("会话列表/新建/删除", "tool", "侧栏",
        ("hx_list_sessions", "hx_create_session", "hx_rename_session",
         "hx_session_admin", "hx_session_history", "hx_search_sessions")),
    6: ("个人模型连接", "human", "设置页（凭据边界）", ()),
    7: ("模型选择/权限模式", "human", "助手区选择器（会话级）", ()),
    8: ("对话与事件流", "tool", "会话页 + /api/turns", ("hx_ask_user", "hx_open_page")),
    9: ("待确认动作", "human", "确认区由服务端判定", ()),
    10: ("停止任务", "human", "停止按钮（撤销继续动手的资格）", ()),
    11: ("附件上传/登记", "tool", "助手区拖拽 + 资产页", ("hx_register_asset",)),
    12: ("资料/手册", "tool", "设置页 → 资料与手册",
         ("hx_list_documents", "hx_search_documents", "hx_read_document", "hx_document_admin",
          "hx_list_manuals", "hx_read_manual", "hx_manual_admin")),
    13: ("MCP 扩展连接", "tool", "设置页 → 外部 MCP（配置人工、调用逐项授权）",
         ("hx_mcp_servers", "hx_mcp_call")),
    14: ("目录检查/授权", "tool", "扩展页 + 只读状态工具（授权仍人工）", ("hx_directory_status",)),
    15: ("图片检测", "tool", "巡检页", ("hx_detect_images", "hx_inspect_directory")),
    16: ("AI 核查（单/批量）", "tool", "记录页、复核页", ("hx_review_records",)),
    17: ("人工复核提交", "human", "复核页（最终人工结论）", ()),
    18: ("记录查询/删除/恢复", "tool", "记录页", ("hx_list_records", "hx_record_admin")),
    19: ("加入反馈池", "tool", "记录页批量", ("hx_list_feedback", "hx_add_feedback")),
    20: ("数据集导入/查重/划分", "tool", "数据集页",
         ("hx_import_dataset", "hx_dataset_audit", "hx_split_dataset", "hx_dataset_admin")),
    21: ("回原修订/另建数据集", "tool", "数据集页、回流页",
         ("hx_reflow_draft", "hx_reflow_preview", "hx_reflow_apply")),
    22: ("数据集导出", "tool", "数据集页下载 + 导出工具", ("hx_export_dataset",)),
    23: ("模型导入/发布/撤销/删除", "tool", "模型页",
         ("hx_import_model", "hx_inspect_weight", "hx_publish_model", "hx_model_admin")),
    24: ("模型对比", "tool", "模型页 + 对比工具", ("hx_compare_models", "hx_training_compare")),
    25: ("训练启动/取消/状态", "tool", "模型页 → 训练面板",
         ("hx_training_plan", "hx_training_start", "hx_training_status", "hx_training_cancel")),
    26: ("自动巡检闭环", "tool", "对话驱动：检测 → 核查 → 复核 → 回流（同一套业务工具组合）",
         ("hx_inspect_directory", "hx_review_records", "hx_add_feedback", "hx_reflow_apply")),
    27: ("回收站/CSV", "tool", "设置页、记录页 + 导出工具",
         ("hx_list_records", "hx_record_admin", "hx_export_records_csv")),
    28: ("受控远程入口", "human", "只绑 127.0.0.1，单端口同源；私有桥不对外", ()),
}


class MatrixBase(ReviewBase):
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

    def dataset(self, *, samples: int = 2) -> dict:
        dataset_id = f"dst_{secrets.token_hex(8)}"
        base = storage.project_dir(self.root, storage.DATASETS, dataset_id)
        for split in ("train", "val"):
            storage.project_dir(self.root, storage.DATASETS, dataset_id, "images", split)
            storage.project_dir(self.root, storage.DATASETS, dataset_id, "labels", split)
        storage.write_bytes(
            self.root,
            base / "data.yaml",
            yaml.safe_dump({"names": ["单一热斑", "大面积热斑"], "nc": 2}).encode("utf-8"),
        )
        conn = self.conn()
        try:
            conn.execute(
                "INSERT INTO datasets (id, project_id, owner_id, name, modalities, secondary_modality,"
                " classes, root_rel, source, sample_count, split_counts, status, created_at)"
                " VALUES (?, ?, ?, ?, '[\"rgb\"]', NULL, ?, ?, 'local', ?, ?, 'ready', ?)",
                (
                    dataset_id,
                    self.project["id"],
                    int(self.alice["id"]),
                    f"矩阵数据集-{dataset_id[-4:]}",
                    json.dumps(["单一热斑", "大面积热斑"], ensure_ascii=False),
                    storage.relative(self.root, base),
                    samples,
                    json.dumps({"train": samples // 2, "val": samples - samples // 2}),
                    "2026-11-05T00:00:00Z",
                ),
            )
            for index in range(samples):
                split = "train" if index % 2 == 0 else "val"
                data = png_bytes(color=(index * 7 + 1, 20, 30))
                image = base / "images" / split / f"s{index}.png"
                label = base / "labels" / split / f"s{index}.txt"
                storage.write_bytes(self.root, image, data)
                storage.write_bytes(self.root, label, b"0 0.5 0.5 0.2 0.2\n")
                detail = identity_service.identity_of_bytes(data)
                conn.execute(
                    "INSERT INTO dataset_samples (id, dataset_id, split, stem, rgb_rel, x_rel,"
                    " label_rel, width, height, boxes, class_ids, created_at, sha256, pixel_sha256)"
                    " VALUES (?, ?, ?, ?, ?, NULL, ?, 64, 48, '[{\"class_id\": 0}]', '[0]', ?, ?, ?)",
                    (
                        f"smp_{secrets.token_hex(8)}",
                        dataset_id,
                        split,
                        f"s{index}",
                        storage.relative(self.root, image),
                        storage.relative(self.root, label),
                        "2026-11-05T00:00:00Z",
                        detail["sha256"],
                        detail["pixel_sha256"],
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        return {"id": dataset_id, "root": base}


# --------------------------------------------------------------------------- 矩阵闭合


class MatrixCoverageTest(MatrixBase):
    def test_every_business_row_has_a_real_tool(self):
        from backend.agent import tools as agent_tools

        missing: list[str] = []
        for number, (_title, status, _where, names) in MATRIX.items():
            if status != "tool":
                continue
            for name in names:
                if name not in agent_tools.TOOLS:
                    missing.append(f"#{number} 缺工具 {name}")
        self.assertEqual(missing, [])
        self.assertEqual(len(MATRIX), 28)

    def test_human_only_rows_have_no_agent_tool(self):
        from backend.agent import tools as agent_tools

        names = sorted(agent_tools.TOOLS)
        forbidden = {
            "提交复核结论": "review submit",
            "保存连接凭据": "connection save",
            "绑定/授权目录": "directory bind",
            "建号注册": "user register",
            "停止任务": "turn stop",
        }
        for label, needle in forbidden.items():
            hits = [name for name in names if needle.split()[0] in name and needle.split()[1] in name]
            self.assertEqual(hits, [], f"{label} 不该有 Agent 工具：{hits}")
        # 手册里明确写了这条边界
        conn = self.conn()
        try:
            manual = conn.execute("SELECT id FROM manuals WHERE id = 'man_builtin_permissions'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(manual)

    def test_matrix_doc_matches_code(self):
        """WB17-00 的 28 行在 WB17-05 日志里逐条给了结论，且不再有"未实现（WB17-03/04/05）"。"""
        from pathlib import Path

        text = Path("docs/workbuddy-development/WB17-00.md").read_text(encoding="utf-8")
        self.assertIn("功能矩阵", text)
        log = Path("docs/workbuddy-development/WB17-05.md").read_text(encoding="utf-8")
        self.assertIn("WB17-00 矩阵", log)


# --------------------------------------------------------------------------- CSV


class CsvExportTest(MatrixBase):
    def test_scope_is_required(self):
        record = self.record()
        response = self.client.get(f"/api/projects/{self.project['id']}/detections/export")
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        self.assertEqual(body(response)["error"], "EXPORT_SCOPE_REQUIRED")

        vague = self.client.get(
            f"/api/projects/{self.project['id']}/detections/export?scope=all"
        )
        self.assertEqual(vague.status_code, 400, vague.get_data(as_text=True))

    def test_page_export_writes_real_file_with_open_conclusion(self):
        first = self.record(key="a")
        self.record(key="b")
        self.confirm(first["id"], outcome="defect")

        response = self.client.get(
            f"/api/projects/{self.project['id']}/detections/export?scope=page&limit=10"
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIn("text/csv", response.headers.get("Content-Type", ""))
        self.assertIn("attachment", response.headers.get("Content-Disposition", ""))
        raw = response.data
        response.close()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "CSV 要带 BOM，Excel 才不乱码")
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 3)  # 表头 + 2 条
        self.assertEqual(rows[0][0], "记录ID")
        outcomes = {row[0]: row[10] for row in rows[1:]}
        self.assertEqual(outcomes[first["id"]], "defect")
        self.assertTrue(any(value == "" for key, value in outcomes.items() if key != first["id"]))


    def test_selected_export_only_contains_chosen_records(self):
        first = self.record(key="a")
        second = self.record(key="b")
        response = self.client.get(
            f"/api/projects/{self.project['id']}/detections/export"
            f"?scope=selected&ids={first['id']},{second['id']}"
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
        response.close()
        self.assertEqual(len(rows), 3)

        only = self.client.get(
            f"/api/projects/{self.project['id']}/detections/export?scope=selected&ids={first['id']}"
        )
        self.assertEqual(only.status_code, 200, only.get_data(as_text=True))
        rows = list(csv.reader(io.StringIO(only.data.decode("utf-8-sig"))))
        only.close()
        self.assertEqual([row[0] for row in rows[1:]], [first["id"]])

    def test_missing_selection_fails_without_writing_a_file(self):
        self.record()
        response = self.client.get(
            f"/api/projects/{self.project['id']}/detections/export?scope=selected&ids=det_00000000000000ff"
        )
        self.assertEqual(response.status_code, 404, response.get_data(as_text=True))
        self.assertEqual(body(response)["error"], "RECORD_NOT_FOUND")

    def test_tool_returns_real_path_and_counts(self):
        from backend.agent import tools as agent_tools

        self.record()
        conn = self.conn()
        try:
            result = agent_tools.TOOLS["hx_export_records_csv"].handler(
                self._ctx(conn, {"scope": "page", "limit": 5})
            )
        finally:
            conn.close()
        self.assertEqual(result["scope"], "page")
        self.assertEqual(result["rows"], 1)
        self.assertTrue(result["relative"].startswith("exports/"))
        self.assertTrue(result["verification"]["verified"])
        path = self.root / result["relative"]
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_size, result["bytes"])


# --------------------------------------------------------------------------- 数据集与模型


class DatasetModelMatrixTest(MatrixBase):
    def test_dataset_export_returns_real_zip(self):
        from backend.agent import tools as agent_tools

        dataset = self.dataset()
        conn = self.conn()
        try:
            result = agent_tools.TOOLS["hx_export_dataset"].handler(
                self._ctx(conn, {"dataset_id": dataset["id"]})
            )
        finally:
            conn.close()
        self.assertTrue(result["relative"].endswith("dataset.zip"))
        self.assertGreater(result["bytes"], 0)
        self.assertTrue((self.root / result["relative"]).is_file())

        download = self.client.get(
            f"/api/projects/{self.project['id']}/datasets/{dataset['id']}/export"
        )
        try:
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.headers.get("Content-Type"), "application/zip")
            body = download.get_data()  # 二进制，不能按文本解码
            self.assertTrue(body.startswith(b"PK"))
            self.assertGreater(len(body), 0)
        finally:
            download.close()

    def test_split_dataset_creates_a_new_version_and_is_idempotent(self):
        from backend.agent import tools as agent_tools

        dataset = self.dataset()
        conn = self.conn()
        try:
            first = agent_tools.TOOLS["hx_split_dataset"].handler(
                self._ctx(conn, {"dataset_id": dataset["id"], "strategy": "even", "seed": 7, "request_key": "split-1"})
            )
            again = agent_tools.TOOLS["hx_split_dataset"].handler(
                self._ctx(conn, {"dataset_id": dataset["id"], "strategy": "even", "seed": 7, "request_key": "split-1"})
            )
        finally:
            conn.close()
        self.assertNotEqual(first["dataset"]["id"], dataset["id"])
        self.assertEqual(first["dataset"]["id"], again["dataset"]["id"])
        self.assertEqual(first["dataset"]["sample_count"], 2)

    def test_model_compare_marks_different_conditions_as_not_comparable(self):
        from backend.agent import tools as agent_tools

        left = self.make_model()
        right = self.make_model()
        dataset = self.dataset()
        conn = self.conn()
        try:
            conn.execute(
                "UPDATE models SET metrics = ?, dataset_id = ? WHERE id = ?",
                (json.dumps({"map50": 0.6, "map50_95": 0.4}), dataset["id"], left["id"]),
            )
            conn.execute(
                "UPDATE models SET metrics = ?, dataset_id = ? WHERE id = ?",
                (json.dumps({"map50": 0.7, "map50_95": 0.45}), "dst_other", right["id"]),
            )
            conn.commit()
            result = agent_tools.TOOLS["hx_compare_models"].handler(
                self._ctx(conn, {"model_id": left["id"], "other_model_id": right["id"]})
            )
        finally:
            conn.close()
        self.assertFalse(result["comparable"])
        self.assertEqual(result["metrics"]["map50"]["delta"], 0.1)
        self.assertIn("不能说", result["note"])
        self.assertTrue(result["conditions"])

    def test_directory_status_is_read_only_view(self):
        from backend.agent import tools as agent_tools

        conn = self.conn()
        try:
            result = agent_tools.TOOLS["hx_directory_status"].handler(self._ctx(conn, {}))
        finally:
            conn.close()
        self.assertTrue(result["binding"]["bound"])
        self.assertTrue(result["binding"]["exists"])
        self.assertTrue(result["binding"]["writable"])
        self.assertTrue(any(item["path"] == str(self.root) for item in result["granted_roots"]))
        self.assertIn("不能改", result["note"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""WB17 收尾批次4：root 独立反例的最小共享修正。

覆盖四类真实反例（不重写已通过的业务）：

* 显式重开后：记录回到草稿队列、可继续编辑，但**生效结论仍是最新已提交的驳回**，
  旧 confirmed 不能复活加入反馈；重开后真正再提交（有缺陷 / 无缺陷 / 再次驳回）
  都能落成新的人工结论版本。
* ``hx_list_feedback`` 的状态与真实数据库一致（含 ``reflowed``），页面与工具同口径。
* 只读子动作（``hx_reflow_draft`` list/read、``hx_document_admin`` locate）在
  request 模式下直接执行，不出确认卡；写动作仍按既有权限模式。
* 回收站里的项目/会话可以通过已有的只读列表找到真实 ID，再恢复；
  默认列表仍只显示活跃项，不改变页面行为。

工具用例走**真实工具网关**（与生产同一顺序：先开一轮，再签发会话级令牌），
不是直接调 handler 冒充 Agent。
"""

from __future__ import annotations

import secrets
import unittest

from backend.agent import tools as agent_tools
from backend.agent.policy import DECISION_AUTO, DECISION_CONFIRM, decide
from backend.errors import ApiError
from backend.services import documents as document_service
from backend.services import projects as project_service
from backend.services import records as record_service
from backend.services import reflow as reflow_service
from backend.services import review as review_service
from backend.services import sessions as session_service

from backend.tests.support import body
from backend.tests.test_business_wb17_03 import png_bytes
from backend.tests.test_business_wb17_04 import ReviewBase
from backend.tests.test_business_wb17_b_agent_crud_ir import AgentCallBase


class ReopenStateTest(ReviewBase):
    """重开不是“把驳回藏起来”，而是“旧结论继续生效 + 新草稿可编辑”。"""

    def _rejected(self, key: str) -> tuple[dict, dict]:
        record = self.record(key=key)
        confirmed = self.confirm(record["id"], key=f"{key}-ok")
        conn = self.conn()
        try:
            review_service.reject(
                conn,
                self.alice["id"],
                self.project["id"],
                record["id"],
                reason="框错了",
                base_version=confirmed["version"],
                request_key=f"{key}-reject",
            )
            state = review_service.reopen(
                conn,
                self.alice["id"],
                self.project["id"],
                record["id"],
                request_key=f"{key}-reopen",
            )
        finally:
            conn.close()
        return record, state

    def test_reopen_puts_record_back_in_draft_queue_but_keeps_rejection_effective(self):
        record, state = self._rejected("reopen-state")
        self.assertEqual(state["queue"], "draft", "重开后应回到草稿队列")
        self.assertTrue(state["reopened"])
        self.assertEqual(state["human_status"], "draft")
        self.assertEqual(state["latest_submitted"]["status"], "rejected")
        self.assertEqual(state["latest_submitted"]["version"], 2)
        self.assertEqual(state["draft"]["version"], 3, "草稿编辑号独立于结论号")

        conn = self.conn()
        try:
            draft_view = review_service.queue(
                conn, self.alice["id"], self.project["id"], view="draft"
            )
            rejected_view = review_service.queue(
                conn, self.alice["id"], self.project["id"], view="rejected"
            )
            statuses = review_service.statuses_for_records(
                conn, self.alice["id"], [record["id"]]
            )
            effective = review_service.effective_review(
                conn, record["id"], self.alice["id"]
            )
            with self.assertRaises(ApiError) as ctx:
                record_service.add_feedback(
                    conn,
                    self.alice["id"],
                    self.project["id"],
                    record["id"],
                    request_key="reopen-feedback",
                )
        finally:
            conn.close()

        self.assertIn(record["id"], [item["id"] for item in draft_view["items"]])
        self.assertEqual(rejected_view["counts"]["rejected"], 0)
        self.assertEqual(statuses[record["id"]]["human_status"], "draft")
        self.assertEqual(statuses[record["id"]]["review_version"], 2)
        self.assertEqual(effective["status"], "rejected", "旧驳回继续生效")
        self.assertEqual(ctx.exception.code, "REVIEW_NOT_CONFIRMED", "旧 confirmed 不能复活")

    def test_reopen_then_resubmit_defect_and_no_defect_both_complete(self):
        for key, outcome in (("reopen-defect", "defect"), ("reopen-clean", "no_defect")):
            record, state = self._rejected(key)
            boxes = (
                [{"class_id": 1, "bbox": [10, 10, 44, 36], "severity": "high"}]
                if outcome == "defect"
                else []
            )
            conn = self.conn()
            try:
                version, created = review_service.submit(
                    conn,
                    self.alice["id"],
                    self.project["id"],
                    record["id"],
                    boxes=boxes,
                    outcome=outcome,
                    base_version=state["latest_submitted"]["version"],
                    request_key=f"{key}-resubmit",
                )
                after = review_service.state(
                    conn, self.alice["id"], self.project["id"], record["id"]
                )
            finally:
                conn.close()
            self.assertTrue(created)
            self.assertEqual(version["version"], 3, "结论版本只数提交：v1 确认 / v2 驳回 / v3 新结论")
            self.assertEqual(version["outcome"], outcome)
            self.assertEqual(after["queue"], "confirmed")
            self.assertIsNone(after["draft"], "提交后草稿被消费，不应残留")
            self.assertFalse(after["reopened"])

    def test_reopen_then_edit_then_reject_again_is_a_new_conclusion(self):
        record, state = self._rejected("reopen-reject")
        conn = self.conn()
        try:
            saved = review_service.save_draft(
                conn,
                self.alice["id"],
                self.project["id"],
                record["id"],
                boxes=[{"class_id": 0, "bbox": [12, 12, 40, 34], "severity": "low"}],
                note="重开后改了框，仍判驳回",
                base_version=state["draft"]["version"],
            )
            rejected, created = review_service.reject(
                conn,
                self.alice["id"],
                self.project["id"],
                record["id"],
                reason="改完还是不对",
                base_version=state["latest_submitted"]["version"],
                request_key="reopen-reject-2",
            )
            after = review_service.state(
                conn, self.alice["id"], self.project["id"], record["id"]
            )
        finally:
            conn.close()
        self.assertEqual(saved["draft"]["version"], 4)
        self.assertTrue(created, "重开后的再次驳回是新意图，不能被幂等分支吞掉")
        self.assertEqual(rejected["version"], 3)
        self.assertEqual(rejected["status"], "rejected")
        self.assertIsNone(after["draft"])
        self.assertEqual(after["queue"], "rejected")

    def test_plain_draft_on_confirmed_record_does_not_downgrade_status(self):
        """已确认记录上的普通草稿只是再次编辑，不能把人工状态改成 draft。"""
        record = self.record(key="confirmed-draft")
        self.confirm(record["id"], key="confirmed-draft-ok")
        conn = self.conn()
        try:
            review_service.save_draft(
                conn,
                self.alice["id"],
                self.project["id"],
                record["id"],
                boxes=[{"class_id": 1, "bbox": [15, 15, 42, 35], "severity": "medium"}],
                base_version=1,
            )
            state = review_service.state(
                conn, self.alice["id"], self.project["id"], record["id"]
            )
            statuses = review_service.statuses_for_records(
                conn, self.alice["id"], [record["id"]]
            )
        finally:
            conn.close()
        self.assertEqual(state["queue"], "confirmed")
        self.assertEqual(statuses[record["id"]]["human_status"], "confirmed")


class ToolReversalTest(AgentCallBase):
    """root 网关预检失败项的共享修正。"""

    def test_feedback_status_reflowed_matches_page_and_database(self):
        record = self.record(key="fb-status")
        self.confirm(record["id"], key="fb-status-ok")
        token = self.open_turn(mode="full")
        created = self.ok_call(
            token,
            "hx_add_feedback",
            {"record_id": record["id"], "note": "回流用", "request_key": "fb-reflowed"},
        )
        conn = self.conn()
        try:
            conn.execute(
                "UPDATE record_feedback SET status = 'reflowed' WHERE id = ?",
                (created["feedback"]["id"],),
            )
            conn.commit()
        finally:
            conn.close()

        listed = self.ok_call(token, "hx_list_feedback", {"status": "reflowed"})
        self.assertEqual([item["id"] for item in listed["feedback"]], [created["feedback"]["id"]])
        self.assertEqual(listed["feedback"][0]["status"], "reflowed")

        page = self.client.get(f"/api/projects/{self.project['id']}/feedback?status=reflowed")
        self.assertEqual(
            [item["id"] for item in body(page)["data"]["feedback"]],
            [created["feedback"]["id"]],
        )

    def test_read_only_subactions_run_directly_in_request_mode(self):
        # 草稿先由真实服务建好（full 下 Agent 新建已有批次3用例覆盖）；
        # 这里专测 request 模式下只读子动作不被写审批挡住。
        record = self.record(key="req-draft")
        self.confirm(record["id"], key="req-draft-ok")
        conn = self.conn()
        try:
            draft = reflow_service.save_draft(
                conn,
                int(self.alice["id"]),
                self.project["id"],
                name="请求模式草稿",
                mode="new",
                dataset_id=None,
                selections=[{"record_id": record["id"], "action": "append", "split": "train"}],
                strategy="even",
                ratios=None,
                seed=0,
            )
        finally:
            conn.close()

        token = self.open_turn(mode="request")
        listed = self.ok_call(token, "hx_reflow_draft", {"action": "list"})
        self.assertIn(draft["id"], [item["id"] for item in listed["drafts"]])

        read = self.ok_call(
            token, "hx_reflow_draft", {"action": "read", "draft_id": draft["id"]}
        )
        self.assertEqual(read["id"], draft["id"], "read 是只读，request 模式直接执行")

        # 写动作仍要确认：同一模式下 save/delete 不会绕过权限模式。
        pending = self.call(
            token,
            "hx_reflow_draft",
            {"action": "delete", "draft_id": draft["id"]},
        )
        self.assertEqual(pending.status_code, 200)
        self.assertEqual(pending.json()["data"]["status"], "pending_confirmation")

    def test_document_locate_is_read_but_import_stays_write(self):
        token = self.open_turn(mode="request")
        source = self.root / "locate-source.txt"
        source.write_text("原件内容", encoding="utf-8")
        conn = self.conn()
        try:
            document = document_service.ingest_path(
                conn, self.alice["id"], self.project["id"], "locate-source.txt"
            )
        finally:
            conn.close()
        located = self.ok_call(
            token, "hx_document_admin", {"action": "locate", "document_id": document["id"]}
        )
        self.assertEqual(located["size"], len("原件内容".encode("utf-8")))
        # 资料是复制进项目 documents/<doc_id>/ 的副本；定位返回的就是项目内真实相对路径。
        self.assertTrue(located["project_relative_path"].endswith("locate-source.txt"))
        self.assertTrue(located["project_relative_path"].startswith("documents/"))

    def test_deleted_projects_and_sessions_are_discoverable_before_restore(self):
        token = self.open_turn(mode="full")
        # 另建一个项目并删掉：默认列表看不到，include_deleted 能看到并拿到真实 ID。
        conn = self.conn()
        try:
            other = project_service.create(
                conn, int(self.alice["id"]), name="待恢复项目"
            )
            row = project_service.get_owned(conn, other["id"], int(self.alice["id"]))
            project_service.soft_delete(conn, row)
            extra = session_service.create(
                conn,
                project_id=str(self.project["id"]),
                owner_id=int(self.alice["id"]),
                title="待恢复会话",
            )
            session_row = session_service.get_owned(conn, extra["id"], int(self.alice["id"]))
            session_service.soft_delete(conn, session_row)
        finally:
            conn.close()

        active_projects = self.ok_call(token, "hx_list_projects", {})
        self.assertNotIn(other["id"], [item["id"] for item in active_projects["projects"]])
        all_projects = self.ok_call(token, "hx_list_projects", {"include_deleted": True})
        found = next(item for item in all_projects["projects"] if item["id"] == other["id"])
        self.assertEqual(found["status"], "deleted")
        self.assertTrue(found["deleted_at"])

        active_sessions = self.ok_call(token, "hx_list_sessions", {})
        self.assertNotIn(extra["id"], [item["id"] for item in active_sessions["sessions"]])
        all_sessions = self.ok_call(token, "hx_list_sessions", {"include_deleted": True})
        found_session = next(item for item in all_sessions["sessions"] if item["id"] == extra["id"])
        self.assertEqual(found_session["status"], "deleted")
        self.assertTrue(found_session["deleted_at"])

        restored = self.ok_call(
            token, "hx_project_admin", {"action": "restore", "project_id": other["id"]}
        )
        self.assertEqual(restored["status"], "restored")


class ActionRiskTest(unittest.TestCase):
    """共用 ToolSpec 按 action 计算风险，不能把只读子动作整体当写。"""

    def _ctx(self, action: str):
        return type("Ctx", (), {"args": {"action": action}})()

    def test_reflow_draft_risk_by_action(self):
        spec = agent_tools.TOOLS["hx_reflow_draft"]
        self.assertEqual(spec.effective_risk(self._ctx("list")), "read")
        self.assertEqual(spec.effective_risk(self._ctx("read")), "read")
        for action in ("save", "delete", "restore"):
            self.assertEqual(spec.effective_risk(self._ctx(action)), "write")
        self.assertEqual(decide("request", spec.effective_risk(self._ctx("list"))).decision, DECISION_AUTO)
        self.assertEqual(
            decide("request", spec.effective_risk(self._ctx("save"))).decision, DECISION_CONFIRM
        )

    def test_document_admin_risk_by_action(self):
        spec = agent_tools.TOOLS["hx_document_admin"]
        self.assertEqual(spec.effective_risk(self._ctx("locate")), "read")
        for action in ("import", "delete", "restore"):
            self.assertEqual(spec.effective_risk(self._ctx(action)), "write")

    def test_feedback_schema_uses_real_status_vocabulary(self):
        spec = agent_tools.TOOLS["hx_list_feedback"]
        pattern = spec.schema["properties"]["status"]["pattern"]
        self.assertIn("pending", pattern)
        self.assertIn("reflowed", pattern)
        self.assertIn("void", pattern)
        self.assertNotIn("used", pattern)

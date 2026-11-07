"""WB17-05 收尾：反馈池“待整理”只认当前有效的人工确认版本。

root 独立反例（``反馈池有效版本独立核验``）的三类脏数据：

1. v1 确认入池后又 v2 确认入池 → pending 同时列出 v1 + v2；
2. 入池后又驳回 → 旧 confirmed 反馈仍 pending；
3. 记录软删 → 反馈仍 pending。

口径与 ``review.effective_review`` 一致：记录未删 + 反馈绑定的版本就是该记录
最近一次已提交版本且为 confirmed。历史行不删（``status=all`` 仍可查），记录恢复
后原反馈仍能取回。筛选必须在 LIMIT 之前，否则先截一页再过滤会漏项。
"""

from __future__ import annotations

import secrets
import unittest

from backend.agent import identity as agent_identity
from backend.agent import tools as agent_tools
from backend.agent.context import Actor
from backend.agent.tools import ToolContext
from backend.services import records as record_service
from backend.services import review as review_service
from backend.services import reflow as reflow_service
from backend.tests import test_business_wb17_04 as wb04
from backend.tests.support import body
from backend.tests.test_business_wb17_03 import png_bytes
from backend.tests.test_business_wb17_04 import ReviewBase


class FeedbackQueueTest(ReviewBase):
    def _actor(self, conn) -> Actor:
        project = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (self.project["id"],)
        ).fetchone()
        connection = agent_identity.connection_for(conn, self.alice["id"])
        return Actor(
            user_id=int(self.alice["id"]),
            username="alice",
            project_id=str(self.project["id"]),
            project_name=str(project["name"]),
            session_id=str(self.session["id"]),
            turn_id=f"trn_{secrets.token_hex(8)}",
            mode=agent_identity.effective_mode(conn, self.alice["id"]),
            root_path=self.root,
            connection_fingerprint=agent_identity.connection_fingerprint(connection),
            provider=str(connection["provider"]),
            model=agent_identity.effective_model(conn, self.alice["id"], connection),
        )

    def _confirm(self, record_id: str, *, base: int, key: str) -> dict:
        conn = self.conn()
        try:
            version, _ = review_service.submit(
                conn,
                self.alice["id"],
                self.project["id"],
                record_id,
                boxes=[{"class_id": 1, "bbox": [10, 10, 44, 36], "severity": "high"}],
                outcome="defect",
                base_version=base,
                request_key=key,
            )
        finally:
            conn.close()
        return version

    def _reject(self, record_id: str, *, base: int, key: str) -> dict:
        conn = self.conn()
        try:
            version, _ = review_service.reject(
                conn,
                self.alice["id"],
                self.project["id"],
                record_id,
                reason="误检",
                base_version=base,
                request_key=key,
            )
        finally:
            conn.close()
        return version

    def _add(self, record_id: str, key: str) -> dict:
        conn = self.conn()
        try:
            feedback, created = record_service.add_feedback(
                conn, self.alice["id"], self.project["id"], record_id, request_key=key
            )
        finally:
            conn.close()
        self.assertTrue(created, feedback)
        return feedback

    def _pending(self, *, limit: int = 50) -> list[dict]:
        conn = self.conn()
        try:
            return record_service.list_feedback(
                conn, self.alice["id"], self.project["id"], status="pending", limit=limit
            )["feedback"]
        finally:
            conn.close()

    def _all(self) -> list[dict]:
        conn = self.conn()
        try:
            return record_service.list_feedback(
                conn, self.alice["id"], self.project["id"], status="all", limit=200
            )["feedback"]
        finally:
            conn.close()

    # ------------------------------------------------------------------ 反例

    def test_only_latest_confirmed_version_stays_pending(self):
        record = self.record(key="fbq-v2")
        self._confirm(record["id"], base=0, key="fbq-v2-c1")
        first = self._add(record["id"], "fbq-v2-f1")
        self._confirm(record["id"], base=1, key="fbq-v2-c2")
        second = self._add(record["id"], "fbq-v2-f2")

        pending = self._pending()
        self.assertEqual([item["id"] for item in pending], [second["id"]], pending)
        # 历史不能抹掉：旧版本那一行仍在表里，只是不算待整理。
        ids = {item["id"] for item in self._all()}
        self.assertIn(first["id"], ids)
        self.assertIn(second["id"], ids)

    def test_rejected_record_leaves_pending_but_keeps_history(self):
        record = self.record(key="fbq-reject")
        self._confirm(record["id"], base=0, key="fbq-reject-c1")
        feedback = self._add(record["id"], "fbq-reject-f1")
        self._reject(record["id"], base=1, key="fbq-reject-r1")

        self.assertEqual(self._pending(), [])
        self.assertIn(feedback["id"], {item["id"] for item in self._all()})

    def test_deleted_record_leaves_pending_and_returns_after_restore(self):
        record = self.record(key="fbq-del")
        self._confirm(record["id"], base=0, key="fbq-del-c1")
        feedback = self._add(record["id"], "fbq-del-f1")

        conn = self.conn()
        try:
            record_service.soft_delete(conn, self.alice["id"], self.project["id"], [record["id"]])
        finally:
            conn.close()
        self.assertEqual(self._pending(), [], "软删记录的反馈不能还挂在待整理")

        conn = self.conn()
        try:
            record_service.restore(conn, self.alice["id"], self.project["id"], [record["id"]])
        finally:
            conn.close()
        pending = self._pending()
        self.assertEqual([item["id"] for item in pending], [feedback["id"]], "恢复记录后原反馈仍可取回")

    def test_filter_happens_before_limit(self):
        # A 的反馈是有效待办，但比 B 的过期反馈旧；limit=1 不能漏掉 A。
        record_a = self.record(key="fbq-a")
        self._confirm(record_a["id"], base=0, key="fbq-a-c1")
        valid = self._add(record_a["id"], "fbq-a-f1")

        record_b = self.record(key="fbq-b")
        self._confirm(record_b["id"], base=0, key="fbq-b-c1")
        self._add(record_b["id"], "fbq-b-f1")
        # 只再确认、不入池：B 的旧反馈立刻过期，但 created_at 比 A 新。
        self._confirm(record_b["id"], base=1, key="fbq-b-c2")

        page = self._pending(limit=1)
        self.assertEqual([item["id"] for item in page], [valid["id"]], page)

    # ------------------------------------------------------- 页面与 Agent 同口径

    def test_http_page_and_agent_tool_share_the_same_pending_rows(self):
        record = self.record(key="fbq-http")
        self._confirm(record["id"], base=0, key="fbq-http-c1")
        self._add(record["id"], "fbq-http-f1")
        self._confirm(record["id"], base=1, key="fbq-http-c2")
        self._add(record["id"], "fbq-http-f2")

        page = self.client.get(f"/api/projects/{self.project['id']}/feedback?status=pending")
        self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
        page_ids = [item["id"] for item in body(page)["data"]["feedback"]]

        conn = self.conn()
        try:
            ctx = ToolContext(
                conn=conn,
                actor=self._actor(conn),
                args={"status": "pending"},
                emit=lambda *_args: None,
                jobs={},
            )
            tool_rows = agent_tools.TOOLS["hx_list_feedback"].handler(ctx)["feedback"]
        finally:
            conn.close()
        self.assertEqual(page_ids, [item["id"] for item in tool_rows])
        self.assertEqual(len(page_ids), 1, "只应剩最新确认版本那一条")

    def test_records_page_feedback_status_matches_queue(self):
        record = self.record(key="fbq-status")
        self._confirm(record["id"], base=0, key="fbq-status-c1")
        self._add(record["id"], "fbq-status-f1")
        self._confirm(record["id"], base=1, key="fbq-status-c2")

        conn = self.conn()
        try:
            statuses = review_service.feedback_statuses_for_records(
                conn, self.alice["id"], [record["id"]]
            )
        finally:
            conn.close()
        # 新确认版本尚未入池 → 记录页应显示 none（可再次加入），不是沿用旧行的 pending。
        self.assertEqual(statuses[record["id"]], "none")


class FeedbackReflowTest(ReviewBase):
    """旧版本 pending 行是历史，不能把新版本的回流永远卡成 STALE_FEEDBACK。

    只借用 ``ReflowTest`` 的两个造数辅助，不继承它的测试方法（避免同一批用例跑两遍）。
    """

    _base_dataset = wb04.ReflowTest._base_dataset
    _record_with_bytes = wb04.ReflowTest._record_with_bytes

    def _confirm_at(self, record_id: str, *, base: int, key: str) -> dict:
        conn = self.conn()
        try:
            version, _ = review_service.submit(
                conn,
                self.alice["id"],
                self.project["id"],
                record_id,
                boxes=[{"class_id": 0, "bbox": [4, 4, 30, 30], "severity": "high"}],
                outcome="defect",
                base_version=base,
                request_key=key,
            )
        finally:
            conn.close()
        return version

    def test_newer_feedback_reflows_while_older_pending_row_stays_history(self):
        dataset_id, _ = self._base_dataset()
        record, _ = self._record_with_bytes(png_bytes(), "fbq-reflow")
        self._confirm_at(record["id"], base=0, key="fbq-reflow-c1")
        conn = self.conn()
        try:
            first, _ = record_service.add_feedback(
                conn, self.alice["id"], self.project["id"], record["id"], request_key="fbq-reflow-f1"
            )
        finally:
            conn.close()
        self._confirm_at(record["id"], base=1, key="fbq-reflow-c2")
        conn = self.conn()
        try:
            second, _ = record_service.add_feedback(
                conn, self.alice["id"], self.project["id"], record["id"], request_key="fbq-reflow-f2"
            )
            new = reflow_service.apply(
                conn,
                self.alice["id"],
                self.project["id"],
                mode="revision",
                dataset_id=dataset_id,
                selections=[{"record_id": record["id"], "action": "replace"}],
                name="反馈回流",
                request_key="fbq-reflow-apply",
            )
            pending = record_service.list_feedback(
                conn, self.alice["id"], self.project["id"], status="pending"
            )["feedback"]
            reflowed = record_service.list_feedback(
                conn, self.alice["id"], self.project["id"], status="reflowed"
            )["feedback"]
        finally:
            conn.close()
        self.assertTrue(new["id"])
        self.assertEqual(pending, [], "当前版本已回流，待整理应为空")
        self.assertEqual([item["id"] for item in reflowed], [second["id"]])
        # 旧版本那一行仍是 pending 历史，可查但不参与待整理。
        conn = self.conn()
        try:
            history = {
                item["id"]: item["status"]
                for item in record_service.list_feedback(
                    conn, self.alice["id"], self.project["id"], status="all", limit=200
                )["feedback"]
            }
        finally:
            conn.close()
        self.assertEqual(history[first["id"]], "pending")
        self.assertEqual(history[second["id"]], "reflowed")


if __name__ == "__main__":
    unittest.main()

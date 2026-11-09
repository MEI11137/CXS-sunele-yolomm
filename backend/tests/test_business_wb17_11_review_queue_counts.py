"""WB17-11 复核队列计数的精确性。

反例来自代码审查：``queue()`` 先按全项目最新 500 条取列表，再在 Python 里分桶，
超过 500 条时“待复核/草稿/已驳回/已确认”的 counts 会静默截断；列表上限和真实
计数也没有区分。这里用隔离测试库插 500+ 条记录钉住：

* 计数必须是全量精确统计，不受列表窗口影响；
* 指定批次时计数只收窄该批次，但批次下拉仍返回全部批次；
* 列表只取当前视图，不能因为别的视图记录更新就把少见视图饿死。
"""

from __future__ import annotations

import secrets
import unittest

from backend.services import review as review_service
from backend.tests.test_business_wb17_03 import BusinessBase


class ReviewQueueCountsTest(BusinessBase):
    """隔离测试库，不跑真实检测、不调用付费模型。"""

    def _insert_detections(self, count: int, *, prefix: str, flow_id: str | None = None) -> list[str]:
        """直接落 detections 行，只保留 queue 分桶需要的列。"""
        ids: list[str] = []
        rows = []
        for index in range(count):
            record_id = f"det_{prefix}{index:05d}"
            ids.append(record_id)
            created = f"2026-11-09T00:{index // 60:02d}:{index % 60:02d}Z"
            rows.append(
                (
                    record_id,
                    self.project["id"],
                    int(self.alice["id"]),
                    f"queue-count-{prefix}-{index}-{secrets.token_hex(3)}",
                    "mdl_queue_counts_missing",
                    "ast_queue_counts_missing",
                    "done",
                    created,
                    flow_id,
                )
            )
        conn = self.conn()
        try:
            conn.executemany(
                """
                INSERT INTO detections
                    (id, project_id, owner_id, request_key, model_id, rgb_asset_id,
                     status, created_at, flow_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        finally:
            conn.close()
        return ids

    def _insert_version(self, record_id: str, version: int, status: str) -> None:
        conn = self.conn()
        try:
            now = "2026-11-09T00:00:00Z"
            conn.execute(
                """
                INSERT INTO review_versions
                    (id, project_id, owner_id, detection_id, version, status,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"rvw_{secrets.token_hex(8)}",
                    self.project["id"],
                    int(self.alice["id"]),
                    record_id,
                    version,
                    status,
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_counts_are_exact_beyond_five_hundred(self):
        """523 条记录：旧实现 counts 最多到 500，新实现必须逐桶精确。"""
        pending = self._insert_detections(520, prefix="p")
        draft = self._insert_detections(1, prefix="d")[0]
        confirmed = self._insert_detections(1, prefix="c")[0]
        rejected = self._insert_detections(1, prefix="r")[0]
        self._insert_version(draft, 1, "draft")
        self._insert_version(confirmed, 1, "confirmed")
        self._insert_version(rejected, 1, "rejected")

        conn = self.conn()
        try:
            data = review_service.queue(conn, self.alice["id"], self.project["id"], limit=200)
        finally:
            conn.close()

        self.assertEqual(data["counts"], {
            "pending": 520,
            "draft": 1,
            "rejected": 1,
            "confirmed": 1,
        })
        # 列表窗口与真实计数分开：pending 列表最多 200，但计数仍是 520。
        self.assertEqual(data["view"], "pending")
        self.assertLessEqual(len(data["items"]), 200)
        self.assertTrue(all(item["bucket"] == "pending" for item in data["items"]))
        self.assertEqual(len(pending), 520)

    def test_flow_filter_counts_only_selected_batch_but_keeps_all_flow_ids(self):
        """指定批次：counts 只算该批次；flow_ids 仍返回全项目全部批次。"""
        self._insert_detections(510, prefix="fa", flow_id="flow-alpha")
        self._insert_detections(3, prefix="fb", flow_id="flow-beta")

        conn = self.conn()
        try:
            alpha = review_service.queue(
                conn, self.alice["id"], self.project["id"], flow_id="flow-alpha", limit=5
            )
            beta = review_service.queue(
                conn, self.alice["id"], self.project["id"], flow_id="flow-beta", limit=5
            )
        finally:
            conn.close()

        self.assertEqual(alpha["counts"]["pending"], 510)
        self.assertEqual(alpha["counts"]["draft"], 0)
        self.assertEqual(beta["counts"]["pending"], 3)
        self.assertEqual(alpha["flow_ids"], ["flow-alpha", "flow-beta"])
        self.assertEqual(beta["flow_ids"], ["flow-alpha", "flow-beta"])

    def test_reopened_draft_stays_in_draft_bucket(self):
        """驳回后显式重开的新草稿仍算 draft，不被“全局最新 500”挤掉。"""
        reopened = self._insert_detections(1, prefix="ro")[0]
        rejected = self._insert_detections(1, prefix="rj")[0]
        self._insert_version(reopened, 2, "rejected")
        self._insert_version(reopened, 3, "draft")
        self._insert_version(rejected, 4, "rejected")

        conn = self.conn()
        try:
            data = review_service.queue(conn, self.alice["id"], self.project["id"], limit=10)
        finally:
            conn.close()

        self.assertEqual(data["counts"]["draft"], 1)
        self.assertEqual(data["counts"]["rejected"], 1)
        self.assertEqual(data["counts"]["pending"], 0)


if __name__ == "__main__":
    unittest.main()
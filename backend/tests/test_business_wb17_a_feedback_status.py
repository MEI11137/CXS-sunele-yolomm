"""WB17-整轮收尾 A：反馈状态口径（按当前人工确认版本）钉住的行为。

派发点名：
* 反馈状态按「当前人工确认版本」显示 未入池/已入池/已回流；
* 已入池或已回流不再计为可反馈（列表 feedback_status 与页面同口径）；
* 旧版本回流/入池记录不能阻止新人工版本再次入池；
* 服务层批量给状态，页面与 Agent 共用（list/get 同一字段）。
"""

from __future__ import annotations

import unittest

from backend.services import records as record_service
from backend.services import review as review_service
from backend.tests.support import body
from backend.tests.test_business_wb17_04 import ReviewBase


class FeedbackStatusTest(ReviewBase):
    def _status(self, record_id: str) -> str:
        conn = self.conn()
        try:
            return review_service.feedback_statuses_for_records(
                conn, self.alice["id"], [record_id]
            )[record_id]
        finally:
            conn.close()

    def _mark_reflowed(self, record_id: str) -> None:
        conn = self.conn()
        try:
            conn.execute(
                "UPDATE record_feedback SET status = 'reflowed', dataset_id = 'ds_fake'"
                " WHERE detection_id = ?",
                (record_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def test_status_lifecycle_follows_current_confirmed_version(self):
        record = self.record(key="fbk-life")
        self.confirm(record["id"], key="fbk-v1")

        self.assertEqual(self._status(record["id"]), "none", "确认后未入池就是 none")

        conn = self.conn()
        try:
            record_service.add_feedback(
                conn, self.alice["id"], self.project["id"], record["id"], request_key="fbk-add-1"
            )
        finally:
            conn.close()
        self.assertEqual(self._status(record["id"]), "pending", "当前版本入池后是 pending")

        self._mark_reflowed(record["id"])
        self.assertEqual(self._status(record["id"]), "reflowed", "当前版本回流后是 reflowed")

        # 新人工确认版本（v2）：旧版本回流不能挡住新版本，状态回到未入池。
        conn = self.conn()
        try:
            version = review_service.submit(
                conn,
                self.alice["id"],
                self.project["id"],
                record["id"],
                boxes=[{"class_id": 1, "bbox": [12, 12, 46, 38], "severity": "medium"}],
                outcome="defect",
                base_version=1,
                request_key="fbk-v2",
            )[0]
        finally:
            conn.close()
        self.assertEqual(version["version"], 2)
        self.assertEqual(
            self._status(record["id"]),
            "none",
            "旧版本回流记录不能算到新人工版本头上；新版本应可再次加入",
        )
        conn = self.conn()
        try:
            feedback, created = record_service.add_feedback(
                conn, self.alice["id"], self.project["id"], record["id"], request_key="fbk-add-2"
            )
        finally:
            conn.close()
        self.assertTrue(created, "新版本必须能再次入池（版本不同就是另一条反馈）")
        self.assertNotEqual(feedback["review_version_id"], "")
        self.assertEqual(self._status(record["id"]), "pending")

    def test_old_version_pending_does_not_block_new_version(self):
        record = self.record(key="fbk-old")
        self.confirm(record["id"], key="fbk-old-v1")
        conn = self.conn()
        try:
            record_service.add_feedback(
                conn, self.alice["id"], self.project["id"], record["id"], request_key="fbk-old-add"
            )
        finally:
            conn.close()
        conn = self.conn()
        try:
            review_service.submit(
                conn,
                self.alice["id"],
                self.project["id"],
                record["id"],
                boxes=[{"class_id": 1, "bbox": [10, 10, 44, 36], "severity": "low"}],
                outcome="defect",
                base_version=1,
                request_key="fbk-old-v2",
            )
        finally:
            conn.close()
        self.assertEqual(
            self._status(record["id"]),
            "none",
            "v1 在池里不代表 v2 已入池；新版本仍可加入反馈",
        )

    def test_unconfirmed_record_is_none(self):
        record = self.record(key="fbk-un")
        self.assertEqual(self._status(record["id"]), "none", "未确认的记录没有反馈状态可言")

    def test_list_and_get_record_carry_feedback_status(self):
        record = self.record(key="fbk-http")
        self.confirm(record["id"], key="fbk-http-v1")
        conn = self.conn()
        try:
            record_service.add_feedback(
                conn, self.alice["id"], self.project["id"], record["id"], request_key="fbk-http-add"
            )
        finally:
            conn.close()
        listed = self.client.get(f"/api/projects/{self.project['id']}/detections")
        row = next(
            item for item in body(listed)["data"]["records"] if item["id"] == record["id"]
        )
        self.assertEqual(row["feedback_status"], "pending", "列表必须带服务端的反馈状态")
        single = self.client.get(
            f"/api/projects/{self.project['id']}/detections/{record['id']}"
        )
        data = body(single)["data"]["record"]
        self.assertEqual(data["feedback_status"], "pending", "按 ID 授权读取与列表同一口径")


if __name__ == "__main__":
    unittest.main()

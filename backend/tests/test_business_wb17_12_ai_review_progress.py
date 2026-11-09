"""WB17-12 AI 核查进度终态计数。

反例来自真实 job：``job_3de7484a2237821b`` status=done、finished_at 已写，但
progress 仍是 ``{done:1,failed:0,total:2}``，两条实际都成功。根因是 worker 在
每条**处理前**写进度，最后一条处理完直接 return，jobs._run 只落 result/status，
于是终态永远停在 N-1。这里用假 ``_review_one`` 钉住：2 条成功 = done 2；1 成功
1 失败 = done 1 / failed 1，不调用任何真实模型。
"""

from __future__ import annotations

import secrets
import unittest
from unittest import mock

from backend.errors import ApiError
from backend.services import aireview, jobs as job_service
from backend.tests.test_business_wb17_03 import BusinessBase, png_bytes


class AiReviewProgressTest(BusinessBase):
    """隔离测试库 + 假单条核查，不产生 API 费用。"""

    def _job(self, record_ids: list[str]):
        conn = self.conn()
        try:
            job, created = job_service.create(
                conn,
                project_id=self.project["id"],
                owner_id=int(self.alice["id"]),
                kind="ai_review",
                request_key=f"ai-progress-{secrets.token_hex(8)}",
                payload={
                    "record_ids": record_ids,
                    "model": "stub-vision",
                    "provider": "stub",
                    "connection_fingerprint": "stub-fp",
                },
                session_id=self.session["id"],
            )
            self.assertTrue(created)
            return job
        finally:
            conn.close()

    def _run(self, job, side_effect):
        conn = self.conn()
        try:
            with mock.patch.object(aireview, "_review_one", side_effect=side_effect):
                result = aireview.run_batch(conn, job)
            row = job_service.get(conn, job["id"])
            return result, job_service.public(row)["progress"]
        finally:
            conn.close()

    def test_two_successes_progress_is_two(self):
        job = self._job(["det_progress_a", "det_progress_b"])
        result, progress = self._run(
            job,
            [
                {"status": "done", "review": {"id": "r1"}},
                {"status": "done", "review": {"id": "r2"}},
            ],
        )
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["done"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(progress, {"done": 2, "failed": 0, "total": 2})

    def test_mixed_success_failure_counts_match(self):
        job = self._job(["det_progress_c", "det_progress_d"])
        result, progress = self._run(
            job,
            [
                {"status": "done", "review": {"id": "r3"}},
                ApiError(502, "VISION_UNVERIFIED", "图像验证未通过"),
            ],
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["done"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(progress, {"done": 1, "failed": 1, "total": 2})

    def test_vision_payload_contains_both_images(self):
        """双模态记录构造出的真实请求必须包含两张图（脱敏：只数 image_url 与文字模态）。"""
        rgb = self.upload_asset(png_bytes(160, 90), "progress-rgb.png", role="rgb")
        ir = self.upload_asset(png_bytes(90, 120, color=(9, 9, 9)), "progress-ir.png", role="ir")
        record_id = f"det_{secrets.token_hex(8)}"
        conn = self.conn()
        try:
            conn.execute(
                """
                INSERT INTO detections
                    (id, project_id, owner_id, request_key, model_id, rgb_asset_id,
                     x_asset_id, status, boxes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'done', ?, ?)
                """,
                (
                    record_id,
                    self.project["id"],
                    int(self.alice["id"]),
                    f"vision-payload-{secrets.token_hex(4)}",
                    "mdl_progress_missing",
                    rgb["id"],
                    ir["id"],
                    '[{"class_name":"热斑","confidence":0.9,"bbox":[1,2,30,40]}]',
                    "2026-11-09T00:00:00Z",
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM detections WHERE id = ?", (record_id,)).fetchone()
            images = aireview._review_images(conn, int(self.alice["id"]), self.project["id"], row)
        finally:
            conn.close()
        image_parts = [part for part in images if part.get("type") == "image_url"]
        texts = [part.get("text", "") for part in images if part.get("type") == "text"]
        self.assertEqual(len(image_parts), 2)
        self.assertTrue(any("可见光 RGB" in text for text in texts))
        self.assertTrue(any("红外" in text for text in texts))
        payload = aireview._payload(row, images)
        content = payload["messages"][1]["content"]
        self.assertEqual(sum(1 for part in content if part.get("type") == "image_url"), 2)
        # data URI 只证明构造，不把 base64 写进测试输出/日志。
        self.assertTrue(all(str(part["image_url"]["url"]).startswith("data:image/") for part in image_parts))


if __name__ == "__main__":
    unittest.main()
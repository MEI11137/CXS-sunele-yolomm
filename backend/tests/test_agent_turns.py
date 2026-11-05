"""轮次 API 与运行器：最终回答判定、幂等、停止、事件续拉、权限模式传递。

引擎被替换成按脚本返回消息的假实现；所有断言都看**真实落库的消息与事件**，
不看实现内部变量。
"""

from __future__ import annotations

import json
import time
import unittest
import uuid

from backend import db
from backend.agent import identity, store
from backend.tests.support import Harness, api, body, login, wait_turn


class TurnTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(fake_engine=True)
        self.alice = self.harness.create_user("alice", role="admin")
        self.root = self.harness.make_dir("alice-root")
        (self.root / "readme.txt").write_text("巡检记录", encoding="utf-8")
        self.root_row = self.harness.add_root(self.root, grant_to=["alice"])
        self.project = self.harness.create_project(self.alice["id"], "光伏项目")
        self.harness.bind_project(self.project["id"], self.root, "alice")
        self.session = self.harness.create_session(self.project["id"], self.alice["id"])
        self.harness.save_connection(self.alice["id"])
        self.client = self.harness.client()
        login(self.client, "alice")
        self.fake = self.harness.fake
        assert self.fake is not None

    def tearDown(self):
        self.harness.close()

    # ------------------------------------------------------------------ 帮助

    def send(self, content="帮我看看目录", *, request_id: str | None = None, mode: str | None = None):
        if mode:
            self.harness.set_mode(self.alice["id"], mode)
        payload = {"content": content, "request_id": request_id or uuid.uuid4().hex}
        return api(self.client, "POST", f"/api/sessions/{self.session['id']}/turns", payload)

    def messages(self) -> list[dict]:
        response = api(self.client, "GET", f"/api/sessions/{self.session['id']}/messages")
        return body(response)["data"]["messages"]

    def assistant_texts(self) -> list[str]:
        return [item["content"] for item in self.messages() if item["role"] == "assistant"]

    # ------------------------------------------------------------------ 正常完成

    def test_turn_completes_with_real_final_text(self):
        self.fake.queue({"text": "目录里有 readme.txt，共 1 个文件。"})
        response = self.send()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        turn = body(response)["data"]["turn"]
        final = wait_turn(self.client, turn["id"])
        self.assertEqual(final["turn"]["status"], "completed")
        self.assertEqual(self.assistant_texts(), ["目录里有 readme.txt，共 1 个文件。"])
        kinds = [event["type"] for event in final["events"]]
        self.assertIn("text", kinds)
        self.assertIn("final", kinds)

    def test_tool_round_then_final_answer(self):
        self.fake.queue(
            {
                "tool_calls": [
                    {
                        "tool": "hx17_hx_list_files",
                        "args": {"path": "."},
                        "output": '{"entries": []}',
                        "title": "列出目录",
                    }
                ],
                "text": "工具执行完了，目录是空的。",
            }
        )
        turn = body(self.send())["data"]["turn"]
        final = wait_turn(self.client, turn["id"])
        self.assertEqual(final["turn"]["status"], "completed")
        tool_events = [event for event in final["events"] if event["type"] == "tool"]
        self.assertTrue(tool_events)
        self.assertEqual(tool_events[-1]["payload"]["state"], "completed")
        self.assertEqual(self.assistant_texts(), ["工具执行完了，目录是空的。"])

    def test_compaction_summary_is_never_shown(self):
        self.fake.queue({"summary": True, "text": "真正的回答"})
        turn = body(self.send())["data"]["turn"]
        wait_turn(self.client, turn["id"])
        texts = self.assistant_texts()
        self.assertEqual(texts, ["真正的回答"])
        self.assertNotIn("压缩摘要", " ".join(texts))

    def test_synthetic_text_is_not_counted_as_answer(self):
        self.fake.queue({"text": "可见回答", "synthetic_extra": True})
        turn = body(self.send())["data"]["turn"]
        wait_turn(self.client, turn["id"])
        self.assertEqual(self.assistant_texts(), ["可见回答"])

    def test_brief_idle_does_not_finish_early(self):
        self.fake.queue({"idle_blip": True, "blip_seconds": 0.8, "text": "迟到但完整的回答"})
        turn = body(self.send())["data"]["turn"]
        final = wait_turn(self.client, turn["id"])
        self.assertEqual(final["turn"]["status"], "completed")
        self.assertEqual(self.assistant_texts(), ["迟到但完整的回答"])

    def test_text_arriving_after_idle_is_still_the_answer(self):
        self.fake.queue({"idle_before_text": True, "idle_gap": 2.0, "text": "空闲之后才出现的回答"})
        turn = body(self.send())["data"]["turn"]
        final = wait_turn(self.client, turn["id"], timeout=25)
        self.assertEqual(final["turn"]["status"], "completed")
        self.assertEqual(self.assistant_texts(), ["空闲之后才出现的回答"])

    def test_no_final_text_fails_honestly(self):
        self.fake.queue({"no_text": True})
        turn = body(self.send())["data"]["turn"]
        final = wait_turn(self.client, turn["id"], timeout=25)
        self.assertEqual(final["turn"]["status"], "failed")
        self.assertEqual(final["turn"]["error_code"], "NO_FINAL_TEXT")
        # 不能塞一条固定假回复。
        self.assertEqual(self.assistant_texts(), [])

    def test_engine_error_is_mapped(self):
        self.fake.queue(
            {"error": {"name": "ProviderAuthError", "data": {"message": "bad key"}}}
        )
        turn = body(self.send())["data"]["turn"]
        final = wait_turn(self.client, turn["id"])
        self.assertEqual(final["turn"]["status"], "failed")
        self.assertEqual(final["turn"]["error_code"], "PROVIDER_AUTH")
        self.assertIn("个人连接", final["turn"]["error_message"])

    # ------------------------------------------------------------------ 幂等 / 并发

    def test_same_request_id_returns_same_turn_and_one_message(self):
        self.fake.queue({"text": "第一次回答"})
        request_id = "idem-" + uuid.uuid4().hex[:12]
        first = body(self.send(request_id=request_id))["data"]
        second = body(self.send(request_id=request_id))["data"]
        self.assertEqual(first["turn"]["id"], second["turn"]["id"])
        self.assertTrue(second["resumed"])
        wait_turn(self.client, first["turn"]["id"])
        user_messages = [item for item in self.messages() if item["role"] == "user"]
        self.assertEqual(len(user_messages), 1)
        self.assertEqual(len(self.fake.prompts), 1)

    def test_second_turn_is_refused_while_one_is_active(self):
        self.fake.queue({"hang": True})
        body(self.send())
        time.sleep(0.4)
        response = api(
            self.client,
            "POST",
            f"/api/sessions/{self.session['id']}/turns",
            {"content": "再来一轮", "request_id": uuid.uuid4().hex},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(body(response)["error"], "TURN_BUSY")

    def test_stop_marks_turn_stopped(self):
        self.fake.queue({"hang": True})
        turn = body(self.send())["data"]["turn"]
        time.sleep(0.6)
        response = api(self.client, "POST", f"/api/turns/{turn['id']}/stop")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body(response)["data"]["stopped"])
        final = wait_turn(self.client, turn["id"], timeout=25)
        self.assertEqual(final["turn"]["status"], "stopped")
        self.assertTrue(self.fake.aborts)
        # 停止后不能生成假的最终回答。
        self.assertEqual(self.assistant_texts(), [])

    def test_stop_is_idempotent_after_finish(self):
        self.fake.queue({"text": "done"})
        turn = body(self.send())["data"]["turn"]
        wait_turn(self.client, turn["id"])
        response = api(self.client, "POST", f"/api/turns/{turn['id']}/stop")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body(response)["data"]["already"])

    # ------------------------------------------------------------------ 校验

    def test_turn_requires_personal_connection(self):
        self.harness.delete_connection(self.alice["id"])
        response = self.send()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(body(response)["error"], "CONNECTION_MISSING")

    def test_turn_requires_bound_directory(self):
        unbind = api(
            self.client,
            "DELETE",
            f"/api/projects/{self.project['id']}/root",
        )
        self.assertEqual(unbind.status_code, 200)
        response = self.send()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(body(response)["error"], "NO_PROJECT_ROOT")

    def test_mode_comes_from_saved_preferences(self):
        self.harness.set_mode(self.alice["id"], "full")
        self.fake.queue({"text": "ok"})
        turn = body(self.send())["data"]["turn"]
        self.assertEqual(turn["mode"], "full")
        wait_turn(self.client, turn["id"])

    def test_preferences_validation(self):
        bad = api(self.client, "PUT", "/api/me/preferences", {"mode": "root"})
        self.assertEqual(bad.status_code, 400)
        unknown_model = api(self.client, "PUT", "/api/me/preferences", {"model": "not-a-model"})
        self.assertEqual(unknown_model.status_code, 400)
        good = api(self.client, "PUT", "/api/me/preferences", {"mode": "assist"})
        self.assertEqual(good.status_code, 200)
        self.assertEqual(body(good)["data"]["mode"], "assist")

    def test_other_user_cannot_read_turn(self):
        self.fake.queue({"hang": True})
        turn = body(self.send())["data"]["turn"]
        other = self.harness.client()
        self.harness.create_user("bob")
        login(other, "bob")
        response = api(other, "GET", f"/api/turns/{turn['id']}")
        self.assertEqual(response.status_code, 404)
        api(self.client, "POST", f"/api/turns/{turn['id']}/stop")

    # ------------------------------------------------------------------ 事件续拉

    def test_events_are_incremental(self):
        self.fake.queue({"text": "可续拉的回答"})
        turn = body(self.send())["data"]["turn"]
        wait_turn(self.client, turn["id"])
        full = body(api(self.client, "GET", f"/api/turns/{turn['id']}"))["data"]["events"]
        self.assertGreaterEqual(len(full), 2)
        cursor = full[0]["seq"]
        tail = body(
            api(self.client, "GET", f"/api/turns/{turn['id']}?after={cursor}")
        )["data"]["events"]
        self.assertEqual(len(tail), len(full) - 1)
        empty = body(
            api(self.client, "GET", f"/api/turns/{turn['id']}?after={full[-1]['seq']}")
        )["data"]["events"]
        self.assertEqual(empty, [])

    def test_sse_endpoint_replays_and_ends(self):
        self.fake.queue({"text": "SSE 回应"})
        turn = body(self.send())["data"]["turn"]
        wait_turn(self.client, turn["id"])
        response = self.client.get(f"/api/turns/{turn['id']}/stream")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["Content-Type"])
        raw = response.get_data(as_text=True)
        self.assertIn('"type": "final"', raw)
        self.assertIn('"type": "end"', raw)

    def test_stale_fingerprint_turn_fails_before_calling_engine(self):
        """已排队但连接被换掉的轮次必须失败，不能拿旧配置继续。"""
        conn = self.harness.conn()
        try:
            connection = identity.connection_for(conn, self.alice["id"])
            turn, _ = store.create(
                conn,
                session_id=self.session["id"],
                project_id=self.project["id"],
                owner_id=self.alice["id"],
                request_id="stale-" + uuid.uuid4().hex[:8],
                mode="request",
                provider=str(connection["provider"]),
                model=str(connection["model"]),
                fingerprint="deadbeef" * 4,
            )
        finally:
            conn.close()
        self.harness.app.config["HX17_AGENT"].runner.start(turn["id"])
        final = wait_turn(self.client, turn["id"], timeout=20)
        self.assertEqual(final["turn"]["status"], "failed")
        self.assertEqual(final["turn"]["error_code"], "CONNECTION_CHANGED")
        self.assertEqual(self.fake.prompts, [])

    # ------------------------------------------------------------------ 历史与失效

    def test_history_beyond_page_size_keeps_latest(self):
        from backend.services import sessions as session_service

        conn = self.harness.conn()
        try:
            for index in range(240):
                session_service.append_message(
                    conn,
                    session_id=self.session["id"],
                    role="user",
                    content=f"旧消息 {index}",
                )
        finally:
            conn.close()
        self.fake.queue({"text": "最新一轮的回答"})
        turn = body(self.send("这是第 241 条用户消息"))["data"]["turn"]
        final = wait_turn(self.client, turn["id"])
        self.assertEqual(final["turn"]["status"], "completed")

        payload = body(
            api(self.client, "GET", f"/api/sessions/{self.session['id']}/messages")
        )["data"]
        texts = [item["content"] for item in payload["messages"]]
        # 旧实现按 id 升序取前 200 条，新消息会整页消失。
        self.assertIn("这是第 241 条用户消息", texts)
        self.assertIn("最新一轮的回答", texts)
        self.assertTrue(payload["has_more"])

        before = payload["next_before"]
        older = body(
            api(
                self.client,
                "GET",
                f"/api/sessions/{self.session['id']}/messages?before={before}",
            )
        )["data"]
        self.assertTrue(older["messages"])
        self.assertTrue(all(item["id"] < before for item in older["messages"]))

    def test_final_save_failure_does_not_mark_completed(self):
        from unittest import mock

        self.fake.queue({"text": "这段回答不会保存成功"})
        turn = body(self.send())["data"]["turn"]
        with mock.patch(
            "backend.services.sessions.append_message",
            side_effect=RuntimeError("disk full"),
        ):
            final = wait_turn(self.client, turn["id"])
        self.assertEqual(final["turn"]["status"], "failed")
        self.assertEqual(final["turn"]["error_code"], "FINAL_NOT_SAVED")
        self.assertEqual(self.assistant_texts(), [])

    def test_connection_removed_mid_turn_stops_it(self):
        self.fake.queue({"initial_delay": 3.0, "text": "不该作为成功完成"})
        turn = body(self.send())["data"]["turn"]
        time.sleep(1.0)
        self.harness.delete_connection(self.alice["id"])
        final = wait_turn(self.client, turn["id"], timeout=25)
        self.assertEqual(final["turn"]["status"], "failed")
        self.assertEqual(final["turn"]["error_code"], "CONNECTION_CHANGED")
        self.assertEqual(self.assistant_texts(), [])

    def test_pending_action_is_not_expired_by_completion(self):
        self.fake.queue({"initial_delay": 1.5, "text": "我先等你确认写入。"})
        turn = body(self.send(mode="request"))["data"]["turn"]
        conn = self.harness.conn()
        try:
            action = store.create_action(
                conn,
                turn_id=turn["id"],
                session_id=self.session["id"],
                project_id=self.project["id"],
                owner_id=self.alice["id"],
                tool="hx_write_file",
                risk="write",
                title="写入报告",
                args={"path": "r.txt", "content": "x"},
                preview="r.txt",
            )
        finally:
            conn.close()
        final = wait_turn(self.client, turn["id"])
        self.assertEqual(final["turn"]["status"], "completed")
        conn = self.harness.conn()
        try:
            row = store.get_action(conn, action["id"])
        finally:
            conn.close()
        self.assertEqual(row["status"], "proposed")

    def test_delete_session_mid_turn_interrupts_it(self):
        self.fake.queue({"initial_delay": 3.0, "text": "会话已经没了"})
        turn = body(self.send())["data"]["turn"]
        time.sleep(0.8)
        response = api(self.client, "DELETE", f"/api/sessions/{self.session['id']}")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        deadline = time.time() + 10
        status = ""
        while time.time() < deadline:
            conn = self.harness.conn()
            try:
                row = store.get(conn, turn["id"])
            finally:
                conn.close()
            status = row["status"]
            if status in store.TERMINAL_STATUSES:
                break
            time.sleep(0.2)
        self.assertEqual(status, "failed")


class ActionContinuationTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(fake_engine=True)
        self.alice = self.harness.create_user("alice", role="admin")
        self.root = self.harness.make_dir("alice-root")
        self.root_row = self.harness.add_root(self.root, grant_to=["alice"])
        self.project = self.harness.create_project(self.alice["id"], "项目")
        self.harness.bind_project(self.project["id"], self.root, "alice")
        self.session = self.harness.create_session(self.project["id"], self.alice["id"])
        self.harness.save_connection(self.alice["id"])
        self.harness.set_mode(self.alice["id"], "request")
        self.client = self.harness.client()
        login(self.client, "alice")

    def tearDown(self):
        self.harness.close()

    def test_approved_action_triggers_continuation_turn(self):
        conn = self.harness.conn()
        try:
            action = store.create_action(
                conn,
                turn_id=self._create_turn(conn),
                session_id=self.session["id"],
                project_id=self.project["id"],
                owner_id=self.alice["id"],
                tool="hx_write_file",
                risk="write",
                title="写入报告",
                args={"path": "report.txt", "content": "巡检完成"},
                preview="写入 report.txt（12 字节）",
            )
            action_id = action["id"]
        finally:
            conn.close()

        self.harness.fake.queue({"text": "报告已写入，继续下一步。"})
        response = api(self.client, "POST", f"/api/actions/{action_id}/approve")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = body(response)["data"]
        self.assertTrue(payload["executed"])
        self.assertEqual((self.root / "report.txt").read_text(encoding="utf-8"), "巡检完成")
        self.assertIsNotNone(payload["turn"])
        final = wait_turn(self.client, payload["turn"]["id"], timeout=25)
        self.assertEqual(final["turn"]["status"], "completed")

        conn = self.harness.conn()
        try:
            texts = [
                row["content"]
                for row in conn.execute(
                    "SELECT content FROM messages WHERE session_id = ? AND role = 'assistant'",
                    (self.session["id"],),
                ).fetchall()
            ]
            kinds = [row["kind"] for row in conn.execute("SELECT kind FROM turns").fetchall()]
        finally:
            conn.close()
        self.assertIn("报告已写入，继续下一步。", texts)
        self.assertIn("continuation", kinds)


    def _create_turn(self, conn) -> str:
        connection = identity.connection_for(conn, self.alice["id"])
        turn, _ = store.create(
            conn,
            session_id=self.session["id"],
            project_id=self.project["id"],
            owner_id=self.alice["id"],
            request_id="act-" + uuid.uuid4().hex[:8],
            mode="request",
            provider=str(connection["provider"]),
            model=str(connection["model"]),
            fingerprint=identity.connection_fingerprint(connection),
        )
        store.finish(conn, turn["id"], "completed")
        return turn["id"]


class CapabilitiesTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.harness.create_user("alice", role="admin")
        self.client = self.harness.client()
        login(self.client, "alice")

    def tearDown(self):
        self.harness.close()

    def test_capabilities_expose_tools_without_secrets(self):
        response = api(self.client, "GET", "/api/agent/capabilities")
        self.assertEqual(response.status_code, 200)
        data = body(response)["data"]
        names = {item["name"] for item in data["tools"]}
        self.assertIn("hx_list_files", names)
        self.assertNotIn("hx_run_shell", names)
        self.assertFalse(data["runtime"]["shell_enabled"])
        raw = response.get_data(as_text=True)
        self.assertNotIn(str(self.harness.data_dir), raw)
        self.assertNotIn("api_key", raw)

    def test_services_report_not_available(self):
        data = body(api(self.client, "GET", "/api/agent/capabilities"))["data"]
        # 已接入的服务必须如实报告为可用（WB17-03 检测/核查，WB17-04 复核/反馈/回流）。
        for name in ("detection", "ai_review", "datasets", "models", "review", "feedback", "reflow"):
            self.assertTrue(data["services"][name]["available"], name)
        # 还没接入的一律保持不可用，不能“先声明后实现”。
        for name in ("training",):
            self.assertFalse(data["services"][name]["available"], name)

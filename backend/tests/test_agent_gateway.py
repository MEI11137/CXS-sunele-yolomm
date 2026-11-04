"""私有工具网关：令牌换身份、权限模式、越界与并发幂等。

这些用例都走**真实 HTTP**（127.0.0.1 上的回环网关）与真实文件系统，
只有引擎被替换成假实现——因为本轮不调用任何付费模型。
"""

from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

import requests

from backend.agent import identity, policy, store
from backend.agent.gateway import TOKEN_HEADER
from backend.services import projects as project_service
from backend.services import users as user_service
from backend.tests.support import Harness, api, body, login, wait_turn

PASSWORD = "correct-horse-9"


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.alice = self.harness.create_user("alice", role="admin")
        self.root = self.harness.make_dir("alice-project")
        (self.root / "note.txt").write_text("hello world", encoding="utf-8")
        (self.root / "sub").mkdir(exist_ok=True)
        self.root_row = self.harness.add_root(self.root, grant_to=["alice"])
        self.project = self.harness.create_project(self.alice["id"], "光伏项目")
        self.harness.bind_project(self.project["id"], self.root, "alice")
        self.session = self.harness.create_session(self.project["id"], self.alice["id"])
        self.harness.save_connection(self.alice["id"])
        self.runtime = self.harness.app.config["HX17_AGENT"]

    def tearDown(self):
        self.harness.close()

    # ------------------------------------------------------------------ 帮助

    def _turn(self, *, mode: str = "request", request_id: str = "req-aaaaaaaa") -> dict:
        conn = self.harness.conn()
        try:
            connection = identity.connection_for(conn, self.alice["id"])
            turn, _ = store.create(
                conn,
                session_id=self.session["id"],
                project_id=self.project["id"],
                owner_id=self.alice["id"],
                request_id=request_id,
                mode=mode,
                provider=str(connection["provider"]),
                model=str(connection["model"]),
                fingerprint=identity.connection_fingerprint(connection),
            )
            store.mark_running(conn, turn["id"])
            return dict(turn)
        finally:
            conn.close()

    def _token(self, turn: dict, *, session: dict | None = None) -> str:
        target = session or self.session
        return self.runtime.gateway.issue_session(
            user_id=self.alice["id"],
            session_id=target["id"],
            project_id=self.project["id"],
            fingerprint=turn["connection_fingerprint"],
        )

    def call(self, token: str, tool: str, args: dict | None = None, *, call_id: str | None = None):
        response = requests.post(
            f"{self.runtime.gateway.base_url}/v1/tools/{tool}",
            headers={TOKEN_HEADER: token},
            json={"args": args or {}, "call_id": call_id or uuid.uuid4().hex},
            timeout=30,
        )
        return response.status_code, response.json()

    @staticmethod
    def inner(payload: dict) -> dict:
        """业务结果在 ok/data 里；身份错误在 HTTP 层直接就是 4xx。"""
        return payload.get("data") or payload

    # ------------------------------------------------------------------ 身份

    def test_gateway_binds_to_loopback_only(self):
        self.assertEqual(self.runtime.gateway.base_url, f"http://127.0.0.1:{self.runtime.gateway.port}")

    def test_missing_token_is_rejected(self):
        response = requests.post(
            f"{self.runtime.gateway.base_url}/v1/tools/hx_project_overview",
            json={"args": {}, "call_id": "x"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "TOKEN_INVALID")

    def test_token_from_other_user_cannot_use_session(self):
        self.harness.create_user("bob")
        turn = self._turn()
        # 用 alice 的轮次签发令牌，但把令牌换成别的用户的轮次身份是不可能的：
        # 这里验证 bob 的令牌根本拿不到 alice 的上下文。
        bob_token = self.runtime.gateway.issue_session(
            user_id=9999,
            session_id=self.session["id"],
            project_id=self.project["id"],
            fingerprint=turn["connection_fingerprint"],
        )
        status, payload = self.call(bob_token, "hx_project_overview")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "ACTOR_MISMATCH")

    def test_unknown_tool_is_rejected_without_listing_fallback(self):
        token = self._token(self._turn())
        status, payload = self.call(token, "hx_run_shell", {"cmd": "dir"})
        # 业务层拒绝：结构化返回，模型能读到原因；不是只有一句“工具未连接”。
        self.assertEqual(status, 200)
        self.assertEqual(self.inner(payload)["error"]["code"], "TOOL_NOT_FOUND")

    def test_argument_schema_is_enforced(self):
        token = self._token(self._turn())
        status, payload = self.call(token, "hx_read_file", {})
        self.assertEqual(self.inner(payload)["error"]["code"], "MISSING_ARGUMENT")
        status, payload = self.call(token, "hx_read_file", {"path": "note.txt", "bogus": 1})
        self.assertEqual(self.inner(payload)["error"]["code"], "UNKNOWN_ARGUMENT")
        status, payload = self.call(token, "hx_read_file", {"path": "note.txt", "offset": -1})
        self.assertEqual(self.inner(payload)["error"]["code"], "ARGUMENT_OUT_OF_RANGE")

    # ------------------------------------------------------------------ 读写

    def test_read_list_and_search(self):
        token = self._token(self._turn())
        status, payload = self.call(token, "hx_list_files", {"path": "."})
        self.assertEqual(status, 200)
        names = {item["name"] for item in self.inner(payload)["result"]["entries"]}
        self.assertIn("note.txt", names)
        status, payload = self.call(token, "hx_read_file", {"path": "note.txt"})
        self.assertEqual(self.inner(payload)["result"]["text"], "hello world")
        status, payload = self.call(token, "hx_search_files", {"query": "note"})
        self.assertEqual(self.inner(payload)["result"]["hits"][0]["relative"], "note.txt")

    def test_path_escape_is_rejected_and_nothing_written(self):
        token = self._token(self._turn(mode="full"))
        outside = self.harness.workspace_dir / "outside.txt"
        status, payload = self.call(
            token, "hx_write_file", {"path": str(outside), "content": "x"}
        )
        self.assertEqual(self.inner(payload)["error"]["code"], "PATH_OUTSIDE_PROJECT")
        self.assertFalse(outside.exists())

    def test_delete_goes_to_trash(self):
        token = self._token(self._turn(mode="full"))
        target = self.root / "sub" / "tmp.txt"
        target.write_text("x", encoding="utf-8")
        status, payload = self.call(token, "hx_delete_path", {"path": "sub/tmp.txt"})
        self.assertEqual(status, 200)
        self.assertFalse(target.exists())
        self.assertTrue(self.inner(payload)["result"]["recoverable"])
        trash = self.root / ".hx17-trash"
        self.assertTrue(any(trash.iterdir()))
        self.assertFalse(any(item.name == "tmp.txt" for item in trash.iterdir()))

    # ------------------------------------------------------------------ 权限模式

    def test_request_mode_needs_confirmation_and_does_not_write(self):
        token = self._token(self._turn(mode="request"))
        status, payload = self.call(
            token, "hx_write_file", {"path": "new.txt", "content": "hi"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.inner(payload)["status"], "pending_confirmation")
        self.assertFalse((self.root / "new.txt").exists())
        action_id = self.inner(payload)["action_id"]
        self.assertTrue(action_id.startswith("act_"))

        # 用 HTTP 确认后动作才真正执行。
        client = self.harness.client()
        login(client, "alice")
        response = api(client, "POST", f"/api/actions/{action_id}/approve")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(body(response)["data"]["executed"])
        self.assertEqual((self.root / "new.txt").read_text(encoding="utf-8"), "hi")

        # 重复确认是幂等的：不会再写一次，也不会报错。
        again = api(client, "POST", f"/api/actions/{action_id}/approve")
        self.assertEqual(again.status_code, 200)
        self.assertFalse(body(again)["data"]["executed"])
        self.assertEqual((self.root / "new.txt").read_text(encoding="utf-8"), "hi")

    def test_assist_mode_auto_writes_but_confirms_delete(self):
        token = self._token(self._turn(mode="assist", request_id="req-assist1"))
        status, payload = self.call(
            token, "hx_write_file", {"path": "auto.txt", "content": "ok"}
        )
        self.assertEqual(self.inner(payload)["status"], "ok")
        self.assertTrue((self.root / "auto.txt").is_file())
        status, payload = self.call(token, "hx_delete_path", {"path": "auto.txt"})
        self.assertEqual(self.inner(payload)["status"], "pending_confirmation")
        self.assertTrue((self.root / "auto.txt").is_file())

    def test_assist_mode_confirms_overwrite_of_existing_file(self):
        token = self._token(self._turn(mode="assist", request_id="req-assist2"))
        status, payload = self.call(
            token, "hx_write_file", {"path": "note.txt", "content": "changed"}
        )
        self.assertEqual(self.inner(payload)["status"], "pending_confirmation")
        self.assertEqual((self.root / "note.txt").read_text(encoding="utf-8"), "hello world")

    def test_full_mode_executes_and_audits(self):
        token = self._token(self._turn(mode="full"))
        status, payload = self.call(
            token, "hx_write_file", {"path": "full.txt", "content": "done"}
        )
        self.assertEqual(self.inner(payload)["status"], "ok")
        self.assertTrue((self.root / "full.txt").is_file())
        conn = self.harness.conn()
        try:
            rows = conn.execute(
                "SELECT action, detail FROM audit_log WHERE action LIKE 'agent.tool.%'"
            ).fetchall()
        finally:
            conn.close()
        self.assertTrue(rows)
        self.assertIn("hx_write_file", rows[-1]["detail"])

    def test_full_mode_cannot_touch_another_users_project(self):
        self.harness.create_user("bob")
        conn = self.harness.conn()
        try:
            bob_id = int(user_service.find_by_username(conn, "bob")["id"])
        finally:
            conn.close()
        bob_project = self.harness.create_project(bob_id, "bob 项目")
        token = self._token(self._turn(mode="full"))
        # 工具没有 project_id 参数：越权只能通过伪造参数，而 schema 直接拒绝。
        status, payload = self.call(
            token, "hx_project_overview", {"project_id": bob_project["id"]}
        )
        self.assertEqual(self.inner(payload)["error"]["code"], "UNKNOWN_ARGUMENT")

    # ------------------------------------------------------------------ 幂等 / 失效

    def test_same_call_id_is_not_executed_twice(self):
        token = self._token(self._turn(mode="full"))
        call_id = "fixed-call-1"
        status, first = self.call(
            token, "hx_write_file", {"path": "once.txt", "content": "1"}, call_id=call_id
        )
        self.assertEqual(self.inner(first)["status"], "ok")
        (self.root / "once.txt").write_text("changed-by-user", encoding="utf-8")
        # 同一 call_id + 同一参数：原样返回上次结果，不重跑。
        status, second = self.call(
            token, "hx_write_file", {"path": "once.txt", "content": "1"}, call_id=call_id
        )
        self.assertTrue(self.inner(second).get("replayed"))
        self.assertEqual((self.root / "once.txt").read_text(encoding="utf-8"), "changed-by-user")
        # 同一 call_id 换了参数：必须冲突，不能把别的操作当成上次的结果返回。
        status, third = self.call(
            token, "hx_write_file", {"path": "once.txt", "content": "2"}, call_id=call_id
        )
        self.assertEqual(self.inner(third)["error"]["code"], "CALL_ID_CONFLICT")

    def test_connection_change_invalidates_token(self):
        token = self._token(self._turn(mode="full"))
        status, payload = self.call(token, "hx_project_overview")
        self.assertEqual(status, 200)
        self.harness.save_connection(self.alice["id"], api_key="another-key-0987654321")
        status, payload = self.call(token, "hx_project_overview")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "CONNECTION_CHANGED")

    def test_disabled_user_is_rejected(self):
        token = self._token(self._turn(mode="full"))
        conn = self.harness.conn()
        try:
            user_service.set_status(conn, self.alice["id"], "disabled")
        finally:
            conn.close()
        status, payload = self.call(token, "hx_project_overview")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "USER_DISABLED")

    def test_revoked_grant_is_rejected(self):
        token = self._token(self._turn(mode="full"))
        self.harness.revoke_root(self.root_row["id"], self.alice["id"])
        status, payload = self.call(token, "hx_project_overview")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "GRANT_REVOKED")

    def test_finished_turn_cannot_call_tools(self):
        turn = self._turn(mode="full")
        token = self._token(turn)
        conn = self.harness.conn()
        try:
            store.finish(conn, turn["id"], "completed")
        finally:
            conn.close()
        status, payload = self.call(token, "hx_project_overview")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "TURN_FINISHED")

    def test_tool_status_reports_unknown_without_replaying(self):
        token = self._token(self._turn(mode="full"))
        status, payload = self.call(token, "hx_tool_status", {"job_id": "does-not-exist"})
        self.assertEqual(status, 200)
        self.assertEqual(self.inner(payload)["result"]["status"], "unknown")
        self.assertIn("不要重复", self.inner(payload)["result"]["note"])


    # ------------------------------------------------------------------ 会话令牌 / 并发

    def test_session_token_survives_second_turn_and_tracks_active_turn(self):
        first = self._turn(mode="full", request_id="req-turn-1")
        token = self._token(first)
        status, payload = self.call(token, "hx_write_file", {"path": "one.txt", "content": "1"})
        self.assertEqual(self.inner(payload)["status"], "ok")

        conn = self.harness.conn()
        try:
            store.finish(conn, first["id"], "completed")
        finally:
            conn.close()
        # 第一轮结束后，同一令牌不能再调用（没有活动轮次）。
        status, payload = self.call(token, "hx_project_overview")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "TURN_FINISHED")

        second = self._turn(mode="full", request_id="req-turn-2")
        # 第二轮复用同一令牌：它绑定的是会话，不需要（也不能）由模型传 session_id。
        status, payload = self.call(token, "hx_ask_user", {"question": "第二轮还在吗？"})
        self.assertEqual(status, 200)
        conn = self.harness.conn()
        try:
            rows = conn.execute(
                "SELECT turn_id, type FROM turn_events WHERE turn_id IN (?, ?)",
                (first["id"], second["id"]),
            ).fetchall()
            actor_turn = conn.execute(
                "SELECT session_id FROM turns WHERE id = ?", (second["id"],)
            ).fetchone()["session_id"]
        finally:
            conn.close()
        self.assertEqual(actor_turn, self.session["id"])
        self.assertIn((second["id"], "question"), {(row["turn_id"], row["type"]) for row in rows})
        self.assertNotIn((first["id"], "question"), {(row["turn_id"], row["type"]) for row in rows})

    def test_two_sessions_keep_their_own_turns(self):
        other = self.harness.create_session(self.project["id"], self.alice["id"], "另一个会话")
        turn_a = self._turn(mode="full", request_id="req-a")
        conn = self.harness.conn()
        try:
            connection = identity.connection_for(conn, self.alice["id"])
            turn_b, _ = store.create(
                conn,
                session_id=other["id"],
                project_id=self.project["id"],
                owner_id=self.alice["id"],
                request_id="req-b",
                mode="full",
                provider=str(connection["provider"]),
                model=str(connection["model"]),
                fingerprint=identity.connection_fingerprint(connection),
            )
            store.mark_running(conn, turn_b["id"])
            turn_b = dict(turn_b)
        finally:
            conn.close()

        token_a = self._token(turn_a)
        token_b = self.runtime.gateway.issue_session(
            user_id=self.alice["id"],
            session_id=other["id"],
            project_id=self.project["id"],
            fingerprint=turn_b["connection_fingerprint"],
        )
        self.call(token_a, "hx_write_file", {"path": "a.txt", "content": "a"})
        self.call(token_b, "hx_write_file", {"path": "b.txt", "content": "b"})
        self.assertTrue((self.root / "a.txt").is_file())
        self.assertTrue((self.root / "b.txt").is_file())

        # 结束 A 轮后，A 的令牌不能借用 B 的活动轮次。
        conn = self.harness.conn()
        try:
            store.finish(conn, turn_a["id"], "completed")
        finally:
            conn.close()
        status, payload = self.call(token_a, "hx_project_overview")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "TURN_FINISHED")

    def test_concurrent_same_call_id_executes_once(self):
        import threading

        token = self._token(self._turn(mode="full"))
        (self.root / "race.txt").write_text("", encoding="utf-8")
        call_id = "parallel-call-1"
        barrier = threading.Barrier(2)
        results: list[tuple[int, dict]] = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            outcome = self.call(
                token,
                "hx_write_file",
                {"path": "race.txt", "content": "X", "mode": "append"},
                call_id=call_id,
            )
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(results), 2)
        for status, payload in results:
            self.assertEqual(status, 200, payload)
        # 只执行一次：追加内容只能是 "X"，不是 "XX"。
        self.assertEqual((self.root / "race.txt").read_text(encoding="utf-8"), "X")

    def test_grant_revoked_after_proposal_blocks_approval(self):
        token = self._token(self._turn(mode="request", request_id="req-grant"))
        status, payload = self.call(
            token, "hx_write_file", {"path": "late.txt", "content": "x"}
        )
        self.assertEqual(self.inner(payload)["status"], "pending_confirmation")
        action_id = self.inner(payload)["action_id"]
        self.harness.revoke_root(self.root_row["id"], self.alice["id"])

        client = self.harness.client()
        login(client, "alice")
        response = api(client, "POST", f"/api/actions/{action_id}/approve")
        self.assertEqual(response.status_code, 403, response.get_data(as_text=True))
        self.assertEqual(body(response)["error"], "GRANT_REVOKED")
        self.assertFalse((self.root / "late.txt").exists())

    def test_copy_tool_is_registered_and_scoped(self):
        token = self._token(self._turn(mode="full"))
        (self.root / "src.txt").write_text("copy-me", encoding="utf-8")
        status, payload = self.call(
            token, "hx_copy_path", {"from": "src.txt", "to": "dst.txt"}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.inner(payload)["result"]["to"], "dst.txt")
        self.assertEqual((self.root / "dst.txt").read_text(encoding="utf-8"), "copy-me")
        status, payload = self.call(
            token, "hx_copy_path", {"from": "src.txt", "to": "../escape.txt"}
        )
        self.assertEqual(self.inner(payload)["error"]["code"], "PATH_OUTSIDE_PROJECT")

    def test_open_page_and_ask_user_emit_events(self):
        turn = self._turn(mode="full")
        token = self._token(turn)
        self.call(token, "hx_open_page", {"page": "settings"})
        self.call(token, "hx_ask_user", {"question": "要处理哪个子目录？"})
        conn = self.harness.conn()
        try:
            rows = conn.execute(
                "SELECT type, payload FROM turn_events WHERE turn_id = ? ORDER BY seq", (turn["id"],)
            ).fetchall()
        finally:
            conn.close()
        kinds = [row["type"] for row in rows]
        self.assertIn("ui.navigate", kinds)
        self.assertIn("question", kinds)
        navigate = json.loads([row for row in rows if row["type"] == "ui.navigate"][0]["payload"])
        self.assertEqual(navigate["page"], "settings")


class PolicyTest(unittest.TestCase):
    def test_mode_matrix(self):
        self.assertEqual(policy.decide("request", "read").decision, "auto")
        self.assertEqual(policy.decide("request", "write").decision, "confirm")
        self.assertEqual(policy.decide("request", "destructive").decision, "confirm")
        self.assertEqual(policy.decide("assist", "write").decision, "auto")
        self.assertEqual(policy.decide("assist", "destructive").decision, "confirm")
        self.assertEqual(policy.decide("full", "write").decision, "auto")
        self.assertEqual(policy.decide("full", "destructive").decision, "auto")
        # 未知模式/未知风险都退化成最保守组合。
        self.assertEqual(policy.decide("nonsense", "weird").decision, "confirm")

    def test_tool_catalogue_has_no_privileged_tools(self):
        from backend.agent import tools

        names = set(tools.TOOLS)
        for forbidden in ("hx_grant_root", "hx_bind_root", "hx_set_mode", "hx_run_shell"):
            self.assertNotIn(forbidden, names)
        # 每个工具都必须声明风险与 schema。
        for spec in tools.TOOLS.values():
            self.assertIn(spec.risk, {"read", "write", "destructive"})
            self.assertIn("type", spec.schema)

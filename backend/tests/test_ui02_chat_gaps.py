"""UI-02 真实对话核验暴露的缺口：附件引用、真流式增量、文本状态单调。

这些用例只断言**真实落库的消息与事件**：
* 用户消息的 ``meta.asset_ids`` 在补 ``turn_id`` 之后仍然存在（曾经被整体覆盖）；
* 引擎事件流的增量只认 assistant 文本，摘要/用户消息/非文本 part 一律不进正文；
* 同一 part 的短快照不会把已经显示的正文改短。
"""

from __future__ import annotations

import io
import json
import threading
import unittest
import uuid

from backend.agent import engine as engine_module
from backend.agent import identity, runner as runner_module, store
from backend.tests.support import Harness, api, body, csrf_headers, login, wait_turn
from backend.tests.test_business_wb17_03 import png_bytes


class ChatGapBase(unittest.TestCase):
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

    def tearDown(self):
        self.harness.close()

    # ------------------------------------------------------------------ 帮助

    def _mark_vision(self, model: str = "probe-model") -> None:
        conn = self.harness.conn()
        try:
            row = identity.connection_for(conn, self.alice["id"])
            conn.execute(
                "UPDATE connections SET capabilities = ?, checked_at = ?, checked_base_url = ?,"
                " checked_model = ?, checked_provider = ?, checked_fingerprint = ?"
                " WHERE user_id = ?",
                (
                    json.dumps({"auth": True, "chat": True, "vision": True, "tools": True}),
                    "2026-11-06T00:00:00Z",
                    str(row["base_url"]),
                    model,
                    str(row["provider"]),
                    str(row["key_fingerprint"]),
                    int(self.alice["id"]),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _upload(self, data: bytes, filename: str) -> dict:
        response = self.client.post(
            f"/api/projects/{self.project['id']}/assets/upload",
            data={"file": (io.BytesIO(data), filename)},
            headers=csrf_headers(self.client),
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return body(response)["data"]["asset"]

    def _messages(self) -> list[dict]:
        response = api(self.client, "GET", f"/api/sessions/{self.session['id']}/messages")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return body(response)["data"]["messages"]


class AttachmentReferenceTest(ChatGapBase):
    """图片必须留在历史里：meta 被补 turn_id 时不能把 asset_ids 覆盖掉。"""

    def test_user_message_keeps_asset_reference_after_turn(self):
        self._mark_vision()
        asset = self._upload(png_bytes(), "热斑.png")
        self.fake.queue({"text": "我看到一张图。"})
        response = api(
            self.client,
            "POST",
            f"/api/sessions/{self.session['id']}/turns",
            {
                "content": "看看这张图",
                "request_id": uuid.uuid4().hex,
                "asset_ids": [asset["id"]],
            },
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        turn = body(response)["data"]["turn"]
        final = wait_turn(self.client, turn["id"])
        self.assertEqual(final["turn"]["status"], "completed")

        user = [item for item in self._messages() if item["role"] == "user"][0]
        self.assertEqual(user["meta"].get("asset_ids"), [asset["id"]], user["meta"])
        self.assertEqual(user["meta"].get("turn_id"), turn["id"])
        self.assertEqual(user["meta"].get("source"), "web")

    def test_asset_content_is_scoped_to_owner_and_project(self):
        asset = self._upload(png_bytes(), "热斑.png")
        # 当前账号当前项目：能取到内容（页面缩略图走的就是这个地址）。
        mine = self.client.get(f"/api/projects/{self.project['id']}/assets/{asset['id']}/content")
        self.assertEqual(mine.status_code, 200)
        mine.close()

        self.harness.create_user("bob")
        other_client = self.harness.client()
        login(other_client, "bob")
        # 另一个账号拿同一个地址必须是 404（不泄露“这个资产存在”）。
        stolen = other_client.get(f"/api/projects/{self.project['id']}/assets/{asset['id']}/content")
        self.assertEqual(stolen.status_code, 404)
        stolen.close()



class _ClosingResponse:
    """close() 之后读取会抛 AttributeError：复现真实 urllib3 释放 fp 的竞态。"""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _ClosingManager:
    """迭代中途“被另一个线程关掉连接”，随后读取端抛 AttributeError。"""

    def __init__(self, on_close) -> None:
        self.on_close = on_close
        self.response = _ClosingResponse()

    def open_events(self, instance):  # noqa: ARG002
        return self.response

    def iter_events(self, response):  # noqa: ARG002
        def generate():
            yield {
                "type": "message.updated",
                "properties": {
                    "sessionID": "ses_live",
                    "info": {"id": "m1", "role": "assistant"},
                },
            }
            self.on_close(self.response)
            raise AttributeError("'NoneType' object has no attribute 'read'")

        return generate()


class LiveCancelRaceTest(ChatGapBase):
    def setUp(self):
        super().setUp()
        conn = self.harness.conn()
        try:
            connection = identity.connection_for(conn, self.alice["id"])
            self.turn, _created = store.create(
                conn,
                session_id=self.session["id"],
                project_id=self.project["id"],
                owner_id=int(self.alice["id"]),
                request_id=uuid.uuid4().hex,
                mode="full",
                provider=str(connection["provider"]),
                model=str(connection["model"]),
                fingerprint=identity.connection_fingerprint(connection),
            )
            self.turn_row = store.get(conn, self.turn["id"])
        finally:
            conn.close()

    def test_close_during_read_ends_quietly_only_when_cancelled(self):
        """停止时另一线程 close() 导致的 AttributeError 不能变成未捕获线程异常。"""
        stop = threading.Event()

        def on_close(response):
            response.close()
            stop.set()

        manager = _ClosingManager(on_close)
        runner = runner_module.TurnRunner(self.harness.settings, manager, None)
        # 不抛异常：取消路径按正常结束处理。
        runner._live_loop(
            self.turn_row, object(), "ses_live", runner_module._LiveText(), stop, {}
        )
        self.assertTrue(stop.is_set())

    def test_unexpected_attribute_error_still_raises(self):
        """没有取消、连接也没关时，AttributeError 是编程错误，必须保留诊断。"""
        stop = threading.Event()
        manager = _ClosingManager(lambda response: None)
        runner = runner_module.TurnRunner(self.harness.settings, manager, None)
        with self.assertRaises(AttributeError):
            runner._live_loop(
                self.turn_row, object(), "ses_live", runner_module._LiveText(), stop, {}
            )
        self.assertFalse(stop.is_set())

# --------------------------------------------------------------------------- 实时增量


class _FakeResponse:
    """假的 SSE 响应：按 (line, delay) 序列吐行，便于稳定复现节流与顺序。"""

    def __init__(self, frames: list[tuple[str, float]]) -> None:
        self._frames = frames
        self.closed = False

    def iter_lines(self, decode_unicode: bool = False):  # noqa: ARG002 - 对齐 requests
        import time

        for line, delay in self._frames:
            if delay:
                time.sleep(delay)
            yield line.encode("utf-8")

    def close(self) -> None:
        self.closed = True


class _StubManager:
    def __init__(self, frames: list[tuple[str, float]]) -> None:
        self.frames = frames
        self.opened = 0
        self.response: _FakeResponse | None = None

    def open_events(self, instance):  # noqa: ARG002
        self.opened += 1
        self.response = _FakeResponse(self.frames)
        return self.response

    def iter_events(self, response):
        return engine_module.EngineManager.iter_events(response)


def _frame(payload: dict, delay: float = 0.0) -> tuple[str, float]:
    return "data: " + json.dumps(payload, ensure_ascii=False), delay


def _delta(message_id: str, part_id: str, chunk: str, delay: float = 0.0):
    return _frame(
        {
            "type": "message.part.delta",
            "properties": {
                "sessionID": "ses_live",
                "messageID": message_id,
                "partID": part_id,
                "field": "text",
                "delta": chunk,
            },
        },
        delay,
    )


def _part(
    message_id: str, part_id: str, kind: str, text: str = "", delay: float = 0.0, **flags
):
    return _frame(
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_live",
                "part": {
                    "type": kind,
                    "id": part_id,
                    "messageID": message_id,
                    "text": text,
                    **flags,
                },
            },
        },
        delay,
    )


def _assistant(message_id: str, *, summary: bool = False):
    info = {"id": message_id, "role": "assistant"}
    if summary:
        info["summary"] = True
    return _frame({"type": "message.updated", "properties": {"sessionID": "ses_live", "info": info}})


class LiveStreamTest(ChatGapBase):
    def setUp(self):
        super().setUp()
        conn = self.harness.conn()
        try:
            connection = identity.connection_for(conn, self.alice["id"])
            self.turn, _created = store.create(
                conn,
                session_id=self.session["id"],
                project_id=self.project["id"],
                owner_id=int(self.alice["id"]),
                request_id=uuid.uuid4().hex,
                mode="full",
                provider=str(connection["provider"]),
                model=str(connection["model"]),
                fingerprint=identity.connection_fingerprint(connection),
            )
            self.turn_row = store.get(conn, self.turn["id"])
        finally:
            conn.close()

    def _run_live(
        self, frames: list[tuple[str, float]], baseline: set[str] | None = None
    ) -> list[dict]:
        # 与生产同一条入口：`_live_loop(turn, instance, engine_session, live, stop, holder, baseline)`。
        manager = _StubManager(frames)
        runner = runner_module.TurnRunner(self.harness.settings, manager, None)
        live = runner_module._LiveText()
        stop = threading.Event()
        holder: dict = {}
        runner._live_loop(self.turn_row, object(), "ses_live", live, stop, holder, baseline)
        self.assertTrue(manager.opened)
        self.assertTrue(manager.response.closed, "订阅结束时必须关掉连接")
        self.assertNotIn("response", holder)
        conn = self.harness.conn()
        try:
            return store.events_after(conn, self.turn["id"], 0)
        finally:
            conn.close()

    def test_incremental_text_reaches_turn_events_before_completion(self):
        frames = [
            # 用户消息：角色不是 assistant，绝不能上屏
            _frame(
                {
                    "type": "message.updated",
                    "properties": {"sessionID": "ses_live", "info": {"id": "msg_user", "role": "user"}},
                }
            ),
            _delta("msg_user", "prt_user", "请列出目录", 0.02),
            # assistant 角色确认后才展示
            _frame(
                {
                    "type": "message.updated",
                    "properties": {
                        "sessionID": "ses_live",
                        "info": {"id": "msg_a", "role": "assistant"},
                    },
                }
            ),
            # 真实 1.19.4 的顺序：先建 part（PartUpdated 带 type=text），再来增量。
            _part("msg_a", "prt_1", "text", ""),
            _delta("msg_a", "prt_1", "第一段。", 0.02),
            _delta("msg_a", "prt_1", "第二段。", 0.2),
            _delta("msg_a", "prt_1", "第三段。", 0.2),
            # 非文本字段与非 text part 一律不进正文
            _frame(
                {
                    "type": "message.part.delta",
                    "properties": {
                        "sessionID": "ses_live",
                        "messageID": "msg_a",
                        "partID": "prt_1",
                        "field": "reasoning",
                        "delta": "内部推理不应展示",
                    },
                    },
                0.02,
            ),
            _frame(
                {
                    "type": "message.part.updated",
                    "properties": {
                        "sessionID": "ses_live",
                        "part": {"type": "reasoning", "id": "prt_r", "messageID": "msg_a", "text": "思维链"},
                    },
                },
                0.02,
            ),
            # 摘要消息：既不是回答也不能展示
            _frame(
                {
                    "type": "message.updated",
                    "properties": {
                        "sessionID": "ses_live",
                        "info": {"id": "msg_s", "role": "assistant", "summary": True},
                    },
                }
            ),
            _delta("msg_s", "prt_s", "压缩摘要内容", 0.02),
        ]
        events = self._run_live(frames)
        texts = [event["payload"]["text"] for event in events if event["type"] == "text"]
        self.assertGreaterEqual(len(texts), 2, f"上游结束前必须已有多次可见更新：{texts}")
        self.assertTrue(all("请列出目录" not in item for item in texts))
        self.assertTrue(all("压缩摘要" not in item for item in texts))
        self.assertTrue(all("推理" not in item for item in texts))
        self.assertTrue(all("思维链" not in item for item in texts))
        self.assertEqual(texts[-1], "第一段。第二段。第三段。")
        # 同一 part 的快照必须是单调增长的（否则界面会“变短”）
        lengths = [len(item) for item in texts]
        self.assertEqual(lengths, sorted(lengths))
        self.assertTrue(all(event["payload"]["part_id"] == "prt_1" for event in events if event["type"] == "text"))

    def test_reasoning_delta_with_text_field_is_never_shown(self):
        """1.19.4 的 reasoning part 也发 ``field=text`` 的增量：不能当成正文。"""
        frames = [
            _assistant("msg_a"),
            # 已知类型：reasoning part 的增量即便 field=text 也必须丢弃
            _part("msg_a", "prt_r", "reasoning", ""),
            _delta("msg_a", "prt_r", "UI02_INTERNAL_REASONING_MUST_NOT_APPEAR", 0.02),
            # 类型未知时先来的增量：缓存，随后被标成 reasoning，仍然不能补发
            _delta("msg_a", "prt_u", "未知类型也不能猜", 0.02),
            _part("msg_a", "prt_u", "reasoning", ""),
            # 真正的正文
            _part("msg_a", "prt_t", "text", "真正的回答。", 0.02),
            _delta("msg_a", "prt_t", "真正的回答。", 0.02),
        ]
        events = self._run_live(frames)
        texts = [event["payload"]["text"] for event in events if event["type"] == "text"]
        self.assertEqual(texts, ["真正的回答。"], texts)
        joined = "".join(texts)
        self.assertNotIn("UI02_INTERNAL_REASONING_MUST_NOT_APPEAR", joined)
        self.assertNotIn("未知类型也不能猜", joined)


    def test_other_session_events_are_not_shown(self):
        """同引擎的其它会话/子会话事件不能混进本轮（sessionID 三处都要校验）。"""
        frames = [
            _assistant("m1"),
            _part("m1", "t1", "text", ""),
            _delta("m1", "t1", "本轮回答。", 0.02),
            _frame(
                {
                    "type": "message.updated",
                    "properties": {
                        "info": {"id": "foreign", "sessionID": "ses_foreign", "role": "assistant"}
                    },
                }
            ),
            _frame(
                {
                    "type": "message.part.updated",
                    "properties": {
                        "part": {
                            "id": "f1",
                            "messageID": "foreign",
                            "sessionID": "ses_foreign",
                            "type": "text",
                            "text": "",
                        }
                    },
                }
            ),
            _frame(
                {
                    "type": "message.part.delta",
                    "properties": {
                        "sessionID": "ses_foreign",
                        "messageID": "foreign",
                        "partID": "f1",
                        "field": "text",
                        "delta": "FOREIGN_SESSION_MARKER",
                    },
                },
                0.02,
            ),
        ]
        events = self._run_live(frames)
        texts = [event["payload"]["text"] for event in events if event["type"] == "text"]
        joined = "".join(texts)
        self.assertNotIn("FOREIGN_SESSION_MARKER", joined)
        self.assertIn("本轮回答。", joined)

    def test_baseline_messages_from_previous_turn_are_excluded(self):
        """基线里已有的 messageID 属于更早的轮次：它的增量一条都不能上屏。"""
        frames = [
            _assistant("msg_old"),
            _part("msg_old", "prt_old", "text", ""),
            _delta("msg_old", "prt_old", "上一轮的回答", 0.02),
            _assistant("msg_new"),
            _part("msg_new", "prt_new", "text", ""),
            _delta("msg_new", "prt_new", "这一轮的回答", 0.02),
        ]
        events = self._run_live(frames, baseline={"msg_old"})
        texts = [event["payload"]["text"] for event in events if event["type"] == "text"]
        self.assertEqual(texts, ["这一轮的回答"], texts)

    def test_summary_flag_is_sticky_after_it_appears(self):
        """消息被判成 summary 后，后面再来增量也不能把它的内容“复活”。"""
        frames = [
            _assistant("m1"),
            _part("m1", "t1", "text", ""),
            _delta("m1", "t1", "公开内容。", 0.02),
            _assistant("m1", summary=True),
            _delta("m1", "t1", "摘要后不该出现的续写", 0.2),
        ]
        events = self._run_live(frames)
        texts = [event["payload"]["text"] for event in events if event["type"] == "text"]
        self.assertEqual(texts, ["公开内容。"], texts)

    def test_synthetic_and_ignored_parts_stay_blocked(self):
        frames = [
            _assistant("m1"),
            _part("m1", "s1", "text", "", synthetic=True),
            _delta("m1", "s1", "SYNTHETIC_MARKER", 0.02),
            _part("m1", "i1", "text", "", ignored=True),
            _delta("m1", "i1", "IGNORED_MARKER", 0.02),
            # 之后再刷新一次元数据（不再带 flag）也不能放行
            _part("m1", "s1", "text", "SYNTHETIC_MARKER"),
        ]
        events = self._run_live(frames)
        joined = "".join(event["payload"]["text"] for event in events if event["type"] == "text")
        self.assertNotIn("SYNTHETIC_MARKER", joined)
        self.assertNotIn("IGNORED_MARKER", joined)

    def test_subscription_handshake_completes_before_prompt(self):
        """订阅必须能在下发 prompt 之前报告就绪，否则会漏掉开头几帧。"""

        class _SlowOpen(_StubManager):
            pass

        manager = _SlowOpen([_frame({"type": "noop", "properties": {}})])
        runner = runner_module.TurnRunner(self.harness.settings, manager, None)
        stream = runner._start_live(self.turn_row, object(), "ses_live", runner_module._LiveText(), set())
        try:
            self.assertTrue(stream.ready(1.0), "握手信号必须在校验点之前到达")
        finally:
            stream.close()
        self.assertTrue(manager.response is not None and manager.response.closed)

    def test_shorter_snapshot_never_rewinds_displayed_text(self):
        live = runner_module._LiveText()
        self.assertTrue(live.update("prt_1", "第一段。第二段。"))
        self.assertFalse(live.update("prt_1", "第一段。"))
        self.assertFalse(live.update("prt_1", "第一段。第二段。"))
        self.assertTrue(live.update("prt_1", "第一段。第二段。第三段。"))

    def test_unsubscribable_engine_only_loses_realtime_not_the_turn(self):
        class _Broken:
            def open_events(self, instance):  # noqa: ARG002
                raise engine_module.EngineUnavailable("ENGINE_HTTP_ERROR", "404")

            def iter_events(self, response):  # noqa: ARG002
                raise AssertionError("订阅失败后不应再读事件")

        runner = runner_module.TurnRunner(self.harness.settings, _Broken(), None)
        live = runner_module._LiveText()
        # 不抛异常：事件流只是加速手段，读不到不能影响这一轮本身。
        runner._live_loop(self.turn_row, object(), "ses_live", live, threading.Event(), {})
        conn = self.harness.conn()
        try:
            self.assertEqual(store.events_after(conn, self.turn["id"], 0), [])
        finally:
            conn.close()

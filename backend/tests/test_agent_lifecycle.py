"""引擎/令牌/停止的生命周期。

这三块是「看起来能用、过一会儿就不对」的地方，所以用例都尽量按真实时序压：

* **Native 会话映射**：视觉能力结论变化、idle 回收、进程崩溃、服务重启之后，
  ``engine_sessions`` 里的 ID 还能不能在新引擎里用？存储真的丢了要重建，
  但重建必须把站点已公开的历史带过去，也不能把不确定（超时）当成“会话不存在”；
* **工具令牌**：缓存里的引擎可能握着一张过期令牌。过期要能带新凭据重建并保留会话，
  被明确撤销则不许复活；
* **停止**：请求停止之后不能再有新副作用，已经跑起来的任务要如实报告状态，
  abort 没确认空闲就不能把旧的引擎/令牌交给下一轮。

引擎全部是 ``FakeEngine``（按脚本吐消息，不连任何模型）；会话存储的“搬移/丢失”
由 ``EngineFleet`` 控制，模拟的是磁盘上那份私有目录。
"""

from __future__ import annotations

import base64
import dataclasses
import io
import json
import time
import unittest
import uuid
import zipfile
from unittest.mock import patch

from backend import db
from backend.agent import identity, store
from backend.agent.engine import EngineHandle, EngineManager, EngineSpec, EngineUnavailable
from backend.agent.runner import TurnRunner
from backend.tests.fake_engine import FakeEngine
from backend.tests.support import Harness, api, body, csrf_headers, login, wait_turn
from backend.tests import test_agent_gateway as gateway_fixture


def png_bytes(width: int = 32, height: int = 24, color=(90, 60, 30)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


class EngineFleet:
    """按 spawn 生成独立引擎；同一份存储目录之间会“继承”会话，模拟磁盘持久化。

    脚本通过 :meth:`script` 投放，由**下一个真正收到 prompt 的引擎**消费——
    否则“上一轮结束后引擎被隔离、下一轮换了新进程”时脚本会留在旧引擎里。
    """

    def __init__(self) -> None:
        self.by_dir: dict[str, FakeEngine] = {}
        self.engines: list[FakeEngine] = []
        self.ports: list[int] = []
        self._pending: list[dict] = []

    def script(self, *items: dict) -> None:
        self._pending.extend(items)

    def spawn(self, spec, work_dir):
        work_dir.mkdir(parents=True, exist_ok=True)
        engine = FakeEngine()
        engine.start()
        previous = self.by_dir.get(str(work_dir))
        if previous is not None:
            engine.restore(previous.snapshot())
        original_prompt = engine.prompt

        def prompt(session_id, body):
            while self._pending:
                engine.queue(self._pending.pop(0))
            return original_prompt(session_id, body)

        engine.prompt = prompt  # type: ignore[method-assign]
        self.by_dir[str(work_dir)] = engine
        self.engines.append(engine)
        self.ports.append(engine.port)
        return EngineHandle(
            port=engine.port,
            password=engine.password,
            process=None,
            work_dir=work_dir,
            version="1.19.4",
        )

    @property
    def latest(self) -> FakeEngine:
        return self.engines[-1]

    def wipe(self) -> None:
        """模拟存储目录被清空/丢失：之后同一目录里不再有历史会话。"""
        self.by_dir.clear()

    def shutdown(self) -> None:
        for engine in self.engines:
            engine.stop()


class LifecycleTestBase(unittest.TestCase):
    """真实网关 + 真实运行器 + 可编排的假引擎。"""

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
        self.runtime = self.harness.app.config["HX17_AGENT"]

        self.fleet = EngineFleet()
        self.manager = EngineManager(
            self.harness.settings, spawner=self.fleet.spawn, tokens=self.runtime.gateway
        )
        self.runtime.manager = self.manager
        self.runtime.runner._manager = self.manager
        self.runner = self.runtime.runner

        self.client = self.harness.client()
        login(self.client, "alice")

    def tearDown(self):
        try:
            self.manager.shutdown()
        finally:
            self.fleet.shutdown()
            self.harness.close()

    # ------------------------------------------------------------------ 帮助

    def spec(self, **overrides) -> EngineSpec:
        conn = self.harness.conn()
        try:
            connection = identity.connection_for(conn, self.alice["id"])
            base = EngineSpec(
                user_id=self.alice["id"],
                project_id=self.project["id"],
                session_id=self.session["id"],
                root=self.root,
                fingerprint=identity.connection_fingerprint(connection),
                provider=str(connection["provider"]),
                model=str(connection["model"]),
                models=identity.available_models(conn, self.alice["id"]),
                base_url=str(connection["base_url"]),
                api_key="unit-test-key",
                gateway_url=self.runtime.gateway.base_url,
                tool_token="",
                python="python",
                vision=False,
            )
            return dataclasses.replace(base, **overrides)
        finally:
            conn.close()

    def send(self, content: str = "帮我看看目录", *, request_id: str | None = None):
        payload = {"content": content, "request_id": request_id or uuid.uuid4().hex}
        return api(self.client, "POST", f"/api/sessions/{self.session['id']}/turns", payload)

    def run_turn(self, content: str = "帮我看看目录", *, timeout: float = 30.0) -> dict:
        response = self.send(content)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return wait_turn(self.client, body(response)["data"]["turn"]["id"], timeout=timeout)

    def native_mapping(self):
        conn = self.harness.conn()
        try:
            return store.engine_session(conn, self.session["id"])
        finally:
            conn.close()

    def assistant_texts(self) -> list[str]:
        response = api(self.client, "GET", f"/api/sessions/{self.session['id']}/messages")
        return [
            item["content"]
            for item in body(response)["data"]["messages"]
            if item["role"] == "assistant"
        ]

    def last_prompt(self) -> dict:
        return self.fleet.latest.prompts[-1]

    def wait_for_engine(self, timeout: float = 20.0):
        """等运行器真的把引擎拉起来（轮次线程是异步的）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.manager._instances:
                return next(iter(self.manager._instances.values()))
            time.sleep(0.05)
        raise AssertionError("引擎没有在预期时间内启动")


class NativeSessionMappingTest(LifecycleTestBase):
    def test_vision_change_keeps_native_history(self):
        self.fleet.script({"text": "第一轮回答：目录里有一个 readme.txt。"})
        self.assertEqual(self.run_turn("第一轮")["turn"]["status"], "completed")
        first = self.native_mapping()
        self.assertIsNotNone(first)

        # 能力检查结论翻成“支持图像”：引擎要换进程（模型配置变了），但存储不该换。
        with patch.object(identity, "verified_vision", return_value=True):
            self.fleet.script({"text": "第二轮回答：还是那个 readme.txt。"})
            final = self.run_turn("第二轮")
        self.assertEqual(final["turn"]["status"], "completed")

        after = self.native_mapping()
        self.assertEqual(
            str(after["engine_session_id"]),
            str(first["engine_session_id"]),
            "同一份存储里 Native 会话还在，不该重建",
        )
        self.assertEqual(str(after["storage_key"]), str(first["storage_key"]))
        self.assertNotIn("【会话恢复】", self.last_prompt()["text"])
        # 第二个引擎确实看到了第一轮的对话
        self.assertGreaterEqual(len(self.fleet.latest.sessions[str(first["engine_session_id"])]["messages"]), 4)

    def test_storage_lost_recreates_and_restores_public_history(self):
        self.fleet.script({"text": "第一轮回答：目录里有一个 readme.txt。"})
        self.assertEqual(self.run_turn("第一轮")["turn"]["status"], "completed")
        first = self.native_mapping()

        # 存储目录被回收：进程停掉、目录删除，下一轮只能从空会话开始。
        self.manager.stop_session(self.session["id"])
        self.fleet.wipe()

        self.fleet.script({"text": "第二轮回答：接着上一轮说。"})
        final = self.run_turn("第二轮")
        self.assertEqual(final["turn"]["status"], "completed")

        after = self.native_mapping()
        self.assertEqual(len(self.fleet.engines), 2, "存储丢了必须在空存储上重建引擎")
        text = self.last_prompt()["text"]
        self.assertIn("【会话恢复】", text)
        self.assertIn("第一轮", text, "重建后要把用户的问题带过去")
        self.assertIn("第一轮回答：目录里有一个 readme.txt。", text, "已公开的回答要带过去")
        self.assertNotIn("内部压缩摘要", text, "内部压缩内容不得进入恢复上下文")
        fresh = self.fleet.latest.sessions[str(after["engine_session_id"])]["messages"]
        self.assertEqual(len(fresh), 2, "新会话只应有本轮的两条消息，历史不靠引擎内存")

    def test_existence_probe_network_error_does_not_recreate(self):
        self.fleet.script({"text": "第一轮回答。"})
        self.assertEqual(self.run_turn("第一轮")["turn"]["status"], "completed")
        first = self.native_mapping()
        engine = self.fleet.latest
        before_sessions = len(engine.sessions)
        before_prompts = len(engine.prompts)

        def broken(*_args, **_kwargs):
            raise EngineUnavailable("ENGINE_UNREACHABLE", "模拟探测超时")

        with patch.object(self.manager, "session_exists", broken):
            final = self.run_turn("第二轮")

        self.assertEqual(final["turn"]["status"], "failed")
        self.assertEqual(final["turn"]["error_code"], "ENGINE_UNREACHABLE")
        self.assertEqual(
            str(self.native_mapping()["engine_session_id"]),
            str(first["engine_session_id"]),
            "不确定是否存在时不能改映射",
        )
        self.assertEqual(len(engine.sessions), before_sessions, "不能新建会话")
        self.assertEqual(len(engine.prompts), before_prompts, "更不能把请求重发一遍")

    def test_idle_sweep_keeps_storage_and_reuses_session(self):
        self.fleet.script({"text": "第一轮回答。"})
        self.assertEqual(self.run_turn("第一轮")["turn"]["status"], "completed")
        first = self.native_mapping()

        self.assertEqual(self.manager.sweep_idle(max_idle_seconds=0), 1)
        self.assertEqual(self.manager.active_count(), 0)

        self.fleet.script({"text": "第二轮回答。"})
        self.assertEqual(self.run_turn("第二轮")["turn"]["status"], "completed")
        self.assertEqual(
            str(self.native_mapping()["engine_session_id"]), str(first["engine_session_id"])
        )
        self.assertNotIn("【会话恢复】", self.last_prompt()["text"])

    def test_process_crash_reuses_native_session(self):
        self.fleet.script({"text": "第一轮回答。"})
        self.assertEqual(self.run_turn("第一轮")["turn"]["status"], "completed")
        first = self.native_mapping()

        # 进程崩溃：端口不再应答健康检查，但磁盘上的会话存储还在。
        self.fleet.latest.stop()

        self.fleet.script({"text": "第二轮回答。"})
        self.assertEqual(self.run_turn("第二轮")["turn"]["status"], "completed")
        self.assertEqual(
            str(self.native_mapping()["engine_session_id"]), str(first["engine_session_id"])
        )
        self.assertNotIn("【会话恢复】", self.last_prompt()["text"])

    def test_service_restart_reuses_native_session(self):
        self.fleet.script({"text": "第一轮回答。"})
        self.assertEqual(self.run_turn("第一轮")["turn"]["status"], "completed")
        first = self.native_mapping()

        # 服务重启：进程池与服务端内存全丢，只剩数据库里的映射和磁盘上的存储。
        restarted = EngineManager(
            self.harness.settings, spawner=self.fleet.spawn, tokens=self.runtime.gateway
        )
        try:
            runner = TurnRunner(self.harness.settings, restarted, self.runtime.gateway)
            instance = restarted.get(self.spec())
            conn = self.harness.conn()
            try:
                engine_session, recreated = runner._prepare_engine_session(
                    conn, instance, self.session, self.spec().fingerprint
                )
            finally:
                conn.close()
            self.assertFalse(recreated, "存储还在，就不该重建会话")
            self.assertEqual(engine_session, str(first["engine_session_id"]))
        finally:
            restarted.shutdown()


class BaselineBoundaryTest(LifecycleTestBase):
    """发起前的“已有消息 ID 集合”读失败时，不能把旧回答当成本轮结果。

    这是已被独立复现的真实反例：基线读失败 → 旧实现返回空集合 → 旧消息全部
    变成“本轮新增” → 新请求短暂 idle 时旧终答满足完成条件 → 本轮假成功，
    旧回答又落一遍、真正的新答丢失。
    """

    def _old_answer_done(self) -> None:
        self.fleet.script({"text": "OLD-ANSWER"})
        self.assertEqual(self.run_turn("第一轮")["turn"]["status"], "completed")

    def test_transient_baseline_failure_recovers_and_new_answer_wins(self):
        self._old_answer_done()
        # 第二轮：新请求先短暂空闲 3 秒（旧答在这段时间里是唯一的“已完成回答”）
        self.fleet.script({"text": "NEW-ANSWER", "idle_blip": True, "blip_seconds": 3.0})

        original = self.manager.messages
        calls = [0]

        def fail_once(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                raise EngineUnavailable("ENGINE_TIMEOUT", "模拟一次瞬时读取失败")
            return original(*args, **kwargs)

        with patch.object(self.manager, "messages", fail_once):
            final = self.run_turn("第二轮")

        self.assertEqual(final["turn"]["status"], "completed")
        self.assertEqual(
            self.assistant_texts(),
            ["OLD-ANSWER", "NEW-ANSWER"],
            "旧回答不能被当作本轮结果再落一遍",
        )
        self.assertGreaterEqual(calls[0], 2, "基线失败后应做有界只读重试，而不是按空历史继续")

    def test_unreadable_baseline_fails_before_any_prompt(self):
        self._old_answer_done()
        engine = self.fleet.latest
        before_prompts = len(engine.prompts)

        def broken(*_args, **_kwargs):
            raise EngineUnavailable("ENGINE_TIMEOUT", "模拟持续读取失败")

        with patch.object(self.manager, "messages", broken):
            final = self.run_turn("第二轮")

        self.assertEqual(final["turn"]["status"], "failed")
        self.assertEqual(final["turn"]["error_code"], "BASELINE_UNAVAILABLE")
        self.assertEqual(len(engine.prompts), before_prompts, "拿不到确定基线就不能发 prompt")
        self.assertEqual(self.assistant_texts(), ["OLD-ANSWER"], "旧回答不能再落一遍")

    def test_messages_without_ids_are_not_a_usable_baseline(self):
        self._old_answer_done()
        engine = self.fleet.latest
        before_prompts = len(engine.prompts)

        with patch.object(self.manager, "messages", lambda *a, **k: [{"parts": []}]):
            final = self.run_turn("第二轮")

        self.assertEqual(final["turn"]["status"], "failed")
        self.assertEqual(final["turn"]["error_code"], "BASELINE_UNAVAILABLE")
        self.assertEqual(len(engine.prompts), before_prompts)


class EngineFailureScopeTest(LifecycleTestBase):
    """失效范围必须等于**真实**范围。

    反例：``_poll`` 里读会话状态失败，一个会话读不出来就把整个项目的引擎都停掉
    （``stop_project``）。表现是“另一个会话明明没出错，正在跑的一轮突然没了”。
    项目级失效（删项目、解除目录绑定、收回目录授权）才允许作用于整个项目。
    """

    def _other_session(self) -> dict:
        return self.harness.create_session(self.project["id"], self.alice["id"], "第二个会话")

    def _instance_for(self, session_id: str):
        for item in list(self.manager._instances.values()):
            if item.spec.session_id == str(session_id):
                return item
        return None

    def test_one_session_engine_failure_does_not_break_other_sessions(self):
        original = self.session
        self.fleet.script({"text": "会话一：目录里有一个 readme.txt。"})
        self.assertEqual(self.run_turn("第一轮")["turn"]["status"], "completed")
        first = self._instance_for(original["id"])
        self.assertIsNotNone(first, "第一轮之后应该有一台会话一的引擎")
        first_port = first.handle.port
        engines_before = len(self.fleet.engines)

        other = self._other_session()
        real_messages = self.manager.messages
        reads = {"n": 0}

        def messages(instance, engine_session, limit=500):
            if instance.spec.session_id == str(other["id"]):
                reads["n"] += 1
                # 第 1 次是发起前的**基线读取**：那次失败本轮根本不会发 prompt，
                # 测的就不是这里要测的东西。这里模拟的是“引擎起来了之后读不出状态”。
                if reads["n"] > 1:
                    raise EngineUnavailable("ENGINE_UNAVAILABLE", "这个会话的引擎读不出消息")
            return real_messages(instance, engine_session, limit=limit)

        self.session = other
        with patch.object(self.manager, "messages", side_effect=messages), patch(
            "backend.agent.runner.MAX_ENGINE_ERRORS", 2
        ):
            failed = self.run_turn("这个会话的引擎会挂", timeout=45.0)

        self.assertEqual(failed["turn"]["status"], "failed")
        self.assertEqual(failed["turn"]["error_code"], "ENGINE_LOST")
        self.assertIsNone(self._instance_for(other["id"]), "故障会话的引擎应该被隔离")

        survivor = self._instance_for(original["id"])
        self.assertIsNotNone(survivor, "同项目另一个会话的引擎被一起杀掉了")
        self.assertEqual(survivor.handle.port, first_port, "另一个会话的引擎不该被重建")
        self.assertTrue(survivor.alive(), "另一个会话的引擎进程不该被停止")

        # 另一个会话必须还能用**同一台**引擎正常完成一轮
        self.session = original
        self.fleet.script({"text": "会话一：第二轮仍然正常。"})
        again = self.run_turn("第二轮")
        self.assertEqual(again["turn"]["status"], "completed")
        self.assertEqual(
            len(self.fleet.engines), engines_before + 1, "只应多出故障会话那一台引擎"
        )
        self.assertEqual(self._instance_for(original["id"]).handle.port, first_port)
        self.assertEqual(
            self.assistant_texts(),
            ["会话一：目录里有一个 readme.txt。", "会话一：第二轮仍然正常。"],
        )

    def test_project_grant_revocation_stops_every_engine_in_project(self):
        original = self.session
        self.fleet.script({"text": "会话一：正常回答。"})
        self.assertEqual(self.run_turn("第一轮")["turn"]["status"], "completed")
        self.assertIsNotNone(self._instance_for(original["id"]))

        other = self._other_session()
        self.session = other
        self.fleet.script({"text": "这条回答不该出现。", "initial_delay": 3.0})
        response = self.send("目录授权马上会被收回")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        turn_id = body(response)["data"]["turn"]["id"]

        deadline = time.time() + 10.0
        while time.time() < deadline and self._instance_for(other["id"]) is None:
            time.sleep(0.05)
        self.assertIsNotNone(self._instance_for(other["id"]))
        # 项目级失效：目录授权被收回
        self.assertTrue(self.harness.revoke_root(self.root_row["id"], self.alice["id"]))

        result = wait_turn(self.client, turn_id, timeout=60.0)
        self.assertEqual(result["turn"]["status"], "failed")
        self.assertEqual(result["turn"]["error_code"], "GRANT_REVOKED")
        self.assertIsNone(self._instance_for(other["id"]))
        self.assertIsNone(
            self._instance_for(original["id"]), "项目级失效时同项目其它会话的引擎也应被隔离"
        )


class AttachmentTurnTest(LifecycleTestBase):
    """聊天附件必须真的送到引擎：真实字节、真实图片 part。

    只把文件路径写进文本是“看起来带了图”，模型其实看不到内容；
    没有视觉证据就把图片塞给模型，会把请求发给一个不支持图像的模型。
    两条都在这里用**真实回环引擎请求体**验证，不靠界面文案。
    """

    def _mark_vision(self, model: str, *, vision: bool = True) -> None:
        conn = self.harness.conn()
        try:
            row = identity.connection_for(conn, self.alice["id"])
            conn.execute(
                """
                UPDATE connections SET capabilities = ?, checked_at = ?, checked_base_url = ?,
                       checked_model = ?, checked_provider = ?, checked_fingerprint = ?
                 WHERE user_id = ?
                """,
                (
                    json.dumps({"auth": True, "chat": True, "vision": vision, "tools": False}),
                    "2026-11-05T00:00:00Z",
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

    def _send_with(self, content: str, asset_ids: list[str]):
        return api(
            self.client,
            "POST",
            f"/api/sessions/{self.session['id']}/turns",
            {"content": content, "request_id": uuid.uuid4().hex, "asset_ids": asset_ids},
        )

    def test_image_is_sent_as_real_bytes_not_a_path(self):
        self._mark_vision("probe-model")
        blob = png_bytes()
        asset = self._upload(blob, "热斑.png")
        self.fleet.script({"text": "我看到图里有一块热斑。"})
        response = self._send_with("看看这张图", [asset["id"]])
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        turn_id = body(response)["data"]["turn"]["id"]
        result = wait_turn(self.client, turn_id, timeout=40.0)
        self.assertEqual(result["turn"]["status"], "completed", result)
        self.assertEqual(result["turn"]["attachments"], [asset["id"]])

        parts = self.fleet.latest.prompts[-1]["parts"]
        files = [part for part in parts if part.get("type") == "file"]
        self.assertEqual(len(files), 1, parts)
        url = str(files[0]["url"])
        self.assertTrue(url.startswith("data:image/png;base64,"), url[:40])
        encoded = url.split(",", 1)[1]
        self.assertEqual(base64.b64decode(encoded), blob, "发出去的必须是原图字节")
        self.assertEqual(files[0]["filename"], "热斑.png")
        self.assertTrue(any(part.get("type") == "text" for part in parts))

    def test_image_without_vision_evidence_is_refused_before_prompt(self):
        self._mark_vision("probe-model", vision=False)
        asset = self._upload(png_bytes(), "热斑.png")
        response = self._send_with("看看这张图", [asset["id"]])
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(body(response)["error"], "VISION_UNVERIFIED")
        # 没有视觉证据就一个 prompt 都不能发（否则会真实计费，还可能失败）。
        self.assertFalse(self.fleet.latest.prompts if self.fleet.engines else [])

    def test_non_image_asset_is_refused(self):
        self._mark_vision("probe-model")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("readme.txt", "hello")
        asset = self._upload(buffer.getvalue(), "pack.zip")
        response = self._send_with("把这个压缩包看看", [asset["id"]])
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(body(response)["error"], "NOT_AN_IMAGE")


class TokenLifecycleTest(LifecycleTestBase):
    def test_expired_token_rebuilds_with_new_credentials_and_keeps_storage(self):
        spec = self.spec()
        instance = self.manager.get(spec)
        old_token = instance.spec.tool_token
        self.assertIsNotNone(self.runtime.gateway.binding_for(old_token))

        binding = self.runtime.gateway.binding_for(old_token)
        binding.expires_at = time.time() - 1

        reused = self.manager.get(self.spec())
        self.assertIsNot(self.manager._instances[spec.key()], instance)
        new_token = reused.spec.tool_token
        self.assertNotEqual(new_token, old_token)
        self.assertIsNotNone(self.runtime.gateway.binding_for(new_token))
        self.assertIsNone(self.runtime.gateway.binding_for(old_token))
        self.assertEqual(
            self.manager.storage_key(reused.spec),
            self.manager.storage_key(instance.spec),
            "换令牌不该换存储，否则历史会丢",
        )

    def test_revoked_token_is_not_resurrected(self):
        spec = self.spec()
        instance = self.manager.get(spec)
        self.runtime.gateway.revoke_session(spec.session_id)
        with self.assertRaises(EngineUnavailable) as caught:
            self.manager.get(self.spec())
        self.assertEqual(caught.exception.code, "SESSION_REVOKED")
        self.assertEqual(self.manager.active_count(), 0, "撤销后不能继续留着这台引擎")
        self.assertIsNone(self.runtime.gateway.binding_for(instance.spec.tool_token))

    def test_turn_after_token_expiry_still_works(self):
        self.fleet.script({"text": "第一轮回答。"})
        self.assertEqual(self.run_turn("第一轮")["turn"]["status"], "completed")
        instance = next(iter(self.manager._instances.values()))
        instance.spec.tool_token  # 令牌仍绑在这台引擎上
        binding = self.runtime.gateway.binding_for(instance.spec.tool_token)
        self.assertIsNotNone(binding)
        binding.expires_at = time.time() - 1

        self.fleet.script({"text": "第二轮回答。"})
        final = self.run_turn("第二轮")
        self.assertEqual(final["turn"]["status"], "completed", final["turn"])
        self.assertEqual(self.assistant_texts()[-1], "第二轮回答。")


class StopBlocksSideEffectsTest(unittest.TestCase):
    """停止之后的副作用必须真的没有发生。

    这里复用网关用例的夹具（真实回环网关 + 真实文件系统），但**继承会重复收集**
    父类的全部用例，所以用组合：只跑本类自己的三条。
    """

    def setUp(self):
        self.case = gateway_fixture.GatewayTest()
        self.case.setUp()
        self.harness = self.case.harness
        self.root = self.case.root
        self.session = self.case.session
        self.runtime = self.case.runtime

    def tearDown(self):
        self.case.tearDown()

    def _approve(self, action_id: str):
        client = self.harness.client()
        login(client, "alice")
        return api(client, "POST", f"/api/actions/{action_id}/approve")

    def test_stop_blocks_new_tool_call(self):
        turn = self.case._turn(mode="full")
        token = self.case._token(turn)
        conn = self.harness.conn()
        try:
            self.assertTrue(store.request_stop(conn, turn["id"]))
        finally:
            conn.close()

        status, payload = self.case.call(
            token, "hx_write_file", {"path": "after-stop.txt", "content": "must not write"}
        )
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "STOP_REQUESTED")
        self.assertFalse((self.root / "after-stop.txt").exists())

    def test_stop_blocks_approving_proposed_action(self):
        turn = self.case._turn(mode="request")
        token = self.case._token(turn)
        status, payload = self.case.call(
            token, "hx_write_file", {"path": "confirmed.txt", "content": "must not write"}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["status"], "pending_confirmation")
        action_id = payload["data"]["action_id"]

        conn = self.harness.conn()
        try:
            self.assertTrue(store.request_stop(conn, turn["id"]))
        finally:
            conn.close()

        response = self._approve(action_id)
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(body(response)["error"], "STOP_REQUESTED")
        self.assertFalse((self.root / "confirmed.txt").exists())

    def test_revoked_session_token_stops_working(self):
        turn = self.case._turn(mode="full")
        token = self.case._token(turn)
        self.runtime.gateway.revoke_session(self.session["id"])
        status, payload = self.case.call(
            token, "hx_write_file", {"path": "after-revoke.txt", "content": "nope"}
        )
        self.assertEqual(status, 401, payload)
        self.assertFalse((self.root / "after-revoke.txt").exists())


class StopQuarantinesEngineTest(LifecycleTestBase):
    def test_unconfirmed_abort_isolates_engine_and_reports_truthfully(self):
        self.fleet.script({"hang": True})
        response = self.send("帮我看看目录")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        turn_id = body(response)["data"]["turn"]["id"]

        instance = self.wait_for_engine()
        old_token = instance.spec.tool_token
        old_port = instance.handle.port

        # 引擎永远不承认空闲：abort 之后状态一直是 busy。
        with patch.object(self.manager, "session_state", return_value="busy"), patch(
            "backend.agent.runner.STOP_WAIT_SECONDS", 0.4
        ):
            stop = api(self.client, "POST", f"/api/turns/{turn_id}/stop")
            self.assertEqual(stop.status_code, 200, stop.get_data(as_text=True))
            final = wait_turn(self.client, turn_id, timeout=40)

        self.assertEqual(final["turn"]["status"], "stopped")
        self.assertEqual(final["turn"]["error_code"], "STOPPED_UNCONFIRMED")
        self.assertIn("未确认空闲", final["turn"]["error_message"])
        self.assertIn("不要把未返回结果的动作当作已完成", final["turn"]["error_message"])
        self.assertEqual(self.manager.active_count(), 0, "被隔离的引擎不能留在池子里")
        self.assertIsNone(self.runtime.gateway.binding_for(old_token), "旧令牌必须失效")

        # 下一轮必须是新引擎、新令牌：旧引擎里可能还有没停干净的调用。
        self.fleet.script({"text": "新一轮回答。"})
        self.assertEqual(self.run_turn("新一轮")["turn"]["status"], "completed")
        new_instance = next(iter(self.manager._instances.values()))
        self.assertNotEqual(new_instance.handle.port, old_port)
        self.assertNotEqual(new_instance.spec.tool_token, old_token)

    def test_confirmed_abort_keeps_engine(self):
        self.fleet.script({"hang": True})
        response = self.send("帮我看看目录")
        turn_id = body(response)["data"]["turn"]["id"]
        instance = self.wait_for_engine()

        stop = api(self.client, "POST", f"/api/turns/{turn_id}/stop")
        self.assertEqual(stop.status_code, 200, stop.get_data(as_text=True))
        final = wait_turn(self.client, turn_id, timeout=40)

        self.assertEqual(final["turn"]["status"], "stopped")
        self.assertEqual(final["turn"]["error_code"], "STOPPED")
        self.assertEqual(self.manager.active_count(), 1, "确认空闲就不该隔离引擎")
        self.assertIsNotNone(self.manager._instances.get(instance.spec.key()))
        self.assertIsNotNone(self.runtime.gateway.binding_for(instance.spec.tool_token))

        # 但下一轮不能复用这台停止过的引擎：实测会让新轮次空回答（NO_FINAL_TEXT）。
        # 运行器应在下一轮开始前只替换本会话的实例，存储保留、从站内历史恢复。
        self.fleet.script({"text": "停止后再次回答。"})
        second = self.run_turn("停止后再问一次")
        self.assertEqual(second["turn"]["status"], "completed")
        new_instance = self.manager._instances.get(instance.spec.key())
        self.assertIsNotNone(new_instance)
        self.assertNotEqual(new_instance.spec.tool_token, instance.spec.tool_token)


class VisionEvidenceScopeTest(unittest.TestCase):
    """视觉能力是**针对某个模型**的结论，不能挪给另一个模型用。"""

    def setUp(self):
        self.harness = Harness()
        self.user = self.harness.create_user("alice", role="admin")
        self.harness.save_connection(
            self.user["id"],
            provider="deepseek",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
        )
        self.conn = self.harness.conn()
        self.conn.execute(
            "UPDATE connections SET capabilities = ?, checked_at = ?, checked_base_url = ?, "
            "checked_model = ?, checked_provider = ?, checked_fingerprint = key_fingerprint "
            "WHERE user_id = ?",
            (
                '{"auth": true, "chat": true, "vision": true, "tools": false}',
                "2026-11-04T00:00:00Z",
                "https://api.deepseek.com/v1",
                "deepseek-chat",
                "deepseek",
                self.user["id"],
            ),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.harness.close()

    def test_vision_is_not_inherited_by_another_model(self):
        selection = identity.resolve(self.conn, self.user["id"])
        self.assertEqual(selection.model, "deepseek-chat")
        self.assertTrue(selection.vision)
        self.assertEqual(selection.vision_reason, "verified")

        store.save_prefs(self.conn, self.user["id"], mode="request", model="deepseek-reasoner")
        selection = identity.resolve(self.conn, self.user["id"])
        self.assertEqual(selection.model, "deepseek-reasoner", "实际请求的模型必须是选中的那个")
        self.assertFalse(selection.vision, "别的模型验证过的视觉能力不能挪过来")
        self.assertEqual(selection.vision_reason, "stale")
        self.assertFalse(identity.verified_vision(self.conn, self.user["id"]))

        from backend.services import connections as connection_service

        view = connection_service.view(self.conn, self.user["id"])
        self.assertTrue(view["capabilities_stale"])
        self.assertEqual(view["effective_model"], "deepseek-reasoner")

    def test_switching_back_restores_verified_vision(self):
        store.save_prefs(self.conn, self.user["id"], mode="request", model="deepseek-reasoner")
        self.assertFalse(identity.verified_vision(self.conn, self.user["id"]))
        store.clear_model_pref(self.conn, self.user["id"])
        self.assertEqual(identity.resolve(self.conn, self.user["id"]).model, "deepseek-chat")
        self.assertTrue(identity.verified_vision(self.conn, self.user["id"]))

    def test_engine_config_only_declares_image_for_the_current_model(self):
        import json

        spec = EngineSpec(
            user_id=self.user["id"],
            project_id="p",
            session_id="s",
            root=self.harness.make_dir("cfg"),
            fingerprint="f",
            provider="deepseek",
            model="deepseek-chat",
            models=["deepseek-chat", "deepseek-reasoner"],
            base_url="https://api.deepseek.com/v1",
            api_key="k",
            gateway_url="http://127.0.0.1:1",
            tool_token="t",
            python="python",
            vision=True,
        )
        manager = EngineManager(self.harness.settings)
        try:
            config = json.loads(manager._config_content(spec))
        finally:
            manager.shutdown()
        models = config["provider"]["hx17"]["models"]
        self.assertTrue(models["deepseek-chat"]["attachment"])
        self.assertIn("image", models["deepseek-chat"]["modalities"]["input"])
        self.assertFalse(models["deepseek-reasoner"]["attachment"], "没验证过的模型不能声明图像")
        self.assertEqual(models["deepseek-reasoner"]["modalities"]["input"], ["text"])

    def test_verified_vision_is_scoped_to_the_requested_model(self):
        self.assertTrue(identity.verified_vision(self.conn, self.user["id"], "deepseek-chat"))
        self.assertFalse(
            identity.verified_vision(self.conn, self.user["id"], "deepseek-reasoner"),
            "给别的模型声明视觉输入就是假阳性",
        )


if __name__ == "__main__":
    unittest.main()

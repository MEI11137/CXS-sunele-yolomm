"""INT-06：统一个人模型客户端与真实多模态核查。

对应关键词：
- 复用个人连接，不持有第二份全局 Key，也没有全局回退；
- 零参数 ``client_factory`` 闭包注入旧批量接口；
- 按真实记录同时发送 RGB 与第二模态图，附类别表与检测结果；
- 缺配套图拒绝，不拿 RGB 冒充；
- 能力不足阻断；解析失败不自动重复计费；
- 逐项失败映射为 failed / partial_failed，不被外层 succeeded 掩盖。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from services import ai_review_service
from services.personal_review_client import (
    PersonalReviewClient,
    create_personal_review_client,
)


class FakeGateway:
    """记录真实发出的多模态消息；credentials 模拟本人连接。"""

    def __init__(self, model="vision-personal"):
        self.credentials = SimpleNamespace(
            provider="openai", api_url="https://example.test/v1", model=model,
            api_key="personal-key",
        )
        self.messages = []
        self.text = '{"status":"consistent","summary":"ok"}'

    def complete(self, messages, tools=None, *, temperature=0.2):
        self.messages.append(messages)
        return SimpleNamespace(text=self.text, tool_calls=[])

    def _get_client(self):
        raise AssertionError("健康检查不应在建客户端时就被调用")


def _png(color):
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buffer, "PNG")
    return buffer.getvalue()


RGB_BYTES = _png((10, 20, 30))
X_BYTES = _png((200, 200, 200))


def _png(color):
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buffer, "PNG")
    return buffer.getvalue()


RGB_BYTES = _png((10, 20, 30))
X_BYTES = _png((200, 200, 200))


class FakeStorage:
    def __init__(self, images):
        self.images = images

    def get(self, bucket, key):
        return self.images[f"{bucket}/{key}"]


class FakeDatabase:
    def __init__(self, record):
        self.record = record
        self.updated = []

    def query(self, sql, params=(), fetchone=False):
        return self.record

    def execute(self, sql, params=()):
        self.updated.append((sql, params))
        return 1


def _record(**overrides):
    base = {
        "id": "record-1",
        "original_image_bucket": "orig",
        "original_image_object_key": "rgb.jpg",
        "secondary_image_bucket": None,
        "secondary_image_object_key": None,
        "detection_result": json.dumps([{"class_name": "crack", "bbox": [1, 2, 3, 4]}]),
        "model_mode": "single",
        "secondary_modality": None,
        "structure_info": json.dumps({"names": ["crack", "hotspot"]}),
    }
    base.update(overrides)
    return base


def _images(**extra):
    base = {"orig/rgb.jpg": RGB_BYTES}
    base.update(extra)
    return base


# --------------------------------------------------------------------------- #
# 复用个人连接，没有全局回退
# --------------------------------------------------------------------------- #
def test_client_is_built_from_the_actor_connection(monkeypatch):
    from agent import connections

    actor = SimpleNamespace(user_id=7)
    gateway = FakeGateway()
    seen = []

    monkeypatch.setattr(connections, "model_client", lambda who: seen.append(who) or gateway)
    monkeypatch.setattr(connections, "require_capability", lambda who, cap: seen.append(cap))

    client = create_personal_review_client(actor)
    assert isinstance(client, PersonalReviewClient)
    assert seen == [actor, "vision"]
    assert client.model_name == "vision-personal"


def test_unconfigured_account_raises_instead_of_falling_back(monkeypatch):
    from agent import connections
    from agent.errors import AgentError, Code

    def missing(actor):
        raise AgentError(Code.MODEL_NOT_CONFIGURED, "当前账号尚未配置可用的大模型连接")

    monkeypatch.setattr(connections, "model_client", missing)
    with pytest.raises(AgentError) as caught:
        create_personal_review_client(SimpleNamespace(user_id=8))
    assert caught.value.code == Code.MODEL_NOT_CONFIGURED


def test_missing_vision_capability_blocks_before_any_call(monkeypatch):
    from agent import connections
    from agent.errors import AgentError, Code

    gateway = FakeGateway()
    monkeypatch.setattr(connections, "model_client", lambda actor: gateway)

    def no_vision(actor, capability):
        raise AgentError(Code.MODEL_CAPABILITY_MISSING, "该模型不支持图片输入")

    monkeypatch.setattr(connections, "require_capability", no_vision)
    with pytest.raises(AgentError) as caught:
        create_personal_review_client(SimpleNamespace(user_id=7))
    assert caught.value.code == Code.MODEL_CAPABILITY_MISSING
    assert gateway.messages == [], "能力不足时不能已经发出请求"


# --------------------------------------------------------------------------- #
# 真实多模态：RGB + X 一起发
# --------------------------------------------------------------------------- #
def test_single_modality_record_sends_exactly_one_image():
    gateway = FakeGateway()
    client = PersonalReviewClient(gateway)
    client.review_detection_structured(
        RGB_BYTES, [{"class_name": "crack"}], class_names=["crack"]
    )
    content = gateway.messages[0][0]["content"]
    assert [part["type"] for part in content] == ["text", "image_url"]
    assert "本记录只有 RGB 一张图" in content[0]["text"]


def test_multimodal_record_sends_rgb_and_the_real_second_image():
    gateway = FakeGateway()
    client = PersonalReviewClient(gateway)
    client.review_detection_structured(
        RGB_BYTES, [], class_names=["crack"],
        secondary_image_bytes=X_BYTES, secondary_modality="infrared",
    )
    content = gateway.messages[0][0]["content"]
    images = [part for part in content if part["type"] == "image_url"]
    assert len(images) == 2
    assert images[0]["image_url"]["url"] != images[1]["image_url"]["url"]
    assert "secondary" not in content[0]["text"] and "配图" in content[0]["text"]


def test_review_service_sends_the_stored_second_image():
    gateway = FakeGateway()
    database = FakeDatabase(_record(
        secondary_image_bucket="sec", secondary_image_object_key="x.jpg",
        model_mode="multimodal", secondary_modality="infrared",
    ))
    storage = FakeStorage(_images(**{"sec/x.jpg": X_BYTES}))
    ai_review_service.review_detection(
        "record-1", PersonalReviewClient(gateway), database, storage
    )
    content = gateway.messages[0][0]["content"]
    assert len([p for p in content if p["type"] == "image_url"]) == 2
    assert "crack" in content[0]["text"], "类别表要一起交给模型"


def test_multimodal_record_without_the_second_image_is_refused():
    """缺配套图直接拒绝：不发请求、不拿 RGB 冒充红外。"""
    gateway = FakeGateway()
    database = FakeDatabase(_record(
        model_mode="multimodal", secondary_modality="infrared",
    ))
    with pytest.raises(ai_review_service.AIReviewInputError, match="缺少.*配图"):
        ai_review_service.review_detection(
            "record-1", PersonalReviewClient(gateway), database, FakeStorage(_images())
        )
    assert gateway.messages == []
    assert database.updated == [], "拒绝时不该写 AI 复核结论"


# --------------------------------------------------------------------------- #
# 解析失败不重复计费
# --------------------------------------------------------------------------- #
def test_parse_failure_is_not_retried():
    gateway = FakeGateway()
    gateway.text = "not json at all"
    database = FakeDatabase(_record())
    with pytest.raises(ai_review_service.AIReviewParseError):
        ai_review_service.review_detection(
            "record-1", PersonalReviewClient(gateway), database, FakeStorage(_images())
        )
    assert len(gateway.messages) == 1


def test_upstream_detail_never_reaches_the_public_message():
    class Boom(RuntimeError):
        pass

    gateway = FakeGateway()

    def explode(messages, tools=None, *, temperature=0.2):
        raise Boom("provider body must stay private")

    gateway.complete = explode
    database = FakeDatabase(_record())
    with pytest.raises(ai_review_service.AIReviewUpstreamError) as caught:
        ai_review_service.review_detection(
            "record-1", PersonalReviewClient(gateway), database, FakeStorage(_images())
        )
    assert caught.value.public_message == ai_review_service.UPSTREAM_FAILURE_SUMMARY
    assert "provider body" not in caught.value.public_message


# --------------------------------------------------------------------------- #
# review.ai：逐项失败必须可见
# --------------------------------------------------------------------------- #
def test_review_ai_reports_partial_failed_when_one_record_fails(tmp_path):
    from services.ai_review_service import AIReviewUpstreamError
    from tests.test_business_tool_chain import Fixture

    fixture = Fixture(tmp_path)
    dataset_id = fixture.dataset()
    model_id = fixture.model(dataset_id)
    first = fixture.record(model_id)
    second = fixture.record(model_id, version=2)

    class FlakyClient:
        def __init__(self):
            self.calls = 0

        def review_detection_structured(self, image_bytes, detections, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise AIReviewUpstreamError("upstream said: secret-token")
            return '{"status":"consistent","summary":"ok"}'

    that = FlakyClient()
    adapter = fixture.adapter(review_client_factory=lambda: that)
    prepared = adapter.prepare_tool(7, "review.ai", {"record_ids": [first, second]})
    result = adapter.execute_tool(7, prepared["operation_id"], "review-partial")

    outputs = result["outputs"]
    assert outputs["succeeded"] == 1
    assert outputs["failed"] == 1
    assert outputs["status"] == "partial_failed"
    failure = [item for item in outputs["items"] if item["status"] == "failed"][0]
    assert failure["error"] == "AI服务调用失败，需要人工复核"
    assert "secret-token" not in json.dumps(outputs, ensure_ascii=False)

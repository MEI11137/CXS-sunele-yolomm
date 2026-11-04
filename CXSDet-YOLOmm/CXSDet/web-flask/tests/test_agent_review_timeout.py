"""AI 复核单次请求放宽超时的回归测试（INT-52 后续修复）。

覆盖：
1. `OpenAIGateway.complete()` 仅在调用方传入 timeout 时用
   `with_options(timeout=..., max_retries=0)` 放宽本次请求，聊天默认行为不变；
2. `PersonalReviewClient` 复核/建议分别传 180s / 120s，且图片仍按原尺寸发送
   （不因超时修复做降采样，避免丢失小热斑信息）；
3. 批量复核遇到 `AgentError` 时展示可执行的原始提示，而不是只有异常类名。
"""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace

from PIL import Image

from agent.errors import AgentError, Code
from agent.llm.provider import OpenAIGateway, ProviderCredentials
from services.personal_review_client import PersonalReviewClient


def _credentials() -> ProviderCredentials:
    return ProviderCredentials(
        provider="deepseek",
        api_url="https://api.deepseek.com",
        model="deepseek-flash",
        api_key="sk-test",
    )


class _FakeChat:
    def __init__(self, calls):
        self.calls = calls

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        message = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeClient:
    def __init__(self):
        self.calls = []
        self.chat = SimpleNamespace(completions=_FakeChat(self.calls))

    def with_options(self, **kwargs):
        self.calls.append(("with_options", kwargs))
        return self


def test_complete_passes_per_request_timeout():
    fake = _FakeClient()
    gateway = OpenAIGateway(_credentials(), client_factory=lambda **_: fake)

    turn = gateway.complete(
        [{"role": "user", "content": "看一下这张图"}], temperature=0.1, timeout=180.0
    )

    assert turn.text == "ok"
    assert ("with_options", {"timeout": 180.0, "max_retries": 0}) in fake.calls
    create_kwargs = [kwargs for name, kwargs in fake.calls if name == "create"][0]
    assert create_kwargs["temperature"] == 0.1


def test_complete_without_timeout_keeps_default_client():
    fake = _FakeClient()
    gateway = OpenAIGateway(_credentials(), client_factory=lambda **_: fake)

    gateway.complete([{"role": "user", "content": "你好"}])

    assert all(name != "with_options" for name, _ in fake.calls)


class _FakeGateway:
    def __init__(self, text: str = "{}"):
        self.credentials = SimpleNamespace(model="deepseek-flash")
        self.text = text
        self.calls = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, **kwargs})
        return SimpleNamespace(text=self.text)


def _jpeg_bytes(size=(2000, 1000)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, "JPEG")
    return buffer.getvalue()


def test_review_structured_uses_180s_and_keeps_original_image_size():
    gateway = _FakeGateway('{"status": "consistent", "summary": "ok"}')
    client = PersonalReviewClient(gateway)

    text = client.review_detection_structured(_jpeg_bytes((2000, 1000)), [])

    assert text.startswith('{"status"')
    call = gateway.calls[0]
    assert call["timeout"] == 180.0
    assert call["temperature"] == 0.1
    image_url = call["messages"][0]["content"][1]["image_url"]["url"]
    raw = base64.b64decode(image_url.split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as image:
        # 原图尺寸发送，不做降采样（避免小热斑被抹掉）。
        assert image.size == (2000, 1000)


def test_improvement_suggestions_use_120s():
    gateway = _FakeGateway("建议人工复核框 3")
    client = PersonalReviewClient(gateway)

    assert client.generate_improvement_suggestions({"summary": "x"}) == "建议人工复核框 3"
    assert gateway.calls[0]["timeout"] == 120.0


def test_batch_review_surfaces_actionable_agent_error(monkeypatch):
    import services.batch_review_service as batch

    monkeypatch.setattr(batch, "_load_accessible_record", lambda *args, **kwargs: object())

    def factory():
        raise AgentError(
            Code.MODEL_CAPABILITY_MISSING,
            "当前连接尚未验证「vision」能力，请先在模型设置中执行能力检查",
            http_status=409,
        )

    result = batch.run_batch_ai_reviews(
        ["record-1"],
        3,
        False,
        object(),
        object(),
        lambda *args, **kwargs: None,
        factory,
    )

    item = result["items"][0]
    assert item["status"] == "failed"
    assert item["error"] == (
        "AI辅助复核失败: 当前连接尚未验证「vision」能力，请先在模型设置中执行能力检查"
    )


def _run_batch_with_agent_error(monkeypatch, error: AgentError) -> str:
    import services.batch_review_service as batch

    monkeypatch.setattr(batch, "_load_accessible_record", lambda *args, **kwargs: object())

    def factory():
        raise error

    result = batch.run_batch_ai_reviews(
        ["record-1"],
        3,
        False,
        object(),
        object(),
        lambda *args, **kwargs: None,
        factory,
    )
    return result["items"][0]["error"]


def test_batch_review_redacts_secrets_from_agent_error(monkeypatch):
    """白名单错误码的提示也要脱敏：上游消息里的 sk/Bearer 不能外露。"""
    error = AgentError(
        Code.MODEL_CALL_FAILED,
        "上游返回 sk-abcdefghijklmnop1234 与 Bearer abcdefghijklmnopqrstuvwxyz012345",
    )

    text = _run_batch_with_agent_error(monkeypatch, error)

    assert "sk-abcdefghijklmnop1234" not in text
    assert "abcdefghijklmnopqrstuvwxyz012345" not in text
    assert "[敏感凭据已隐藏]" in text


def test_batch_review_masks_untrusted_agent_error(monkeypatch):
    """白名单之外的错误码不得把上游原始报错整段返回给用户。"""
    error = AgentError(
        "AGENT_UPSTREAM_RAW",
        "upstream rejected: {\"error\": \"<html>stack trace</html>\"}",
    )

    text = _run_batch_with_agent_error(monkeypatch, error)

    assert "upstream rejected" not in text
    assert "<html>" not in text
    assert "本轮复核未执行，请检查个人连接与模型设置后重试" in text
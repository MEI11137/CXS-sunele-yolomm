import io

from PIL import Image


def _image_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, "JPEG")
    return buffer.getvalue()


def test_default_client_uses_user_selected_default_model(monkeypatch):
    import algo.llm.llm_vl_client as module
    from algo.llm.config import DEFAULT_VL_MODEL

    captured = {}

    class FakeOpenAI:
        def __init__(self, api_key, base_url):
            captured["base_url"] = base_url
            self.api_key_was_present = bool(api_key)

    monkeypatch.setattr(module, "OpenAI", FakeOpenAI)
    client = module.LLMVLClient()

    assert client.model_name == DEFAULT_VL_MODEL
    assert captured["base_url"]
    assert client.client.api_key_was_present is True


def test_structured_review_requests_image_and_json_contract(monkeypatch):
    import algo.llm.llm_vl_client as module
    from algo.llm.config import DEFAULT_VL_MODEL

    captured = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            message = type("Message", (), {"content": '{"status":"consistent","summary":"ok"}'})()
            choice = type("Choice", (), {"message": message})()
            return type("Completion", (), {"choices": [choice]})()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = type("Chat", (), {"completions": Completions()})()

    monkeypatch.setattr(module, "OpenAI", FakeOpenAI)
    client = module.LLMVLClient(DEFAULT_VL_MODEL)
    response = client.review_detection_structured(
        _image_bytes(),
        [{"class_name": "crack", "confidence": 0.8, "bbox": [1, 1, 3, 3]}],
    )

    assert '"status":"consistent"' in response
    content = captured["messages"][0]["content"]
    assert any(item["type"] == "image_url" for item in content)
    assert "possible_false_positive" in content[0]["text"]

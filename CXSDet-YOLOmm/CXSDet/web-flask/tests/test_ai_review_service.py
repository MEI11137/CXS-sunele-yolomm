import json
import io
from types import SimpleNamespace

from PIL import Image


class FakeDatabase:
    def __init__(self, record):
        self.record = record
        self.updated = None

    def query(self, sql, params=(), fetchone=False):
        return self.record

    def execute(self, sql, params=()):
        self.updated = (sql, params)
        return 1


class FakeStorage:
    def get(self, bucket, object_key):
        return b"image"


def _record():
    return {
        "id": "record-1",
        "original_image_bucket": "original",
        "original_image_object_key": "rgb.jpg",
        "detection_result": json.dumps(
            [{"class_name": "crack", "confidence": 0.9, "bbox": [1, 2, 3, 4]}]
        ),
    }


def test_ai_review_parses_fenced_json_and_updates_same_record():
    from services.ai_review_service import review_detection

    class FakeClient:
        def review_detection_structured(self, image_bytes, detections, **kwargs):
            return """```json
            {
              "status": "possible_false_positive",
              "summary": "the marked region looks like glare",
              "possible_false_positives": [0],
              "possible_missed_detections": [],
              "attention_regions": []
            }
            ```"""

    database = FakeDatabase(_record())
    result = review_detection("record-1", FakeClient(), database, FakeStorage())

    assert result.status == "possible_false_positive"
    assert result.possible_false_positives == [0]
    assert "UPDATE detection_records" in database.updated[0]
    assert database.updated[1][-1] == "record-1"
    assert "possible_false_positive" in database.updated[1]


def test_parse_failure_does_not_call_the_model_again():
    """INT-06：格式错误只报失败，不自动再发一次付费请求。"""
    import pytest
    from services.ai_review_service import AIReviewParseError, review_detection

    class FakeClient:
        config = {"model": "vision-test"}

        def __init__(self):
            self.responses = iter([
                "not-json",
                '{"status":"consistent","summary":"second attempt worked"}',
            ])
            self.calls = 0

        def review_detection_structured(self, image_bytes, detections, **kwargs):
            self.calls += 1
            return next(self.responses)

    client = FakeClient()
    database = FakeDatabase(_record())
    with pytest.raises(AIReviewParseError, match="AI返回格式异常"):
        review_detection("record-1", client, database, FakeStorage())

    assert client.calls == 1
    assert json.loads(database.updated[1][1])["status"] == "needs_human_review"


def test_repeated_parse_failure_persists_fallback_then_raises():
    import pytest
    from services.ai_review_service import AIReviewParseError, review_detection

    class FakeClient:
        config = {"model": "vision-test"}

        def __init__(self):
            self.calls = 0

        def review_detection_structured(self, image_bytes, detections, **kwargs):
            self.calls += 1
            return "still-not-json"

    client = FakeClient()
    database = FakeDatabase(_record())

    with pytest.raises(AIReviewParseError, match="AI返回格式异常"):
        review_detection("record-1", client, database, FakeStorage())

    persisted = json.loads(database.updated[1][1])
    assert client.calls == 1
    assert persisted["status"] == "needs_human_review"
    assert persisted["summary"] == "AI返回格式异常，需要人工复核"


def test_malformed_optional_field_fails_once_without_retry():
    import pytest
    from services.ai_review_service import AIReviewParseError, review_detection

    class FakeClient:
        config = {"model": "vision-test"}

        def __init__(self):
            self.calls = 0

        def review_detection_structured(self, image_bytes, detections, **kwargs):
            self.calls += 1
            return '{"status":"consistent","possible_false_positives":null}'

    client = FakeClient()
    database = FakeDatabase(_record())

    with pytest.raises(AIReviewParseError, match="AI返回格式异常") as error:
        review_detection("record-1", client, database, FakeStorage())

    assert isinstance(error.value.__cause__, AIReviewParseError)
    assert client.calls == 1
    persisted = json.loads(database.updated[1][1])
    assert persisted["status"] == "needs_human_review"
    assert persisted["summary"] == "AI返回格式异常，需要人工复核"


def test_parser_rejects_non_object_top_level_payload():
    import pytest
    from services.ai_review_service import AIReviewParseError, _parse_review_payload

    with pytest.raises(AIReviewParseError, match="JSON对象"):
        _parse_review_payload('[{"status":"consistent"}]')


def test_upstream_failure_persists_fallback_without_retry():
    import pytest
    from services.ai_review_service import AIReviewUpstreamError, review_detection

    class UpstreamFailure(RuntimeError):
        status_code = 503
        code = "service_unavailable"

    class FakeClient:
        config = {"model": "vision-test"}

        def __init__(self):
            self.calls = 0

        def review_detection_structured(self, image_bytes, detections, **kwargs):
            self.calls += 1
            raise UpstreamFailure("provider body must stay private")

    client = FakeClient()
    database = FakeDatabase(_record())

    with pytest.raises(AIReviewUpstreamError, match="AI服务调用失败"):
        review_detection("record-1", client, database, FakeStorage())

    persisted = json.loads(database.updated[1][1])
    assert client.calls == 1
    assert persisted["status"] == "needs_human_review"
    assert persisted["summary"] == "AI服务调用失败，需要人工复核"


def test_upstream_failure_logs_categories_without_provider_secrets(caplog):
    import logging
    import pytest
    from services.ai_review_service import AIReviewUpstreamError, review_detection

    class ProviderFailure(RuntimeError):
        status_code = "SECRET-STATUS"
        code = "SECRET-CODE"

    class FakeClient:
        config = {"model": "vision-test", "api_key": "SECRET-API-KEY"}

        def review_detection_structured(self, image_bytes, detections, **kwargs):
            raise ProviderFailure("SECRET-MESSAGE")

    with caplog.at_level(logging.WARNING, logger="services.ai_review_service"):
        with pytest.raises(AIReviewUpstreamError):
            review_detection(
                "record-1", FakeClient(), FakeDatabase(_record()), FakeStorage()
            )

    assert "ai_review_upstream_failure" in caplog.text
    assert "record_id=record-1" in caplog.text
    assert "model=vision-test" in caplog.text
    assert "exception_type=ProviderFailure" in caplog.text
    assert "status_code=unknown" in caplog.text
    assert "error_code=unknown" in caplog.text
    for secret in (
        "SECRET-STATUS", "SECRET-CODE", "SECRET-MESSAGE", "SECRET-API-KEY", "class_name"
    ):
        assert secret not in caplog.text


def test_parse_failure_logs_metadata_without_sensitive_values(caplog):
    import logging
    import pytest
    from services.ai_review_service import AIReviewParseError, review_detection

    class FakeClient:
        config = {
            "model": "vision-test",
            "api_key": "SECRET-API-KEY",
        }

        def review_detection_structured(self, image_bytes, detections, **kwargs):
            assert image_bytes == b"image"
            return "SECRET-RAW-RESPONSE"

    database = FakeDatabase(_record())
    with caplog.at_level(logging.WARNING, logger="services.ai_review_service"):
        with pytest.raises(AIReviewParseError):
            review_detection("record-1", FakeClient(), database, FakeStorage())

    assert "record-1" in caplog.text
    assert "vision-test" in caplog.text
    assert "attempt=" not in caplog.text
    assert "raw_type=str" in caplog.text
    assert "raw_length=19" in caplog.text
    assert "SECRET-API-KEY" not in caplog.text
    assert "SECRET-RAW-RESPONSE" not in caplog.text
    assert "class_name" not in caplog.text


def test_health_check_reports_model_availability_without_secret_values():
    from services.ai_review_service import check_vl_health

    class FakeClient:
        def health_check(self):
            return {"ok": True, "model": "vision-model", "available": True}

    health = check_vl_health(FakeClient())
    assert health == {"ok": True, "model": "vision-model", "available": True}
    assert "key" not in json.dumps(health).lower()


def test_vl_review_prompt_defines_box_coordinates_and_zero_based_detection_indexes():
    """AI output must map unambiguously to numbered human-review annotations."""
    from algo.llm.llm_vl_client import LLMVLClient

    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"][0]["text"]
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"status":"consistent"}'))]
            )

    fake_openai = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )
    client = object.__new__(LLMVLClient)
    client.config = {"model": "vision-test"}
    client.client = fake_openai
    image_buffer = io.BytesIO()
    Image.new("RGB", (20, 10), "white").save(image_buffer, format="PNG")

    client.review_detection_structured(
        image_buffer.getvalue(),
        [{"class_name": "crack", "bbox": [1, 2, 3, 4]}],
    )

    assert "从0开始" in captured["prompt"]
    assert "0到1归一化坐标" in captured["prompt"]

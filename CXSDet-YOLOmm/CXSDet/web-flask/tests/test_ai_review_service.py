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
        def review_detection_structured(self, image_bytes, detections):
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


def test_malformed_ai_output_becomes_human_review_instead_of_ground_truth():
    from services.ai_review_service import review_detection

    class FakeClient:
        def review_detection_structured(self, image_bytes, detections):
            return "not-json"

    database = FakeDatabase(_record())
    result = review_detection("record-1", FakeClient(), database, FakeStorage())

    assert result.status == "needs_human_review"
    assert "无法解析" in result.summary
    assert "needs_human_review" in database.updated[1]


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

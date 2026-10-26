import json
from types import SimpleNamespace

from flask import Flask

from utils.jwt_util import generate_token


def test_record_ai_review_returns_structured_result(monkeypatch):
    import routes.detection as detection_route

    class FakeDb:
        def query(self, sql, params=(), fetchone=False):
            return {"id": "r1", "user_id": 3}

    expected = {
        "status": "consistent",
        "summary": "matches",
        "possible_false_positives": [],
        "possible_missed_detections": [],
        "attention_regions": [],
    }
    monkeypatch.setattr(detection_route, "db", FakeDb())
    monkeypatch.setattr(detection_route, "is_admin", lambda: False)
    monkeypatch.setattr(detection_route, "_personal_review_client", lambda: object())
    monkeypatch.setattr(
        detection_route,
        "review_detection",
        lambda record_id, client, database, storage: SimpleNamespace(to_dict=lambda: expected),
        raising=False,
    )

    app = Flask(__name__)
    app.register_blueprint(detection_route.detection_bp)
    response = app.test_client().post(
        "/api/detection/records/r1/ai-review",
        headers={"Authorization": f"Bearer {generate_token(3)}"},
    )
    body = json.loads(response.data)

    assert body["code"] == 200
    assert body["data"] == expected


def test_ai_health_never_returns_credentials(monkeypatch):
    import routes.detection as detection_route
    import utils.security_utils as security_utils

    monkeypatch.setattr(security_utils, "is_admin", lambda: True)
    monkeypatch.setattr(detection_route, "_personal_review_client", lambda: object())
    monkeypatch.setattr(
        detection_route,
        "check_vl_health",
        lambda client: {"ok": True, "model": "vision", "available": True},
        raising=False,
    )
    app = Flask(__name__)
    app.register_blueprint(detection_route.detection_bp)
    response = app.test_client().get(
        "/api/detection/llm/health",
        headers={"Authorization": f"Bearer {generate_token(1)}"},
    )
    body = json.loads(response.data)

    assert body["code"] == 200
    assert body["data"] == {"ok": True, "model": "vision", "available": True}
    assert "key" not in response.get_data(as_text=True).lower()


def test_batch_ai_review_route_returns_per_record_summary(monkeypatch):
    import routes.detection as route

    expected = {
        "requested": 2,
        "unique": 2,
        "succeeded": 1,
        "failed": 1,
        "items": [
            {"record_id": "r1", "status": "success", "result": {"status": "consistent"}},
            {"record_id": "missing", "status": "failed", "error": "识别记录不存在或无权访问"},
        ],
    }
    monkeypatch.setattr(route, "is_admin", lambda: False)
    monkeypatch.setattr(route, "run_batch_ai_reviews", lambda *args, **kwargs: expected, raising=False)

    app = Flask(__name__)
    app.register_blueprint(route.detection_bp)
    response = app.test_client().post(
        "/api/detection/records/ai-review/batch",
        headers={"Authorization": f"Bearer {generate_token(3)}"},
        json={"record_ids": ["r1", "missing"]},
    )
    body = json.loads(response.data)

    assert response.status_code == 200
    assert body["data"] == expected


def test_record_ai_review_returns_real_http_502_for_typed_failure(monkeypatch):
    import routes.detection as detection_route

    from services.ai_review_service import AIReviewParseError

    class FakeDb:
        def query(self, sql, params=(), fetchone=False):
            return {"id": "r1", "user_id": 3}

    def fail_review(*args, **kwargs):
        raise AIReviewParseError("private implementation detail")

    monkeypatch.setattr(detection_route, "db", FakeDb())
    monkeypatch.setattr(detection_route, "is_admin", lambda: False)
    monkeypatch.setattr(detection_route, "_personal_review_client", lambda: object())
    monkeypatch.setattr(detection_route, "review_detection", fail_review, raising=False)

    app = Flask(__name__)
    app.register_blueprint(detection_route.detection_bp)
    response = app.test_client().post(
        "/api/detection/records/r1/ai-review",
        headers={"Authorization": f"Bearer {generate_token(3)}"},
    )
    body = response.get_json()

    assert response.status_code == 502
    assert body["code"] == 502
    assert body["msg"] == "AI返回格式异常，需要人工复核"
    assert "private implementation detail" not in response.get_data(as_text=True)

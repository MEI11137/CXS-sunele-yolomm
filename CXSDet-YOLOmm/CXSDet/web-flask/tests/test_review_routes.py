import json
from types import SimpleNamespace

from flask import Flask

from utils.jwt_util import generate_token


def test_human_review_route_returns_new_version(monkeypatch):
    import routes.detection as route

    monkeypatch.setattr(route, "db", object())
    monkeypatch.setattr(route, "is_admin", lambda: True)
    monkeypatch.setattr(
        route,
        "submit_human_review",
        lambda *args, **kwargs: SimpleNamespace(to_dict=lambda: {"record_id": "r1", "new_version": 2}),
        raising=False,
    )
    app = Flask(__name__)
    app.register_blueprint(route.detection_bp)
    response = app.test_client().post(
        "/api/detection/records/r1/human-review",
        headers={"Authorization": f"Bearer {generate_token(3)}"},
        json={"expected_version": 1, "decision": "confirmed", "annotations": []},
    )
    body = json.loads(response.data)
    assert body["code"] == 200
    assert body["data"]["new_version"] == 2


def test_human_review_route_maps_conflict_to_409(monkeypatch):
    import routes.detection as route
    from services.review_service import ReviewConflictError

    monkeypatch.setattr(route, "is_admin", lambda: True)

    monkeypatch.setattr(
        route,
        "submit_human_review",
        lambda *args, **kwargs: (_ for _ in ()).throw(ReviewConflictError("version conflict")),
        raising=False,
    )
    app = Flask(__name__)
    app.register_blueprint(route.detection_bp)
    response = app.test_client().post(
        "/api/detection/records/r1/human-review",
        headers={"Authorization": f"Bearer {generate_token(3)}"},
        json={"expected_version": 0, "decision": "confirmed", "annotations": []},
    )
    assert response.status_code == 409


def test_add_feedback_route_uses_existing_confirmed_record(monkeypatch):
    import routes.detection as route

    monkeypatch.setattr(route, "is_admin", lambda: True)
    monkeypatch.setattr(
        route,
        "add_confirmed_record",
        lambda *args, **kwargs: SimpleNamespace(to_dict=lambda: {"sample_id": "s1", "created": True}),
        raising=False,
    )
    app = Flask(__name__)
    app.register_blueprint(route.detection_bp)
    response = app.test_client().post(
        "/api/detection/records/r1/feedback",
        headers={"Authorization": f"Bearer {generate_token(3)}"},
        json={"dataset_id": "d1"},
    )
    assert json.loads(response.data)["data"]["sample_id"] == "s1"


def test_batch_feedback_route_automatically_resolves_source_datasets(monkeypatch):
    import routes.detection as route

    expected = {
        "requested": 1,
        "unique": 1,
        "succeeded": 1,
        "failed": 0,
        "items": [{"record_id": "r1", "status": "success", "dataset_id": "d1"}],
        "dataset_groups": [{"dataset_id": "d1", "dataset_name": "原训练集", "record_ids": ["r1"]}],
    }
    monkeypatch.setattr(route, "is_admin", lambda: False)
    monkeypatch.setattr(
        route, "add_batch_confirmed_records", lambda *args, **kwargs: expected, raising=False
    )

    app = Flask(__name__)
    app.register_blueprint(route.detection_bp)
    response = app.test_client().post(
        "/api/detection/records/feedback/batch",
        headers={"Authorization": f"Bearer {generate_token(3)}"},
        json={"record_ids": ["r1"]},
    )

    assert response.status_code == 200
    assert json.loads(response.data)["data"] == expected

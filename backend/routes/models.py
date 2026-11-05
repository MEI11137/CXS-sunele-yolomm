"""模型接口：导入、发布（仅当前项目）、归档、列表。"""

from __future__ import annotations

from flask import Blueprint, request

from backend import auth, db
from backend.errors import ApiError, ok
from backend.services import models as model_service

bp = Blueprint("models", __name__)


def _user() -> dict:
    user = auth.current_user()
    assert user is not None
    return user


@bp.get("/projects/<project_id>/models")
@auth.login_required
def list_models(project_id: str):
    conn = db.get_db()
    return ok(model_service.list_for_project(conn, int(_user()["id"]), project_id))


@bp.post("/projects/<project_id>/models")
@auth.login_required
def import_model(project_id: str):
    payload = request.get_json(silent=True) or {}
    class_map = payload.get("class_map")
    if class_map is not None and not isinstance(class_map, dict):
        raise ApiError(400, "VALIDATION", "class_map 必须是 {权重索引: 数据集索引}")
    conn = db.get_db()
    model = model_service.import_model(
        conn,
        int(_user()["id"]),
        project_id,
        name=str(payload.get("name") or ""),
        family=str(payload.get("family") or ""),
        model_mode=payload.get("model_mode"),
        secondary_modality=payload.get("secondary_modality"),
        dataset_id=payload.get("dataset_id"),
        class_map=class_map,
        asset_id=payload.get("asset_id"),
        rel_path=payload.get("rel_path"),
        notes=str(payload.get("notes") or ""),
    )
    return ok({"model": model})


@bp.get("/projects/<project_id>/models/<model_id>")
@auth.login_required
def get_model(project_id: str, model_id: str):
    conn = db.get_db()
    row = model_service.get_owned(conn, int(_user()["id"]), project_id, model_id)
    return ok({"model": model_service.public(row)})


@bp.post("/projects/<project_id>/models/<model_id>/publish")
@auth.login_required
def publish_model(project_id: str, model_id: str):
    conn = db.get_db()
    row = model_service.publish(conn, int(_user()["id"]), project_id, model_id)
    return ok({"model": row})


@bp.post("/projects/<project_id>/models/<model_id>/archive")
@auth.login_required
def archive_model(project_id: str, model_id: str):
    conn = db.get_db()
    row = model_service.archive(conn, int(_user()["id"]), project_id, model_id)
    return ok({"model": row})

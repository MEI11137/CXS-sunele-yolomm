"""模型接口：导入、训练产出、发布/撤销、取代说明、软删恢复（仅当前项目）。"""

from __future__ import annotations

from flask import Blueprint, request

from backend import auth, db
from backend.errors import ApiError, ok
from backend.services import models as model_service
from backend.services import projects as project_service

bp = Blueprint("models", __name__)


def _user() -> dict:
    user = auth.current_user()
    assert user is not None
    return user


@bp.get("/projects/<project_id>/models")
@auth.login_required
def list_models(project_id: str):
    conn = db.get_db()
    # 别人的项目按“不存在”处理：不能返回空列表让人以为这个项目没有模型。
    project_service.get_owned(conn, project_id, int(_user()["id"]))
    include_deleted = str(request.args.get("include_deleted") or "").lower() in {"1", "true", "yes"}
    return ok(
        model_service.list_for_project(
            conn, int(_user()["id"]), project_id, include_deleted=include_deleted
        )
    )


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


@bp.post("/projects/<project_id>/models/inspect")
@auth.login_required
def inspect_model(project_id: str):
    """只读预检：先看清权重真实结构和类别，再决定 family，避免导入后才失败。"""
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    data = model_service.inspect_weight_source(
        conn,
        int(_user()["id"]),
        project_id,
        rel_path=payload.get("rel_path"),
        asset_id=payload.get("asset_id"),
    )
    return ok(data)


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


@bp.patch("/projects/<project_id>/models/<model_id>")
@auth.login_required
def patch_model(project_id: str, model_id: str):
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    row = model_service.update_model(
        conn,
        int(_user()["id"]),
        project_id,
        model_id,
        name=payload.get("name"),
        notes=payload.get("notes"),
    )
    return ok({"model": row})


@bp.post("/projects/<project_id>/models/<model_id>/unpublish")
@auth.login_required
def unpublish_model(project_id: str, model_id: str):
    conn = db.get_db()
    row = model_service.unpublish(conn, int(_user()["id"]), project_id, model_id)
    return ok({"model": row})


@bp.delete("/projects/<project_id>/models/<model_id>")
@auth.login_required
def delete_model(project_id: str, model_id: str):
    conn = db.get_db()
    row = model_service.delete(conn, int(_user()["id"]), project_id, model_id)
    return ok({"model": row})


@bp.post("/projects/<project_id>/models/<model_id>/restore")
@auth.login_required
def restore_model(project_id: str, model_id: str):
    conn = db.get_db()
    row = model_service.restore(conn, int(_user()["id"]), project_id, model_id)
    return ok({"model": row})

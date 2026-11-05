"""数据集接口：导入（ZIP/目录）、列表、样本清单。"""

from __future__ import annotations

from flask import Blueprint, request

from backend import auth, db
from backend.errors import ApiError, ok
from backend.services import datasets as dataset_service

bp = Blueprint("datasets", __name__)


def _user() -> dict:
    user = auth.current_user()
    assert user is not None
    return user


@bp.get("/projects/<project_id>/datasets")
@auth.login_required
def list_datasets(project_id: str):
    conn = db.get_db()
    return ok(dataset_service.list_for_project(conn, int(_user()["id"]), project_id))


@bp.post("/projects/<project_id>/datasets")
@auth.login_required
def import_dataset(project_id: str):
    payload = request.get_json(silent=True) or {}
    modalities = payload.get("modalities") or ["rgb"]
    if not isinstance(modalities, list):
        raise ApiError(400, "VALIDATION", "modalities 必须是数组")
    classes = payload.get("classes")
    if classes is not None and not isinstance(classes, list):
        raise ApiError(400, "VALIDATION", "classes 必须是数组")
    conn = db.get_db()
    dataset = dataset_service.import_dataset(
        conn,
        int(_user()["id"]),
        project_id,
        name=str(payload.get("name") or ""),
        modalities=[str(item) for item in modalities],
        secondary_modality=payload.get("secondary_modality"),
        classes=[str(item) for item in classes] if classes else None,
        asset_id=payload.get("asset_id"),
        rel_path=payload.get("rel_path"),
    )
    return ok({"dataset": dataset})


@bp.get("/projects/<project_id>/datasets/<dataset_id>")
@auth.login_required
def get_dataset(project_id: str, dataset_id: str):
    conn = db.get_db()
    row = dataset_service.get_owned(conn, int(_user()["id"]), project_id, dataset_id)
    return ok({"dataset": dataset_service.public(row)})


@bp.get("/projects/<project_id>/datasets/<dataset_id>/samples")
@auth.login_required
def list_samples(project_id: str, dataset_id: str):
    conn = db.get_db()
    return ok(
        dataset_service.samples(
            conn,
            int(_user()["id"]),
            project_id,
            dataset_id,
            split=(request.args.get("split") or "").strip() or None,
            limit=int(request.args.get("limit") or 200),
        )
    )

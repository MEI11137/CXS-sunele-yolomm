"""数据集接口：导入（ZIP/目录）、列表、样本清单。"""

from __future__ import annotations

from flask import Blueprint, request, send_file

from backend import auth, db
from backend.errors import ApiError, ok
from backend.services import datasets as dataset_service
from backend.services import identity as identity_service
from backend.services import projects as project_service
from backend.services import reflow as reflow_service

bp = Blueprint("datasets", __name__)


def _user() -> dict:
    user = auth.current_user()
    assert user is not None
    return user


def _assert_project(conn, project_id: str, user_id: int) -> None:
    """别人的项目与不存在的项目一样返回 404：不能拿一个空列表冒充“这里没数据”。"""
    project_service.get_owned(conn, project_id, user_id)


def _limit(default: int = 50, maximum: int = 200) -> int:
    raw = request.args.get("limit")
    try:
        value = int(raw if raw not in (None, "") else default)
    except (TypeError, ValueError):
        raise ApiError(400, "BAD_LIMIT", "limit 必须是整数") from None
    return max(1, min(maximum, value))


@bp.get("/projects/<project_id>/datasets")
@auth.login_required
def list_datasets(project_id: str):
    conn = db.get_db()
    _assert_project(conn, project_id, int(_user()["id"]))
    return ok(
        dataset_service.list_for_project(
            conn,
            int(_user()["id"]),
            project_id,
            limit=_limit(50),
            deleted=(request.args.get("deleted") or "exclude").strip(),
        )
    )


@bp.post("/projects/<project_id>/datasets/preview")
@auth.login_required
def preview_dataset(project_id: str):
    """只读识别 ZIP/目录的建议名称、类别、模态与划分；不创建任何数据集。"""
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    _assert_project(conn, project_id, int(_user()["id"]))
    return ok(
        dataset_service.preview_source(
            conn,
            int(_user()["id"]),
            project_id,
            asset_id=payload.get("asset_id") or None,
            rel_path=payload.get("rel_path") or None,
        )
    )


@bp.post("/projects/<project_id>/datasets")
@auth.login_required
def import_dataset(project_id: str):
    payload = request.get_json(silent=True) or {}
    modalities_payload = payload.get("modalities")
    if modalities_payload is None:
        # 省略 modalities = 按压缩包内 data.yaml 自动识别主/辅模态。
        modalities = None
    elif not isinstance(modalities_payload, list):
        raise ApiError(400, "VALIDATION", "modalities 必须是数组")
    else:
        modalities = [str(item) for item in modalities_payload]
    classes = payload.get("classes")
    if classes is not None and not isinstance(classes, list):
        raise ApiError(400, "VALIDATION", "classes 必须是数组")
    conn = db.get_db()
    dataset = dataset_service.import_dataset(
        conn,
        int(_user()["id"]),
        project_id,
        name=str(payload.get("name") or ""),
        modalities=modalities,
        secondary_modality=payload.get("secondary_modality"),
        reference_modality=payload.get("reference_modality"),
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
            limit=_limit(200),
        )
    )


@bp.get("/projects/<project_id>/datasets/<dataset_id>/report")
@auth.login_required
def dataset_report(project_id: str, dataset_id: str):
    """只读身份检查：重复、跨划分泄漏、标注冲突。不改任何数据。"""
    conn = db.get_db()
    return ok(identity_service.dataset_report(conn, int(_user()["id"]), project_id, dataset_id))


@bp.post("/projects/<project_id>/datasets/<dataset_id>/match")
@auth.login_required
def match_records(project_id: str, dataset_id: str):
    payload = request.get_json(silent=True) or {}
    ids = payload.get("record_ids")
    if not isinstance(ids, list) or not ids:
        raise ApiError(400, "VALIDATION", "需要 record_ids")
    conn = db.get_db()
    return ok(
        {
            "matches": identity_service.match_records(
                conn, int(_user()["id"]), project_id, dataset_id, [str(item) for item in ids]
            )
        }
    )


@bp.post("/projects/<project_id>/reflow/preview")
@auth.login_required
def reflow_preview(project_id: str):
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    return ok(
        reflow_service.preview(
            conn,
            int(_user()["id"]),
            project_id,
            mode=str(payload.get("mode") or "revision"),
            dataset_id=payload.get("dataset_id"),
            selections=payload.get("selections") or [],
            strategy=str(payload.get("strategy") or "even"),
            ratios=payload.get("ratios"),
            seed=int(payload.get("seed") or 0),
            reference_modality=payload.get("reference_modality"),
        )
    )


@bp.post("/projects/<project_id>/reflow/apply")
@auth.login_required
def reflow_apply(project_id: str):
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    return ok(
        {
            "dataset": reflow_service.apply(
                conn,
                int(_user()["id"]),
                project_id,
                mode=str(payload.get("mode") or "revision"),
                dataset_id=payload.get("dataset_id"),
                selections=payload.get("selections") or [],
                name=str(payload.get("name") or ""),
                strategy=str(payload.get("strategy") or "even"),
                ratios=payload.get("ratios"),
                seed=int(payload.get("seed") or 0),
                request_key=str(payload.get("request_key") or ""),
                session_id=(payload.get("session_id") or None),
                reference_modality=payload.get("reference_modality"),
            )
        }
    )


@bp.get("/projects/<project_id>/datasets/<dataset_id>/export")
@auth.login_required
def download_export(project_id: str, dataset_id: str):
    conn = db.get_db()
    row = dataset_service.get_owned(conn, int(_user()["id"]), project_id, dataset_id)
    rel = str(row["export_rel"] or "")
    if not rel:
        raise ApiError(404, "NO_EXPORT", "这个数据集还没有导出包")
    from backend.services import storage

    root = storage.project_root(conn, int(_user()["id"]), project_id)
    path = storage.resolve_rel(root, rel, must_exist=True)
    return send_file(path, mimetype="application/zip", as_attachment=True,
                     download_name=f"{row['name']}.zip", max_age=0)


# --------------------------------------------------------------------------- 草稿 / 定稿 / 重新划分


@bp.get("/projects/<project_id>/reflow/drafts")
@auth.login_required
def list_reflow_drafts(project_id: str):
    conn = db.get_db()
    _assert_project(conn, project_id, int(_user()["id"]))
    return ok(
        reflow_service.list_drafts(
            conn,
            int(_user()["id"]),
            project_id,
            include_deleted=(request.args.get("include_deleted") or "") in {"1", "true", "yes"},
        )
    )


@bp.post("/projects/<project_id>/reflow/drafts")
@auth.login_required
def save_reflow_draft(project_id: str):
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    draft = reflow_service.save_draft(
        conn,
        int(_user()["id"]),
        project_id,
        name=str(payload.get("name") or ""),
        mode=str(payload.get("mode") or "revision"),
        dataset_id=payload.get("dataset_id"),
        selections=payload.get("selections") or [],
        strategy=str(payload.get("strategy") or "even"),
        ratios=payload.get("ratios"),
        seed=int(payload.get("seed") or 0),
        session_id=payload.get("session_id") or None,
        draft_id=payload.get("draft_id") or None,
        reference_modality=payload.get("reference_modality"),
    )
    return ok({"draft": draft})


@bp.post("/projects/<project_id>/reflow/drafts/<draft_id>/finalize")
@auth.login_required
def finalize_reflow_draft(project_id: str, draft_id: str):
    conn = db.get_db()
    return ok(reflow_service.finalize_draft(conn, int(_user()["id"]), project_id, draft_id))


@bp.post("/projects/<project_id>/reflow/drafts/<draft_id>/delete")
@auth.login_required
def delete_reflow_draft(project_id: str, draft_id: str):
    conn = db.get_db()
    return ok(reflow_service.delete_draft(conn, int(_user()["id"]), project_id, draft_id))


@bp.post("/projects/<project_id>/reflow/drafts/<draft_id>/restore")
@auth.login_required
def restore_reflow_draft(project_id: str, draft_id: str):
    conn = db.get_db()
    return ok(reflow_service.restore_draft(conn, int(_user()["id"]), project_id, draft_id))


@bp.post("/projects/<project_id>/datasets/<dataset_id>/resplit")
@auth.login_required
def resplit_dataset(project_id: str, dataset_id: str):
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    return ok(
        {
            "dataset": reflow_service.resplit(
                conn,
                int(_user()["id"]),
                project_id,
                dataset_id,
                strategy=str(payload.get("strategy") or "even"),
                ratios=payload.get("ratios"),
                seed=int(payload.get("seed") or 0),
                request_key=str(payload.get("request_key") or ""),
                session_id=payload.get("session_id") or None,
            )
        }
    )


@bp.post("/projects/<project_id>/datasets/<dataset_id>/delete")
@auth.login_required
def delete_dataset(project_id: str, dataset_id: str):
    conn = db.get_db()
    return ok(dataset_service.soft_delete(conn, int(_user()["id"]), project_id, dataset_id))


@bp.post("/projects/<project_id>/datasets/<dataset_id>/restore")
@auth.login_required
def restore_dataset(project_id: str, dataset_id: str):
    conn = db.get_db()
    return ok(dataset_service.restore(conn, int(_user()["id"]), project_id, dataset_id))

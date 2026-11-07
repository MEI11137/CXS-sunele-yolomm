"""识别记录、AI 核查、后台作业接口。

约定：

* 单图检测同步返回记录（交互要即时）；批量与核查走作业，立刻返回 job_id；
* 所有列表都带“当前筛选 + 真实 ID + 时间口径”，Agent 拿到的就是界面看到的；
* 记录详情里附最近一次 AI 核查，但**不**把它与人工状态混为一谈。
"""

from __future__ import annotations

from flask import Blueprint, request, send_file

from backend import auth, db, security
from backend.errors import ApiError, ok
from backend.services import aireview
from backend.services import exports as export_service, detection as detection_service
from backend.services import jobs as job_service
from backend.services import pipelines, records as record_service, review as review_service
from backend.services import projects as project_service

bp = Blueprint("records", __name__)


def _user() -> dict:
    user = auth.current_user()
    assert user is not None
    return user


def _assert_project(conn, project_id: str) -> None:
    """别人的项目一律 404：列表不能拿空结果冒充“这里没有记录”。"""
    project_service.get_owned(conn, project_id, int(_user()["id"]))


def _key(payload: dict, prefix: str) -> str:
    """请求键：调用方给了就复用（重试幂等），没给就本次新建一条。"""
    raw = str(payload.get("request_key") or "").strip()
    if raw:
        return raw[:120]
    return f"{prefix}:{security.new_token(12)}"


@bp.get("/projects/<project_id>/detections")
@auth.login_required
def list_records(project_id: str):
    conn = db.get_db()
    _assert_project(conn, project_id)
    has_hits = request.args.get("has_hits")
    result = detection_service.list_for_project(
        conn,
        int(_user()["id"]),
        project_id,
        status=(request.args.get("status") or "").strip() or None,
        model_id=(request.args.get("model_id") or "").strip() or None,
        session_id=(request.args.get("session_id") or "").strip() or None,
        has_hits=(None if has_hits is None else has_hits.lower() in {"1", "true", "yes"}),
        date_from=(request.args.get("date_from") or "").strip() or None,
        date_to=(request.args.get("date_to") or "").strip() or None,
        limit=int(request.args.get("limit") or 20),
        before=(request.args.get("before") or "").strip() or None,
        deleted=(request.args.get("deleted") or "exclude").strip(),
        q=(request.args.get("q") or "").strip() or None,
    )
    reviews = aireview.latest_for_records(
        conn,
        int(_user()["id"]),
        project_id,
        [item["id"] for item in result["records"]],
    )
    statuses = review_service.statuses_for_records(
        conn, int(_user()["id"]), [item["id"] for item in result["records"]]
    )
    feedback_statuses = review_service.feedback_statuses_for_records(
        conn, int(_user()["id"]), [item["id"] for item in result["records"]]
    )
    for item in result["records"]:
        item["latest_review"] = reviews.get(item["id"])
        item["human_status"] = statuses.get(item["id"], {}).get("human_status", "pending")
        item["review_version"] = statuses.get(item["id"], {}).get("review_version")
        # 按当前人工确认版本的反馈状态（未入池/已入池/已回流），页面与 Agent 共用。
        item["feedback_status"] = feedback_statuses.get(item["id"], "none")
    # 主模态来自记录所用模型绑定的数据集：单 IR 记录的原图就是 IR。
    detection_service.attach_primary_modality(
        conn, int(_user()["id"]), project_id, result["records"]
    )
    return ok(result)


@bp.get("/projects/<project_id>/detections/<record_id>")
@auth.login_required
def get_record(project_id: str, record_id: str):
    conn = db.get_db()
    row = detection_service.get_owned(conn, int(_user()["id"]), project_id, record_id)
    data = detection_service.public_with_labels(conn, int(_user()["id"]), project_id, row)
    data["primary_modality"] = detection_service.primary_modality_of_record(
        conn, int(_user()["id"]), project_id, row
    )
    data["latest_review"] = aireview.latest_for_records(
        conn, int(_user()["id"]), project_id, [record_id]
    ).get(record_id)
    # 深链/详情走同一条授权读取：人工状态与结论版本与列表页同一口径，
    # 不能按 ID 打开后把"已确认"显示成"待人工"。
    statuses = review_service.statuses_for_records(
        conn, int(_user()["id"]), [str(record_id)]
    )
    data["human_status"] = statuses.get(str(record_id), {}).get("human_status", "pending")
    data["review_version"] = statuses.get(str(record_id), {}).get("review_version")
    data["feedback_status"] = review_service.feedback_statuses_for_records(
        conn, int(_user()["id"]), [str(record_id)]
    ).get(str(record_id), "none")
    return ok({"record": data})


@bp.get("/projects/<project_id>/detections/<record_id>/result")
@auth.login_required
def get_result_image(project_id: str, record_id: str):
    conn = db.get_db()
    path = detection_service.result_path(conn, int(_user()["id"]), project_id, record_id)
    return send_file(path, mimetype="image/jpeg", max_age=0)


@bp.post("/projects/<project_id>/detections")
@auth.login_required
def detect_one(project_id: str):
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    record = detection_service.run_detection(
        conn,
        int(_user()["id"]),
        project_id,
        model_id=str(payload.get("model_id") or ""),
        rgb_asset_id=str(payload.get("rgb_asset_id") or ""),
        x_asset_id=payload.get("x_asset_id"),
        confidence=float(payload.get("confidence") or 0.25),
        request_key=_key(payload, "det"),
        session_id=payload.get("session_id"),
    )
    return ok({"record": record})


@bp.post("/projects/<project_id>/detections/batch")
@auth.login_required
def detect_batch(project_id: str):
    payload = request.get_json(silent=True) or {}
    items = payload.get("items")
    if not isinstance(items, list):
        raise ApiError(400, "VALIDATION", "items 必须是数组")
    conn = db.get_db()
    job, created = pipelines.start_detection_batch(
        conn,
        int(_user()["id"]),
        project_id,
        model_id=str(payload.get("model_id") or ""),
        items=items,
        request_key=_key(payload, "batch"),
        confidence=float(payload.get("confidence") or 0.25),
        session_id=payload.get("session_id"),
    )
    return ok({"job": job, "created": created})


@bp.post("/projects/<project_id>/detections/review")
@auth.login_required
def review_records(project_id: str):
    payload = request.get_json(silent=True) or {}
    record_ids = payload.get("record_ids")
    if not isinstance(record_ids, list) or not record_ids:
        raise ApiError(400, "VALIDATION", "需要 record_ids（可只选一条，也可多选）")
    conn = db.get_db()
    job, created = aireview.start_batch(
        conn,
        int(_user()["id"]),
        project_id,
        record_ids=[str(item) for item in record_ids],
        request_key=_key(payload, "review"),
        session_id=payload.get("session_id"),
    )
    return ok({"job": job, "created": created})


@bp.get("/projects/<project_id>/jobs")
@auth.login_required
def list_jobs(project_id: str):
    conn = db.get_db()
    return ok(
        job_service.list_for_project(
            conn,
            int(_user()["id"]),
            project_id,
            session_id=(request.args.get("session_id") or "").strip() or None,
            limit=int(request.args.get("limit") or 20),
        )
    )


@bp.get("/projects/<project_id>/jobs/<job_id>")
@auth.login_required
def get_job(project_id: str, job_id: str):
    conn = db.get_db()
    try:
        row = job_service.get_owned(conn, int(_user()["id"]), project_id, job_id)
    except KeyError:
        raise ApiError(404, "JOB_NOT_FOUND", "作业不存在或不属于当前项目") from None
    return ok({"job": job_service.public(row)})


@bp.get("/projects/<project_id>/job-events")
@auth.login_required
def job_events(project_id: str):
    """会话级事件恢复：轮次结束后仍能拿到业务进度与结果。"""
    conn = db.get_db()
    return ok(
        job_service.events_after(
            conn,
            int(_user()["id"]),
            project_id,
            after=int(request.args.get("after") or 0),
            session_id=(request.args.get("session_id") or "").strip() or None,
        )
    )


# --------------------------------------------------------------------------- 记录操作


def _ids(payload: dict) -> list[str]:
    raw = payload.get("record_ids")
    if not isinstance(raw, list) or not raw:
        raise ApiError(400, "VALIDATION", "需要 record_ids")
    return [str(item) for item in raw]


@bp.post("/projects/<project_id>/detections/delete")
@auth.login_required
def delete_records(project_id: str):
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    return ok(record_service.soft_delete(conn, int(_user()["id"]), project_id, _ids(payload)))


@bp.post("/projects/<project_id>/detections/restore")
@auth.login_required
def restore_records(project_id: str):
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    return ok(record_service.restore(conn, int(_user()["id"]), project_id, _ids(payload)))


@bp.post("/projects/<project_id>/detections/<record_id>/feedback")
@auth.login_required
def add_feedback(project_id: str, record_id: str):
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    feedback, created = record_service.add_feedback(
        conn,
        int(_user()["id"]),
        project_id,
        record_id,
        note=str(payload.get("note") or ""),
        request_key=_key(payload, "fbk"),
    )
    return ok({"feedback": feedback, "created": created})


@bp.get("/projects/<project_id>/feedback")
@auth.login_required
def list_feedback(project_id: str):
    conn = db.get_db()
    _assert_project(conn, project_id)
    return ok(
        record_service.list_feedback(
            conn,
            int(_user()["id"]),
            project_id,
            status=(request.args.get("status") or "pending").strip(),
            limit=int(request.args.get("limit") or 50),
        )
    )


@bp.get("/projects/<project_id>/detections/export")
@auth.login_required
def export_records(project_id: str):
    """CSV 导出：必须显式给 scope=page|selected，不做"整项目一把梭"。"""
    from flask import send_file

    conn = db.get_db()
    scope = (request.args.get("scope") or "").strip()
    ids = [item for item in (request.args.get("ids") or "").split(",") if item.strip()]
    has_hits = request.args.get("has_hits")
    meta = export_service.records_csv(
        conn,
        int(_user()["id"]),
        project_id,
        scope=scope,
        ids=ids,
        limit=int(request.args.get("limit") or 20),
        filters={
            "status": (request.args.get("status") or "").strip(),
            "model_id": (request.args.get("model_id") or "").strip(),
            "session_id": (request.args.get("session_id") or "").strip(),
            "has_hits": (None if has_hits is None else has_hits.lower() in {"1", "true", "yes"}),
            "date_from": (request.args.get("date_from") or "").strip(),
            "date_to": (request.args.get("date_to") or "").strip(),
            "deleted": (request.args.get("deleted") or "exclude").strip(),
            "q": (request.args.get("q") or "").strip(),
        },
    )
    from backend.services import storage

    root = storage.project_root(conn, int(_user()["id"]), project_id)
    path = storage.resolve_rel(root, meta["relative"], must_exist=True)
    return send_file(path, mimetype="text/csv", as_attachment=True, download_name=meta["filename"], max_age=0)

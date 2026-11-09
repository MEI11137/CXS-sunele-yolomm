"""训练接口：计划校验、启动、状态、日志、取消、父子对比。

与 Agent 工具调用**同一个服务**（``backend.services.training``），页面不另写一套逻辑：
参数校验、抢锁顺序、取消的进程校验、重启核对都只有一份实现。
"""

from __future__ import annotations

from flask import Blueprint, request

from backend import auth, db, security
from backend.errors import ApiError, ok
from backend.services import projects as project_service
from backend.services import training as training_service

bp = Blueprint("training", __name__)


def _user() -> dict:
    user = auth.current_user()
    assert user is not None
    return user


@bp.get("/projects/<project_id>/training")
@auth.login_required
def list_runs(project_id: str):
    conn = db.get_db()
    project_service.get_owned(conn, project_id, int(_user()["id"]))
    limit = request.args.get("limit", "30")
    try:
        size = int(limit)
    except ValueError:
        raise ApiError(400, "BAD_LIMIT", "limit 必须是整数") from None
    return ok(training_service.list_for_project(conn, int(_user()["id"]), project_id, limit=size))


@bp.get("/projects/<project_id>/training/device")
@auth.login_required
def device_capabilities(project_id: str):
    """训练设备真实能力：UI 用来生成设备选项与不可用原因，不写死 CPU。"""
    return ok(training_service.device_capabilities())


@bp.post("/projects/<project_id>/training/plan")
@auth.login_required
def plan(project_id: str):
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    return ok(
        training_service.plan(
            conn,
            int(_user()["id"]),
            project_id,
            dataset_id=str(payload.get("dataset_id") or ""),
            params=payload.get("params"),
            parent_model_id=payload.get("parent_model_id") or None,
            name=payload.get("name"),
        )
    )


@bp.post("/projects/<project_id>/training")
@auth.login_required
def start(project_id: str):
    payload = request.get_json(silent=True) or {}
    conn = db.get_db()
    run, created = training_service.start(
        conn,
        int(_user()["id"]),
        project_id,
        dataset_id=str(payload.get("dataset_id") or ""),
        request_key=str(payload.get("request_key") or security.new_token(6)),
        name=payload.get("name"),
        params=payload.get("params"),
        parent_model_id=payload.get("parent_model_id") or None,
        session_id=payload.get("session_id") or None,
        turn_id=payload.get("turn_id") or None,
    )
    return ok({"run": run, "created": created})


@bp.get("/projects/<project_id>/training/<run_id>")
@auth.login_required
def get_run(project_id: str, run_id: str):
    conn = db.get_db()
    row = training_service.get_owned(conn, int(_user()["id"]), project_id, run_id)
    return ok({"run": training_service.public(row)})


@bp.get("/projects/<project_id>/training/<run_id>/log")
@auth.login_required
def get_log(project_id: str, run_id: str):
    conn = db.get_db()
    try:
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        raise ApiError(400, "BAD_OFFSET", "offset 必须是整数") from None
    return ok(
        training_service.log_tail(conn, int(_user()["id"]), project_id, run_id, offset=offset)
    )


@bp.get("/projects/<project_id>/training/<run_id>/compare")
@auth.login_required
def compare(project_id: str, run_id: str):
    conn = db.get_db()
    return ok(training_service.compare(conn, int(_user()["id"]), project_id, run_id))


@bp.post("/projects/<project_id>/training/<run_id>/cancel")
@auth.login_required
def cancel(project_id: str, run_id: str):
    conn = db.get_db()
    return ok(training_service.cancel(conn, int(_user()["id"]), project_id, run_id))

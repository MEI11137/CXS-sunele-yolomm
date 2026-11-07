"""外部 MCP 接口：配置、发现、逐项授权、调用与审计。

所有路径先过 ``mcp.get_owned``（项目 + 所有者），没有"按 ID 直取"的入口；
配置与凭据只在**用户本人**的请求里写，模型侧没有任何写入口。
"""

from __future__ import annotations

from flask import Blueprint, request

from backend import auth, db
from backend.errors import ok
from backend.services import mcp as mcp_service

bp = Blueprint("mcp", __name__)


def _user() -> dict:
    user = auth.current_user()
    assert user is not None
    return user


def _payload() -> dict:
    return request.get_json(silent=True) or {}


@bp.get("/projects/<project_id>/mcp")
@auth.login_required
def list_servers(project_id: str):
    conn = db.get_db()
    return ok(mcp_service.list_servers(conn, int(_user()["id"]), project_id))


@bp.post("/projects/<project_id>/mcp")
@auth.login_required
def create_server(project_id: str):
    body = _payload()
    conn = db.get_db()
    return ok(
        {
            "server": mcp_service.create(
                conn,
                int(_user()["id"]),
                project_id,
                name=str(body.get("name") or ""),
                transport=str(body.get("transport") or ""),
                command=str(body.get("command") or ""),
                args=body.get("args") or [],
                cwd_rel=str(body.get("cwd_rel") or ""),
                url=str(body.get("url") or ""),
                env=body.get("env"),
                headers=body.get("headers"),
                enabled=bool(body.get("enabled")),
            )
        }
    )


@bp.patch("/projects/<project_id>/mcp/<server_id>")
@auth.login_required
def update_server(project_id: str, server_id: str):
    body = _payload()
    conn = db.get_db()
    return ok(
        {
            "server": mcp_service.update(
                conn,
                int(_user()["id"]),
                project_id,
                server_id,
                name=body.get("name"),
                command=body.get("command"),
                args=body.get("args"),
                cwd_rel=body.get("cwd_rel"),
                url=body.get("url"),
                env=body.get("env"),
                headers=body.get("headers"),
                enabled=body.get("enabled"),
            )
        }
    )


@bp.delete("/projects/<project_id>/mcp/<server_id>")
@auth.login_required
def delete_server(project_id: str, server_id: str):
    conn = db.get_db()
    return ok(mcp_service.delete(conn, int(_user()["id"]), project_id, server_id))


@bp.post("/projects/<project_id>/mcp/<server_id>/discover")
@auth.login_required
def discover(project_id: str, server_id: str):
    conn = db.get_db()
    return ok(mcp_service.discover(conn, int(_user()["id"]), project_id, server_id))


@bp.post("/projects/<project_id>/mcp/<server_id>/authorize")
@auth.login_required
def authorize(project_id: str, server_id: str):
    body = _payload()
    conn = db.get_db()
    return ok(
        mcp_service.authorize(
            conn,
            int(_user()["id"]),
            project_id,
            server_id,
            str(body.get("tool") or ""),
            str(body.get("decision") or ""),
        )
    )


@bp.post("/projects/<project_id>/mcp/<server_id>/call")
@auth.login_required
def call_tool(project_id: str, server_id: str):
    body = _payload()
    conn = db.get_db()
    return ok(
        mcp_service.call(
            conn,
            int(_user()["id"]),
            project_id,
            server_id,
            tool_name=str(body.get("tool") or ""),
            arguments=body.get("arguments") or {},
            request_key=(str(body.get("request_key")) if body.get("request_key") else None),
        )
    )


@bp.get("/projects/<project_id>/mcp/<server_id>/calls")
@auth.login_required
def recent_calls(project_id: str, server_id: str):
    conn = db.get_db()
    return ok(mcp_service.recent_calls(conn, int(_user()["id"]), project_id, server_id))

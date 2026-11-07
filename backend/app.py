"""v1.7 后端应用工厂。

约定：

* 配置（尤其是数据目录）在工厂阶段一次性确定，之后请求只读 ``app.config["HX17"]``；
* 密钥文件在工厂阶段加载，缺失时生成，与 SQLite 分文件保管；
* 生产同源：由后端托管 ``frontend/dist``，``/api`` 之外的深层链接回落到 index.html。
"""

from __future__ import annotations

import atexit
import sys
from pathlib import Path

# 允许 `python backend/app.py` 直接运行，同时保持 `backend` 作为包名导入。
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from flask import Flask, jsonify, request  # noqa: E402
from werkzeug.exceptions import HTTPException  # noqa: E402

from backend import auth, config, db, paths, security  # noqa: E402
from backend.agent import store as turn_store  # noqa: E402
from backend.agent.runtime import AgentRuntime  # noqa: E402
from backend.errors import ApiError  # noqa: E402
from backend.routes.admin import bp as admin_bp  # noqa: E402
from backend.routes.agent import bp as agent_bp  # noqa: E402
from backend.routes.assets import bp as assets_bp  # noqa: E402
from backend.routes.auth import bp as auth_bp  # noqa: E402
from backend.routes.connections import bp as connections_bp  # noqa: E402
from backend.routes.datasets import bp as datasets_bp  # noqa: E402
from backend.routes.directories import bp as directories_bp  # noqa: E402
from backend.routes.documents import bp as documents_bp  # noqa: E402
from backend.routes.health import bp as health_bp  # noqa: E402
from backend.routes.meta import bp as meta_bp  # noqa: E402
from backend.routes.models import bp as models_bp  # noqa: E402
from backend.routes.projects import bp as projects_bp  # noqa: E402
from backend.routes.records import bp as records_bp  # noqa: E402
from backend.routes.reviews import bp as reviews_bp  # noqa: E402
from backend.routes.sessions import bp as sessions_bp  # noqa: E402
from backend.routes.service import bp as service_bp  # noqa: E402
from backend.routes.mcp import bp as mcp_bp  # noqa: E402
from backend.routes.training import bp as training_bp  # noqa: E402
from backend.services import aireview as aireview_service  # noqa: E402
from backend.services import jobs as job_service  # noqa: E402
from backend.services import manuals as manual_service  # noqa: E402
from backend.services import mcp as mcp_service  # noqa: E402
from backend.services import training as training_service  # noqa: E402
from backend.vision import runtime as vision_runtime  # noqa: E402

DIST_DIR = _ROOT / "frontend" / "dist"

_HTTP_MESSAGES = {
    400: "请求参数有误",
    401: "请先登录",
    403: "没有权限执行该操作",
    404: "接口不存在",
    405: "请求方法不被允许",
    409: "当前状态不允许该操作",
    413: "请求体过大",
    415: "请求体必须是 application/json",
    429: "请求过于频繁，请稍后再试",
}


def create_app(
    settings: config.Settings | None = None,
    *,
    serve_dist: bool | None = None,
    data_dir: str | Path | None = None,
    engine_spawner=None,
) -> Flask:
    if settings is None:
        settings = config.load_settings(data_dir=data_dir, serve_dist=serve_dist)

    app = Flask(__name__, static_folder=None)
    app.config["HX17"] = settings
    # 主密钥在工厂阶段固定：运行期换环境变量不会换密钥，也不会换库。
    app.config["HX17_SESSION_KEY"] = security.load_or_create_key(settings.secret_dir / "session.key")
    app.config["HX17_CONNECTION_KEY"] = security.load_or_create_key(
        settings.secret_dir / "connection.key"
    )
    app.json.ensure_ascii = False

    db.init_app(app)
    auth.init_app(app)

    # 上次进程退出时没跑完的轮次：明确标成“中断”，不伪装完成，也不在启动时偷偷重跑。
    conn = db.connect(settings.db_path)
    try:
        turn_store.interrupted(
            conn,
            code="SERVER_RESTART",
            message="服务端重启，本轮已中断，没有生成最终回答；请重新发送。",
        )
        # 业务作业与 AI 核查同理：未完成的一律标中断/结果未知。
        # 尤其**不能自动重发**核查——那次请求可能已经计费。
        aireview_service.interrupted(conn)
        job_service.interrupted(conn)
    finally:
        conn.close()

    # 业务后台作业与视觉运行时都只吃工厂阶段的配置快照，请求期间不再读环境变量。
    job_service.configure(settings)
    vision_runtime.configure(settings)
    # 训练槽与在跑的训练进程：只核对真实进程/产物，绝不自动重启训练。
    conn = db.connect(settings.db_path)
    try:
        training_service.recover(conn)
        # 内置手册按稳定 ID 幂等登记；不覆盖用户改过的版本。
        manual_service.seed_builtin(conn)
    finally:
        conn.close()
    # 服务端私有区在工厂阶段就登记：业务文件服务与文件工具共用同一份规则，
    # 不依赖“先起过一次 Agent 网关”这种时序巧合。
    paths.configure_private([settings.data_dir, settings.secret_dir])
    mcp_service.configure(settings.secret_dir)

    runtime = AgentRuntime(settings, spawner=engine_spawner)
    runtime.start()
    app.config["HX17_AGENT"] = runtime
    atexit.register(runtime.shutdown)

    for blueprint in (
        health_bp,
        meta_bp,
        auth_bp,
        admin_bp,
        projects_bp,
        sessions_bp,
        directories_bp,
        connections_bp,
        assets_bp,
        datasets_bp,
        models_bp,
        documents_bp,
        records_bp,
        reviews_bp,
        training_bp,
        mcp_bp,
        agent_bp,
        # 本机启动/停止脚本专用；没有 HX17_SERVICE_TOKEN 时一律 404。
        service_bp,
    ):
        app.register_blueprint(blueprint, url_prefix="/api")

    @app.errorhandler(ApiError)
    def _handle_api_error(error: ApiError):
        return error.to_response()

    @app.errorhandler(HTTPException)
    def _handle_http_error(error: HTTPException):
        status = error.code or 500
        if request.path.startswith("/api/"):
            return (
                jsonify(
                    {
                        "code": status,
                        "msg": _HTTP_MESSAGES.get(status, error.description),
                        "error": f"HTTP_{status}",
                        "data": None,
                    }
                ),
                status,
            )
        return error

    @app.errorhandler(Exception)
    def _handle_unexpected(error: Exception):
        if isinstance(error, HTTPException):
            return _handle_http_error(error)
        app.logger.exception("未处理的异常：%s", error)
        return (
            jsonify(
                {"code": 500, "msg": "服务器内部错误", "error": "INTERNAL", "data": None}
            ),
            500,
        )

    if settings.serve_dist:
        _install_spa(app, DIST_DIR)

    return app


def _install_spa(app: Flask, dist: Path) -> None:
    """托管已构建的前端；/api 之外的路径回落到 index.html。"""
    from flask import abort, send_from_directory

    index = dist / "index.html"
    if not index.is_file():
        # 构建产物缺失时不静默降级成空白页，直接让启动报错更早暴露。
        raise SystemExit(f"未找到前端构建产物：{index}")

    @app.get("/", endpoint="spa_root")
    def _root():
        return send_from_directory(dist, "index.html")

    @app.get("/<path:asset>", endpoint="spa_asset")
    def _asset(asset: str):
        if asset == "api" or asset.startswith("api/"):
            abort(404)
        target = (dist / asset).resolve()
        if not target.is_relative_to(dist.resolve()):
            abort(404)
        if target.is_file():
            return send_from_directory(dist, asset)
        if Path(asset).suffix:
            abort(404)
        return send_from_directory(dist, "index.html")


def main() -> int:
    settings = config.load_settings()
    app = create_app(settings)
    print(f"API READY: http://127.0.0.1:{settings.port}/api/health  (data={settings.data_dir})")
    app.run(host="127.0.0.1", port=settings.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

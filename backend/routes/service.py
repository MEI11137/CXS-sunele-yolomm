"""本机服务管理接口（仅 deploy/serve.py 启动时可用）。

用途只有一个：让**启动/停止脚本**验证“占着这个端口的确实是我自己记录的那个实例”，
然后请它自己退出。它不是业务接口，也不给页面用。

防护范围（如实，别把它当成面向公网的认证方案）：

* **真正的边界是随机令牌**：进程启动时生成 128 位随机令牌，只写进
  ``<data_dir>/run/server.json``（已忽略入库、权限位 0600），请求必须带
  ``X-HX17-Service-Token``，比较用 ``hmac.compare_digest``；
* 没有 ``HX17_SERVICE_TOKEN`` 时整个蓝图像不存在（404），不是“401 提示你去登录”；
* 额外两道筛子：只接受本机回环 ``remote_addr``；带常见转发头
  （``Forwarded`` / ``X-Forwarded-*`` / ``X-Real-IP`` / ``CF-Connecting-IP`` 等）
  的请求一律拒绝——nginx、Cloudflare 隧道这类反代会默认加这些头，因此它们转发不进来。

  但 ``remote_addr`` 本身分不清“本机浏览器”和“本机反代”：同机反代如果主动剥掉
  上述头，看起来就和本机调用一样。所以这里**不宣称“套了反代也一定拒绝”**，
  正确做法是不把管理端口暴露到公网（默认只监听 127.0.0.1）。

返回值不含数据目录以外的内部配置；``instance`` 是令牌的哈希前缀，脚本用它核对
“这个进程是不是我上次启动的那个”，不必把原始令牌写进日志。令牌不会出现在
``/api/health``、``/api/meta`` 或任何页面响应里。
"""

from __future__ import annotations

import hashlib
import hmac
import os

from flask import Blueprint, current_app, request

from backend.errors import ApiError

bp = Blueprint("service", __name__)

TOKEN_HEADER = "X-HX17-Service-Token"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

# 反代/隧道默认会加的头。出现任意一个就说明这个请求是“被转发来的”，不接。
_FORWARD_HEADERS = (
    "Forwarded",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Proto",
    "X-Forwarded-Port",
    "X-Real-IP",
    "X-Client-IP",
    "True-Client-IP",
    "CF-Connecting-IP",
    "CF-Ray",
)


def _token() -> str:
    return (os.environ.get("HX17_SERVICE_TOKEN") or "").strip()


def instance_fingerprint(token: str) -> str:
    """令牌的短指纹：用于日志与状态文件比对，不暴露令牌本身。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _guard() -> str:
    expected = _token()
    if not expected:
        raise ApiError(404, "NOT_FOUND", "接口不存在")
    for header in _FORWARD_HEADERS:
        if request.headers.get(header):
            raise ApiError(
                403,
                "SERVICE_PROXY_REJECTED",
                "服务管理接口不接受经反向代理转发的请求",
            )
    if (request.remote_addr or "") not in _LOOPBACK:
        raise ApiError(403, "SERVICE_LOCAL_ONLY", "只允许本机访问服务管理接口")
    supplied = (request.headers.get(TOKEN_HEADER) or "").strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise ApiError(403, "SERVICE_TOKEN_INVALID", "本机服务令牌不正确")
    return expected


@bp.get("/service/status")
def service_status():
    token = _guard()
    settings = current_app.config["HX17"]
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "status": "ok",
            "pid": os.getpid(),
            "instance": instance_fingerprint(token),
            "port": int(settings.port),
            "version": settings.version,
            "data_dir": str(settings.data_dir),
        },
    }


@bp.post("/service/shutdown")
def service_shutdown():
    _guard()
    hook = current_app.config.get("HX17_STOP")
    if not callable(hook):
        raise ApiError(409, "NO_STOP_HOOK", "当前进程不是由启动脚本拉起的，不能远程停止")
    hook()
    return {"code": 0, "msg": "ok", "data": {"stopping": True}}

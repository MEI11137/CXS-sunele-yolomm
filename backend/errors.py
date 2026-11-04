"""统一的 API 响应与错误封装。

约定（前后端都依赖）：

* 成功：``{"code": 0, "msg": "ok", "data": ...}``
* 失败：``{"code": <HTTP 状态>, "msg": <给人看的话>, "error": <机器码>, "data": <细节>}``

``code`` 与 HTTP 状态一致，前端 ``http.ts`` 只判 ``code === 0``；
``error`` 是稳定的字符串机器码，用来做分支判断，不随文案变化。
"""

from __future__ import annotations

from typing import Any

from flask import jsonify


class ApiError(Exception):
    """业务错误。抛出后由应用工厂注册的处理器转成统一响应。"""

    def __init__(self, status: int, code: str, msg: str, details: Any = None) -> None:
        super().__init__(msg)
        self.status = int(status)
        self.code = code
        self.msg = msg
        self.details = details

    def to_response(self):
        return (
            jsonify({"code": self.status, "msg": self.msg, "error": self.code, "data": self.details}),
            self.status,
        )


def ok(data: Any = None, msg: str = "ok"):
    return jsonify({"code": 0, "msg": msg, "data": data})


def bad_request(msg: str, code: str = "VALIDATION", details: Any = None) -> ApiError:
    return ApiError(400, code, msg, details)


def unauthorized(msg: str = "请先登录", code: str = "AUTH_REQUIRED") -> ApiError:
    return ApiError(401, code, msg)


def forbidden(msg: str = "没有权限执行该操作", code: str = "FORBIDDEN") -> ApiError:
    return ApiError(403, code, msg)


def not_found(what: str = "资源") -> ApiError:
    # 统一说“不存在或无权访问”，避免通过 403/404 差异探测他人资源是否存在。
    return ApiError(404, "NOT_FOUND", f"{what}不存在或无权访问")


def conflict(msg: str, code: str = "CONFLICT") -> ApiError:
    return ApiError(409, code, msg)

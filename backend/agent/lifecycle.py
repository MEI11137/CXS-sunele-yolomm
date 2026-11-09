"""项目生命周期动作：目录绑定变更后终止仍在使用旧目录的轮次。

页面路由与 Agent 工具共用同一套处理，避免一条路径停旧引擎、另一条只改数据库。
运行时对象由调用方显式传入：路由从 ``current_app.config["HX17_AGENT"]`` 取，
工具从 ``ToolContext.runtime`` 取；没有运行时（例如只读单测）就不能假装已处理。

返回值是 ``runtime.invalidate_project`` 报告的中断记录数；
它表示“运行时已接受并标记中断”，**不等于**已经确认引擎进程退出。
"""

from __future__ import annotations

from typing import Any


def invalidate_project_turns(
    runtime: Any,
    user_id: int,
    project_id: str,
    *,
    reason: str,
) -> int | None:
    """终止该项目正在运行的轮次；返回中断记录数，没有运行时返回 None。"""
    if runtime is None:
        return None
    count = runtime.invalidate_project(int(user_id), str(project_id), reason=reason)
    try:
        return int(count or 0)
    except (TypeError, ValueError):
        return 0
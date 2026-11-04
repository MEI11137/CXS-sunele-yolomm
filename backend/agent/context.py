"""Agent 运行时的公共数据结构。

这里的 ``Actor`` 是**服务端**根据引擎 token 反查出来的身份，不是模型传进来的参数：
工具调用的所有权限判断都以它为准，模型无法通过 ``user_id`` 之类的字段冒充别人。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class Actor:
    """一次工具调用所代表的真实身份与边界。"""

    user_id: int
    username: str
    project_id: str
    project_name: str
    session_id: str
    turn_id: str
    mode: str
    root_path: Path | None
    connection_fingerprint: str
    provider: str
    model: str

    @property
    def has_root(self) -> bool:
        return self.root_path is not None


class ToolError(Exception):
    """工具层错误：稳定机器码 + 给人看的中文说明。

    不允许把上游正文、完整进程环境或凭据写进 message。
    """

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = int(status)

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message}

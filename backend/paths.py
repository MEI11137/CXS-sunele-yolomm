"""本机目录边界校验。

目标：把“用户填的路径”收敛成服务器上的真实规范路径，并证明它没有越界。

覆盖的情况：

* 相对路径、``~``、带引号的粘贴路径；
* 盘符相对路径（``C:foo``）、设备/扩展前缀（``\\\\?\\``、``\\\\.\\``）；
* ``..`` 穿越（由 realpath 归一化后再做包含判断）；
* 符号链接 / NTFS 联接指向授权范围外（realpath 会解析到最终目标）；
* 不同盘符或 UNC 共享（``os.path.commonpath`` 抛 ValueError 即视为不包含）。

任何校验失败都抛 :class:`backend.errors.ApiError`，错误码稳定，便于前端提示。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from backend.errors import ApiError

_DRIVE_RELATIVE = re.compile(r"^[A-Za-z]:(?![\\/])")
_DEVICE_PREFIXES = ("\\\\.\\", "\\\\?\\", "//./", "//?/")

# 服务端私有根：数据库、密钥、引擎运行目录与日志。
# 业务文件服务（storage）与 Agent 文件工具（fsops）共用这一份列表，
# 避免出现“工具挡住了、业务接口又能写进去”的双重标准。
_PRIVATE_ROOTS: list[Path] = []


def configure_private(roots) -> None:
    """登记服务端私有目录；命中即拒绝，与目录授权范围无关。"""
    resolved: list[Path] = []
    for item in roots or []:
        if not item:
            continue
        try:
            resolved.append(Path(os.path.realpath(str(item))))
        except OSError:
            continue
    _PRIVATE_ROOTS[:] = resolved


def private_roots() -> list[Path]:
    return list(_PRIVATE_ROOTS)


def is_private(path: str | os.PathLike[str]) -> bool:
    """真实路径是否落在任一私有根内（符号链接/联接按真实落点判断）。"""
    if not _PRIVATE_ROOTS:
        return False
    real = Path(os.path.realpath(str(path)))
    return any(is_within(real, root) for root in _PRIVATE_ROOTS)


def clean_input(raw: str | os.PathLike[str] | None) -> str:
    text = str(raw if raw is not None else "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    if "\x00" in text:
        raise ApiError(400, "BAD_PATH", "路径包含非法字符")
    return text


def canonical(path: str | os.PathLike[str]) -> Path:
    """规范路径：展开 ~、转绝对、解析符号链接与联接。"""
    text = os.path.expanduser(str(path))
    return Path(os.path.realpath(os.path.abspath(text)))


def is_within(child: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    child_real = os.path.normcase(os.path.realpath(str(child)))
    parent_real = os.path.normcase(os.path.realpath(str(parent)))
    try:
        return os.path.commonpath([child_real, parent_real]) == parent_real
    except ValueError:
        # 不同盘符 / UNC 与本地路径混用，commonpath 无法比较。
        return False


def _reject_unsupported(text: str) -> None:
    if text.startswith(_DEVICE_PREFIXES):
        raise ApiError(400, "BAD_PATH", "不支持设备路径或 \\\\?\\ 扩展前缀")
    if text.startswith(("\\\\", "//")):
        # UNC 共享的“是否在授权范围内”依赖远程主机解析，无法只凭本地路径证明；
        # 统一要求先映射成本地盘符，避免绕过盘符级授权。
        raise ApiError(
            400, "UNC_NOT_SUPPORTED", "不支持 UNC 网络路径，请先把共享目录映射为本地盘符"
        )
    if _DRIVE_RELATIVE.match(text):
        raise ApiError(400, "BAD_PATH", "请填写完整绝对路径，例如 D:\\\\data\\\\project")


def validate_root(raw: str | os.PathLike[str] | None) -> Path:
    """管理员授权根目录：必须是已存在的绝对目录，且不能是磁盘根。"""
    text = clean_input(raw)
    if not text:
        raise ApiError(400, "BAD_PATH", "请填写目录路径")
    _reject_unsupported(text)
    candidate = Path(text)
    if not candidate.is_absolute():
        raise ApiError(400, "BAD_PATH", "必须是绝对路径")
    real = canonical(candidate)
    if not real.exists():
        raise ApiError(400, "PATH_NOT_FOUND", "目录不存在（远程选择的是服务器上的目录）")
    if not real.is_dir():
        raise ApiError(400, "NOT_A_DIRECTORY", "这里需要目录，不是文件")
    if real.parent == real:
        raise ApiError(400, "TOO_BROAD", "不能把磁盘根目录设为授权范围，请选择具体目录")
    return real


def validate_binding(raw: str | os.PathLike[str] | None, roots: list[Path]) -> Path:
    """项目目录绑定：必须是授权根目录内部（含根目录本身）的真实目录。"""
    text = clean_input(raw)
    if not text:
        raise ApiError(400, "BAD_PATH", "请填写目录路径")
    _reject_unsupported(text)
    candidate = Path(text)
    if not candidate.is_absolute():
        raise ApiError(400, "BAD_PATH", "必须是服务器上的绝对路径")
    real = canonical(candidate)
    if not real.exists():
        raise ApiError(400, "PATH_NOT_FOUND", "目录不存在（远程选择的是服务器上的目录）")
    if not real.is_dir():
        raise ApiError(400, "NOT_A_DIRECTORY", "这里需要目录，不是文件")
    if not roots:
        raise ApiError(409, "NO_ALLOWED_ROOTS", "管理员尚未配置任何允许的根目录")
    for root in roots:
        if is_within(real, root):
            return real
    raise ApiError(403, "OUTSIDE_ALLOWED_ROOTS", "该目录不在管理员允许的根目录范围内")


def list_entries(base: str | os.PathLike[str], target: str | os.PathLike[str]) -> dict:
    """列出 base 内部的目录内容；指向外部的符号链接/联接不显示，只计数。"""
    base_path = canonical(base)
    target_path = canonical(target)
    if not is_within(target_path, base_path):
        raise ApiError(403, "OUTSIDE_ALLOWED_ROOTS", "只能浏览已授权目录内的内容")
    if not target_path.exists():
        raise ApiError(404, "NOT_FOUND", "目录不存在")
    if not target_path.is_dir():
        raise ApiError(400, "NOT_A_DIRECTORY", "目标不是目录")

    entries: list[dict] = []
    blocked = 0
    try:
        children = list(target_path.iterdir())
    except PermissionError:
        raise ApiError(403, "PATH_DENIED", "没有权限读取该目录") from None
    except OSError:
        raise ApiError(400, "PATH_UNREADABLE", "目录无法读取") from None

    for child in children:
        real = canonical(child)
        if not is_within(real, base_path):
            # 链接或联接指向授权范围之外：不展示，也不允许点进去。
            blocked += 1
            continue
        try:
            is_dir = real.is_dir()
            size = 0 if is_dir else real.stat().st_size
        except OSError:
            is_dir, size = False, 0
        # 指向范围内的链接仍然标出来，让用户知道看到的是另一处目录。
        is_link = os.path.normcase(str(real)) != os.path.normcase(os.path.abspath(str(child)))
        entries.append(
            {
                "name": child.name,
                "path": str(real),
                "is_dir": is_dir,
                "size": size,
                "is_link": is_link,
            }
        )
    entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
    return {"path": str(target_path), "entries": entries, "blocked": blocked}

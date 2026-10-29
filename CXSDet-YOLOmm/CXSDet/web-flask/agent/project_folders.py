"""本机管理员显式选择的项目目录；不把路径授权交给模型或项目说明。"""
from pathlib import Path
import os
import re

from flask import current_app

from agent.errors import AgentError, Code


def require_local_admin(actor):
    if not actor.is_admin:
        raise AgentError(Code.FORBIDDEN, "本机文件夹功能仅限管理员使用", http_status=403)


def directory(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise AgentError(Code.VALIDATION, "请提供本机文件夹的绝对路径")
    path = Path(value.strip())
    if not path.is_absolute() or (os.name == "nt" and (str(path).startswith("\\\\") or ":" in str(path)[2:])):
        raise AgentError(Code.VALIDATION, "请选择本机磁盘上的绝对路径，不支持网络共享或设备路径")
    try:
        real = path.resolve(strict=True)
        if not real.is_dir():
            raise OSError()
    except (OSError, ValueError):
        raise AgentError(Code.VALIDATION, "文件夹不存在或无法访问") from None
    return real


def _protected(path, *, include_ancestors=False):
    # 不允许把系统、账号凭据、本站代码/数据库/引擎私有状态交给项目文件工具。
    blocked = [Path(__file__).resolve().parents[1], Path.home() / ".ssh",
               Path.home() / ".codex", Path.home() / ".aws", Path.home() / ".agents"]
    for key in ("WINDIR", "ProgramFiles", "ProgramFiles(x86)", "APPDATA", "LOCALAPPDATA"):
        if os.environ.get(key):
            blocked.append(Path(os.environ[key]))
    for key in ("DATABASE", "TEMP_DIR", "FILE_STORE_PATH", "OPENCODE_STATE_DIR"):
        value = current_app.config.get(key)
        if value:
            blocked.append(Path(value))
    blocked = [item.resolve() for item in blocked]
    return any(path == item or path.is_relative_to(item)
               or (include_ancestors and item.is_relative_to(path)) for item in blocked)


def validate_root(actor, value, conn=None, project_id=None):
    require_local_admin(actor)
    path = directory(value)
    if path == Path(path.anchor) or path == Path.home().resolve() or _protected(path, include_ancestors=True):
        raise AgentError(Code.VALIDATION, "请选择独立的项目文件夹，不能使用磁盘根目录、用户根目录或受保护目录")
    backend = Path(__file__).resolve().parents[1]
    if backend.is_relative_to(path):
        raise AgentError(Code.VALIDATION, "项目目录不能包含本站后端代码和私有配置")
    if conn is not None:
        for row in conn.execute("SELECT id, root_directory FROM agent_project WHERE root_directory IS NOT NULL"):
            if row[0] == project_id:
                continue
            other = Path(row[1])
            if path == other or path.is_relative_to(other) or other.is_relative_to(path):
                raise AgentError(Code.VALIDATION, "此目录与另一个工作区重叠，请选择独立文件夹")
    return str(path)


def checked_project_root(actor, project):
    value = project.get("root_directory")
    if not value:
        return None
    result = validate_root(actor, value)
    if result != value:
        raise AgentError(Code.VALIDATION, "项目目录的实际位置已改变，请恢复原目录后再继续")
    return Path(result)


def browse(actor, value=""):
    require_local_admin(actor)
    if not value:
        drives = [Path(f"{letter}:/") for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"] if os.name == "nt" else [Path("/")]
        return {"path": "", "parent": None, "directories": [
            {"name": str(path), "path": str(path)} for path in drives if path.is_dir()]}
    path = directory(value)
    if _protected(path):
        raise AgentError(Code.FORBIDDEN, "此目录受保护，不能浏览", http_status=403)
    try:
        children = []
        for child in path.iterdir():
            try:
                # 不沿符号链接/Windows junction 进入其他位置。
                if child.is_dir() and child.resolve() == child.absolute() and not _protected(child):
                    children.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
        children.sort(key=lambda item: item["name"].casefold())
    except OSError:
        raise AgentError(Code.VALIDATION, "无法读取文件夹，请检查系统访问权限") from None
    return {"path": str(path), "parent": str(path.parent) if path.parent != path else "",
            "directories": children}


def make_directory(actor, parent, name):
    require_local_admin(actor)
    if (not isinstance(name, str) or not name.strip() or len(name) > 120
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((".", " "))
            or name in {".", ".."} or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I)):
        raise AgentError(Code.VALIDATION, "文件夹名称无效")
    parent_path = directory(parent)
    if _protected(parent_path):
        raise AgentError(Code.FORBIDDEN, "不能在受保护目录新建文件夹", http_status=403)
    target = parent_path / name
    try:
        target.mkdir()  # 不覆盖现有文件或目录，也不隐式创建上级目录。
    except OSError:
        raise AgentError(Code.VALIDATION, "新建失败：目录可能已存在或没有写入权限") from None
    return {"path": str(target.resolve())}

"""项目工作区布局与路径约束。

业务产物一律落在**项目绑定目录**里，按用途分目录：

``uploads/``    用户上传（原样保存原始字节）
``artifacts/``  检测结果图、可视化等派生文件
``datasets/``   导入后的数据集副本（含生成的 data.yaml）
``.cache/``     可重建的中间件（edge 派生图、缩略图）

服务端私有区（数据库、密钥、引擎存储）永远不在这里：那些在 ``var/`` 下，
由 :mod:`backend.paths` 的私有根列表挡住（与文件工具共用同一份规则）。

**所有写盘动作都必须经过本模块**，因为这里统一做两件事：

1. 用 ``realpath`` 解析**真实目标**，再判断它是否仍在项目根内。只比较字符串
   路径是不够的：``uploads`` 若是一个 NTFS 联接/符号链接，``uploads/x.png``
   在字符串上“在项目内”，真实落点却在项目外。校验必须在 ``mkdir`` 与写入
   **之前**发生，否则拒绝时副作用已经产生了。
2. 拒绝落在服务端私有区（``data_dir`` / ``secrets``）内的目标，避免业务接口
   绕开文件工具的私有目录保护。

创建、写入、复制、失败清理各自都有独立入口（``project_dir`` / ``write_bytes``
/ ``copy_into`` / ``remove_tree``），不做“先写后验”的事后检查。
"""

from __future__ import annotations

import io
import os
import re
import shutil
import time
import unicodedata
from pathlib import Path

from backend import paths, security
from backend.errors import ApiError
from backend.services import projects as project_service

UPLOADS = "uploads"
ARTIFACTS = "artifacts"
DATASETS = "datasets"
CACHE = ".cache"

_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class StorageError(ApiError):
    pass


def safe_name(raw: str, *, fallback: str = "file", max_length: int = 120) -> str:
    """把用户提供的文件名收敛成可安全落盘的名字（保留可读性，不保留路径）。"""
    text = unicodedata.normalize("NFC", str(raw or "")).replace("\x00", "")
    text = text.replace("\\", "/").split("/")[-1].strip()
    text = _BAD_CHARS.sub("_", text).strip(" .")
    if not text:
        text = fallback
    stem, dot, suffix = text.rpartition(".")
    if not dot:
        stem, suffix = text, ""
    stem = stem[: max(1, max_length - len(suffix) - 1)]
    if stem.lower() in _RESERVED:
        stem = f"_{stem}"
    return f"{stem}.{suffix}" if suffix else stem


# --------------------------------------------------------------------------- 根与守卫


def project_root(conn, user_id: int, project_id: str) -> Path:
    """取项目绑定目录；未绑定/已撤权/指向私有区一律拒绝（按当前授权实时复核）。"""
    project = project_service.get_owned(conn, project_id, int(user_id))
    root = project_service.assert_bound_root_granted(conn, project)
    if not root.is_dir():
        raise ApiError(409, "ROOT_MISSING", "项目绑定的目录已不存在，请重新绑定")
    if paths.is_private(root):
        raise ApiError(
            403,
            "ROOT_PRIVATE",
            "项目绑定目录指向服务端私有数据（数据库/密钥/引擎目录），不能作为业务目录使用",
        )
    return root


def guard(root: Path, target: Path) -> Path:
    """解析真实路径并确认它仍在项目内、且不落私有区；返回规范化的真实路径。

    这是本模块唯一的判定点。任何创建/写入/复制/清理动作都要先过这里，
    而不是等写完了再用字符串 ``relative_to`` 发现越界。
    """
    real = Path(os.path.realpath(str(target)))
    if not paths.is_within(real, root):
        raise ApiError(
            403,
            "PATH_OUTSIDE_PROJECT",
            "目标真实路径已离开项目绑定目录（可能是符号链接或 NTFS 联接），已拒绝",
        )
    if paths.is_private(real):
        raise ApiError(
            403,
            "PATH_PRIVATE",
            "目标属于服务端私有数据（数据库/密钥/引擎目录），业务文件不能写到这里",
        )
    return real


def project_dir(root: Path, *parts: str) -> Path:
    """创建（或复用）项目内的目录，返回**真实**路径。

    先查后建：如果某一级是通往项目外的联接，``guard`` 在 ``mkdir`` 之前就会拒绝，
    不会出现“报错了但目录已经建在项目外”的情况。创建后再查一次，覆盖
    “父目录在检查与创建之间被替换”的窄窗口。

    并发下要注意一件事：目标目录**还不存在**时，Windows 的 ``realpath`` 解析
    结果并不稳定（多线程同时创建同一层级时实测会偶发判成“越界”）。所以先校验
    最近一个**已存在**的祖先（联接若存在，一定在这一段里，能验出来），建完再校验
    完整路径；只有这时才做一次极短重试，仍不通过就照旧拒绝。
    """
    target = root
    for part in parts:
        text = str(part or "").strip()
        if not text or text in {".", ".."} or "/" in text or "\\" in text:
            raise ApiError(400, "PATH_INVALID", "目录名不合法")
        target = target / text
    # 每一轮都仍然做真实的 guard：重试只是为了绕开“目录正在被并发创建”时
    # realpath 的瞬时抖动，不是因为放宽判定。真越界（联接/符号链接）每轮都会失败。
    for attempt in range(3):
        try:
            existing = target
            while not existing.exists() and existing != root and root in existing.parents:
                existing = existing.parent
            guard(root, existing)
            target.mkdir(parents=True, exist_ok=True)
            return guard(root, target)
        except ApiError as exc:
            if exc.code != "PATH_OUTSIDE_PROJECT" or attempt == 2:
                raise
        time.sleep(0.02 * (attempt + 1))
    raise ApiError(403, "PATH_OUTSIDE_PROJECT", "目标真实路径已离开项目绑定目录")  # pragma: no cover


def unique_path(directory: Path, filename: str) -> Path:
    """同名文件不覆盖：追加 -2、-3……直到不冲突。

    只负责取名，**不建目录、不写盘**：调用方必须已经用 :func:`project_dir`
    建好并校验过目录。真正的并发安全来自“每个资产使用独立目录”
    （见 :mod:`backend.services.assets`），这个函数只解决同一目录内的重名。
    """
    if not directory.is_dir():
        raise ApiError(409, "DIR_MISSING", "目标目录不存在，请先通过项目目录入口创建")
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for index in range(2, 1000):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise ApiError(409, "NAME_CONFLICT", "同名文件过多，请改名后重试")


# --------------------------------------------------------------------------- 路径换算


def inside(root: Path, target: Path) -> bool:
    return paths.is_within(str(target), str(root))


def relative(root: Path, target: Path) -> str:
    """相对项目根的统一写法（正斜杠），数据库只存这个。"""
    try:
        return Path(os.path.realpath(str(target))).relative_to(
            Path(os.path.realpath(str(root)))
        ).as_posix()
    except ValueError:
        raise ApiError(400, "PATH_OUTSIDE_PROJECT", "文件必须位于项目绑定目录内") from None


def resolve_rel(root: Path, rel: str, *, must_exist: bool = False) -> Path:
    """把库里的相对路径解析回绝对路径，并再次确认没有跑到项目根之外。"""
    text = str(rel or "").replace("\\", "/").strip()
    if not text or text.startswith("/") or ".." in text.split("/"):
        raise ApiError(400, "PATH_INVALID", "路径不合法")
    target = guard(root, root / text)
    if must_exist and not target.is_file():
        raise ApiError(404, "FILE_MISSING", "文件已不存在")
    return target


# --------------------------------------------------------------------------- 写入


def write_stream(root: Path, target: Path, reader) -> Path:
    """把一个可读流写入项目内目标：临时文件 → 校验 → 原子替换。

    所有写盘动作（上传字节、解压条目、目录复制、结果图）都走这里，
    失败清理只动受控目录里的临时文件。
    """
    real = guard(root, target)
    parent = guard(root, real.parent)
    parent.mkdir(parents=True, exist_ok=True)
    guard(root, parent)
    tmp = parent / f".{real.name}.hx17-{security.new_token(4)}.tmp"
    try:
        with open(tmp, "wb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        guard(root, real)
        os.replace(tmp, real)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ApiError(500, "WRITE_FAILED", f"写入失败（{type(exc).__name__}）") from None
    return real


def write_bytes(root: Path, target: Path, data: bytes) -> Path:
    return write_stream(root, target, io.BytesIO(data))


def copy_into(root: Path, source: Path, destination: Path) -> Path:
    """把外部文件复制进项目（流式，避免整文件进内存），同样先校验再写。"""
    try:
        handle = open(source, "rb")
    except OSError as exc:
        raise ApiError(400, "SOURCE_UNREADABLE", f"源文件无法读取（{type(exc).__name__}）") from None
    with handle:
        try:
            return write_stream(root, destination, handle)
        except ApiError as exc:
            if exc.code == "WRITE_FAILED":
                raise ApiError(500, "COPY_FAILED", exc.msg) from None
            raise


def remove_tree(root: Path, target: Path) -> None:
    """失败清理专用：确认目标真实路径在项目内之后才递归删除。"""
    real = guard(root, target)
    if real == Path(os.path.realpath(str(root))) or not paths.is_within(real, root):
        raise ApiError(400, "PATH_OUTSIDE_PROJECT", "拒绝删除项目根或项目外目录")
    shutil.rmtree(real, ignore_errors=True)


__all__ = [
    "ARTIFACTS",
    "CACHE",
    "DATASETS",
    "UPLOADS",
    "StorageError",
    "copy_into",
    "guard",
    "inside",
    "project_dir",
    "project_root",
    "relative",
    "remove_tree",
    "resolve_rel",
    "safe_name",
    "unique_path",
    "write_bytes",
    "write_stream",
]

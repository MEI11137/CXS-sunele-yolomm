"""项目目录内的受控文件操作。

所有路径都必须先收敛到「项目当前绑定目录」的真实路径：

* 相对路径按绑定目录展开，绝对路径也照样校验；
* 拒绝 UNC、``\\\\?\\`` 设备前缀、盘符相对路径、NTFS 备用数据流与保留设备名；
* ``realpath`` 之后再判断包含关系，符号链接 / NTFS 联接指向外部即拒绝；
* 服务端私有数据（数据库、密钥、引擎缓存与日志）即使落在绑定目录内也一律不可见；
* 删除不直接抹掉，而是移入**项目工作区内**的 ``.hx17-trash`` 并写清单，可人工找回。

这里没有任何“任意命令”或“任意 SQL”的入口：参数只能是真实路径与真实 ID。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from backend import paths, security
from backend.agent.context import ToolError

MAX_TEXT_BYTES = 2 * 1024 * 1024
DEFAULT_READ_BYTES = 48_000
MAX_READ_BYTES = 200_000
MAX_LIST = 200
MAX_SEARCH = 100
SEARCH_SCAN_LIMIT = 20_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vs"}
TRASH_DIRNAME = ".hx17-trash"
TRASH_MANIFEST = "manifest.json"
_DEVICE_PREFIXES = ("\\\\.\\", "\\\\?\\", "//./", "//?/")
_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}

# 服务端私有根由 :mod:`backend.paths` 统一登记：业务文件服务与文件工具共用一份规则，
# 不会出现“工具挡住了、业务接口还能写”的缺口。
def configure_private(roots) -> None:
    """登记服务端私有目录；命中即拒绝，与目录授权范围无关。"""
    paths.configure_private(roots)


def _private_roots() -> list[Path]:
    return paths.private_roots()


def _is_private(path: Path) -> bool:
    return paths.is_private(path)


def _reject_unsupported(text: str) -> None:
    if text.startswith(_DEVICE_PREFIXES):
        raise ToolError("PATH_DEVICE", "不支持设备路径或 \\\\?\\ 扩展前缀")
    if text.startswith(("\\\\", "//")):
        raise ToolError("PATH_UNC", "不支持 UNC 网络路径，请先把共享目录映射为本地盘符")
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha() and len(text) > 2:
        if text[2] not in "\\/":
            raise ToolError("PATH_RELATIVE_DRIVE", "请填写完整绝对路径")


def _validate_segments(text: str) -> None:
    """拒绝 Windows 备用数据流、保留设备名与末尾点/空格。

    这些写法在 Win32 上会打开“另一个东西”：``file.txt:stream`` 是 ADS，
    ``CON``/``NUL`` 是设备，``name.`` 会被静默当成 ``name``。它们都绕开
    直观的“目录内文件”语义，因此在参数层直接拒绝。
    """
    body = text[2:] if len(text) >= 2 and text[1] == ":" else text
    for segment in body.replace("/", "\\").split("\\"):
        if not segment or segment in {".", ".."}:
            # "." / ".." 交给 realpath + 包含关系校验处理，不在这里误判成“末尾点”。
            continue
        if ":" in segment:
            raise ToolError("PATH_ADS", "路径里不能包含冒号（Windows 备用数据流）")
        if segment.endswith((".", " ")):
            raise ToolError("PATH_TRAILING", "文件名不能以点或空格结尾")
        stem = segment.split(".", 1)[0].strip().lower()
        if stem in _RESERVED_NAMES:
            raise ToolError("PATH_RESERVED", "不能用 Windows 保留设备名作为文件名")


def require_root(root: Path | None) -> Path:
    if root is None:
        raise ToolError(
            "NO_PROJECT_ROOT",
            "该项目还没有绑定目录，先让用户在项目页绑定服务器目录后再操作文件",
            status=409,
        )
    real = Path(os.path.realpath(str(root)))
    if not real.is_dir():
        raise ToolError("ROOT_MISSING", "项目绑定目录已不存在，请让用户重新绑定", status=409)
    if _is_private(real):
        raise ToolError(
            "ROOT_PRIVATE",
            "项目绑定目录指向服务端私有数据（数据库/密钥/引擎目录），不能作为业务目录使用",
            status=403,
        )
    return real


def resolve(root: Path, raw: object, *, must_exist: bool = False) -> Path:
    """把模型给的路径收敛成绑定目录内的真实路径。"""
    raw_text = "" if raw is None else str(raw)
    text = raw_text.strip()
    if not text:
        raise ToolError("PATH_REQUIRED", "必须给出项目内的路径")
    if raw_text != raw_text.rstrip():
        # 末尾空格在 Win32 上会被静默忽略，指向另一个文件；明确拒绝而不是猜。
        raise ToolError("PATH_TRAILING", "路径不能以空格结尾")
    if "\x00" in text:
        raise ToolError("PATH_INVALID", "路径包含非法字符")
    _reject_unsupported(text)
    _validate_segments(text)
    if len(text) > 1000:
        raise ToolError("PATH_TOO_LONG", "路径过长")

    candidate = Path(os.path.expanduser(text))
    if not candidate.is_absolute():
        candidate = root / candidate
    real = Path(os.path.realpath(os.path.abspath(str(candidate))))
    if not paths.is_within(real, root):
        raise ToolError(
            "PATH_OUTSIDE_PROJECT",
            "只能访问当前项目绑定目录内的文件；该路径在目录之外",
            status=403,
        )
    if _is_private(real):
        raise ToolError(
            "PATH_PRIVATE",
            "该路径属于服务端私有数据（数据库/密钥/引擎目录），工具不能读写",
            status=403,
        )
    if must_exist and not real.exists():
        raise ToolError("PATH_NOT_FOUND", f"路径不存在：{relative(root, real)}", status=404)
    return real


def relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/") or "."
    except ValueError:
        return str(path)


def trash_root(root: Path) -> Path:
    return root / TRASH_DIRNAME


def _is_trash(root: Path, path: Path) -> bool:
    return paths.is_within(path, trash_root(root))


# --------------------------------------------------------------------------- 读


def _entry_key(path: Path, is_dir: bool) -> tuple[int, str, str]:
    return (0 if is_dir else 1, path.name.casefold(), path.name)


def _encode_cursor(key: tuple[int, str, str]) -> str:
    raw = json.dumps(list(key), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(value: str) -> tuple[int, str, str] | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(text.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ToolError("BAD_CURSOR", "分页游标无效；请不带游标重新列一次目录") from None
    if not (isinstance(data, list) and len(data) == 3 and data[0] in (0, 1)):
        raise ToolError("BAD_CURSOR", "分页游标无效；请不带游标重新列一次目录")
    return (int(data[0]), str(data[1]), str(data[2]))


def list_dir(root: Path, raw: object, *, limit: int = 100, cursor: str = "") -> dict:
    target = resolve(root, raw or ".", must_exist=True)
    if not target.is_dir():
        raise ToolError("NOT_A_DIRECTORY", f"不是目录：{relative(root, target)}")
    cap = max(1, min(int(limit or 100), MAX_LIST))
    after = _decode_cursor(cursor)
    try:
        children = sorted(
            target.iterdir(), key=lambda item: _entry_key(item, _safe_is_dir(item))
        )
    except PermissionError:
        raise ToolError("PATH_DENIED", "没有权限读取该目录", status=403) from None
    except OSError:
        raise ToolError("PATH_UNREADABLE", "目录无法读取")

    entries: list[dict] = []
    blocked = 0
    skipped_private = 0
    last_key: tuple[int, str, str] | None = None
    for child in children:
        real = Path(os.path.realpath(str(child)))
        if not paths.is_within(real, root):
            blocked += 1
            continue
        if _is_private(real):
            skipped_private += 1
            continue
        if real.name == TRASH_DIRNAME and real.parent == root:
            # 回收区是内部实现，不当作业务目录暴露。
            continue
        is_dir = _safe_is_dir(real)
        key = _entry_key(child, is_dir)
        if after is not None and key <= after:
            continue
        last_key = key
        try:
            stat = real.stat()
            size, mtime = (0 if is_dir else stat.st_size), int(stat.st_mtime)
        except OSError:
            size, mtime = 0, 0
        entries.append(
            {
                "name": child.name,
                "path": str(real),
                "relative": relative(root, real),
                "is_dir": is_dir,
                "size": size,
                "mtime": mtime,
                "cursor": _encode_cursor(key),
            }
        )
        if len(entries) >= cap:
            break
    truncated = len(entries) >= cap
    return {
        "path": str(target),
        "relative": relative(root, target),
        "entries": entries,
        "truncated": truncated,
        "next_cursor": _encode_cursor(last_key) if truncated and last_key else None,
        "blocked_links": blocked,
        "skipped_private": skipped_private,
    }


def _safe_is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def search(root: Path, query: object, *, start: object = ".", limit: int = 30) -> dict:
    text = str(query or "").strip()
    if len(text) < 2:
        raise ToolError("QUERY_TOO_SHORT", "搜索关键词至少 2 个字符")
    base = resolve(root, start or ".", must_exist=True)
    if not base.is_dir():
        raise ToolError("NOT_A_DIRECTORY", "搜索起点不是目录")
    cap = max(1, min(int(limit or 30), MAX_SEARCH))
    needle = text.lower()
    hits: list[dict] = []
    scanned = 0
    for current, dirs, files in os.walk(base):
        if scanned >= SEARCH_SCAN_LIMIT or len(hits) >= cap:
            break
        dirs[:] = sorted(d for d in dirs if d.lower() not in SKIP_DIRS and d != TRASH_DIRNAME)
        for name in sorted(files) + dirs:
            scanned += 1
            if scanned > SEARCH_SCAN_LIMIT:
                break
            if needle not in name.lower():
                continue
            real = Path(os.path.realpath(os.path.join(current, name)))
            if not paths.is_within(real, root) or _is_private(real):
                continue
            hits.append(
                {
                    "name": name,
                    "path": str(real),
                    "relative": relative(root, real),
                    "is_dir": _safe_is_dir(real),
                }
            )
            if len(hits) >= cap:
                break
    return {
        "query": text,
        "base": relative(root, base),
        "hits": hits,
        "scanned": scanned,
        "truncated": len(hits) >= cap or scanned >= SEARCH_SCAN_LIMIT,
    }


def read_file(root: Path, raw: object, *, offset: int = 0, limit: int | None = None) -> dict:
    target = resolve(root, raw, must_exist=True)
    if target.is_dir():
        raise ToolError("IS_DIRECTORY", "目标是目录，请用列目录工具")
    try:
        stat = target.stat()
    except OSError:
        raise ToolError("PATH_UNREADABLE", "文件无法读取") from None
    size = int(stat.st_size)
    meta = {
        "path": str(target),
        "relative": relative(root, target),
        "size": size,
        "mtime": int(stat.st_mtime),
    }
    if size == 0:
        return {**meta, "kind": "empty", "text": "", "eof": True, "next_offset": 0}

    try:
        with open(target, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        raise ToolError("PATH_UNREADABLE", "文件无法读取") from None

    # 图片等二进制先给元信息，不把大段二进制当文本塞进对话。
    if b"\x00" in head:
        return {
            **meta,
            "kind": "binary",
            "text": "",
            "eof": True,
            "next_offset": 0,
            "hint": "这是二进制文件（可能是图片/模型/压缩包）。需要内容时请让用户确认处理方式。",
        }

    explicit = limit is not None
    if size > MAX_TEXT_BYTES and not explicit:
        return {
            **meta,
            "kind": "large",
            "text": "",
            "eof": False,
            "next_offset": int(offset or 0),
            "hint": "文件较大，请带 offset/limit 分段读取（每次不超过 200000 字节）。",
        }

    start = max(0, int(offset or 0))
    span = max(1, min(int(limit or DEFAULT_READ_BYTES), MAX_READ_BYTES))
    try:
        with open(target, "rb") as handle:
            handle.seek(start)
            chunk = handle.read(span)
    except OSError:
        raise ToolError("PATH_UNREADABLE", "文件无法读取") from None
    text, encoding = _decode(chunk)
    next_offset = start + len(chunk)
    return {
        **meta,
        "kind": "text",
        "encoding": encoding,
        "offset": start,
        "next_offset": next_offset,
        "eof": next_offset >= size,
        "text": text,
    }


def _decode(chunk: bytes) -> tuple[str, str]:
    for encoding in ("utf-8", "gb18030"):
        try:
            return chunk.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return chunk.decode("utf-8", errors="replace"), "utf-8/replace"


# --------------------------------------------------------------------------- 写


def write_file(root: Path, raw: object, content: object, *, mode: str = "auto") -> dict:
    text = content if isinstance(content, str) else str(content or "")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ToolError("CONTENT_TOO_LARGE", "单次写入内容过大（上限 2MB），请分开写入")
    target = resolve(root, raw)
    if target.exists() and target.is_dir():
        raise ToolError("IS_DIRECTORY", "目标是目录，不能写入文件")
    existed = target.exists()
    kind = str(mode or "auto").lower()
    if kind == "create" and existed:
        raise ToolError("FILE_EXISTS", "文件已存在；如需改动请用 overwrite 或 auto", status=409)
    if kind in {"overwrite", "append"} and not existed:
        raise ToolError("FILE_MISSING", "文件不存在；新建请用 create 或 auto", status=404)
    if kind not in {"auto", "create", "overwrite", "append"}:
        raise ToolError("BAD_MODE", "写入模式只能是 auto/create/overwrite/append")

    target.parent.mkdir(parents=True, exist_ok=True)
    if not paths.is_within(Path(os.path.realpath(str(target.parent))), root):
        raise ToolError("PATH_OUTSIDE_PROJECT", "父目录在项目目录之外", status=403)
    before_size = target.stat().st_size if existed and target.is_file() else 0
    if kind == "append":
        try:
            with open(target, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        except OSError as exc:
            raise ToolError("WRITE_FAILED", f"写入失败（{type(exc).__name__}）") from None
    else:
        # 先写同目录临时文件再替换：失败时旧文件原样保留，不会出现半截内容。
        tmp = target.with_name(f".{target.name}.hx17-{security.new_token(4)}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise ToolError("WRITE_FAILED", f"写入失败（{type(exc).__name__}）") from None
    # 独立回读：把磁盘上现在的字节重新读一遍再比对，不信任上面写操作的返回。
    verification = _verify_after_write(
        target,
        text=text,
        appended=kind == "append",
        before_size=before_size,
    )
    stat = target.stat()
    return {
        "path": str(target),
        "relative": relative(root, target),
        "bytes": int(stat.st_size),
        "created": not existed,
        "mode": kind,
        "verification": verification,
    }


def _check_destination(root: Path, src: Path, dst: Path, *, overwrite: bool) -> bool:
    """返回目标是否已存在；把“目标目录”与“目标文件”的语义分开。"""
    if src == dst:
        raise ToolError("SAME_PATH", "源路径和目标路径相同")
    if src.is_dir() and paths.is_within(dst, src):
        raise ToolError("MOVE_INTO_SELF", "不能把目录移动到它自己内部")
    if _is_trash(root, src):
        raise ToolError("TRASH_LOCKED", "回收区内的条目不能直接移动；需要找回请联系管理员", status=403)
    if dst.exists():
        if dst.is_dir():
            raise ToolError(
                "DEST_IS_DIRECTORY",
                "目标是一个已存在的目录。要放进去请把目标写成完整文件名"
                "（例如 dir/文件名），本工具不会替你猜测嵌套位置",
                status=409,
            )
        if not overwrite:
            raise ToolError("DEST_EXISTS", "目标已存在；如需覆盖请显式允许覆盖", status=409)
    if not dst.parent.exists():
        raise ToolError("DEST_PARENT_MISSING", f"目标父目录不存在：{relative(root, dst.parent)}")
    return dst.exists()


def move_path(root: Path, source: object, dest: object, *, overwrite: bool = False) -> dict:
    src = resolve(root, source, must_exist=True)
    dst = resolve(root, dest)
    if src == root:
        raise ToolError("CANNOT_MOVE_ROOT", "不能移动项目绑定目录本身", status=403)
    existed = _check_destination(root, src, dst, overwrite=overwrite)
    if existed and src.is_dir():
        raise ToolError(
            "DEST_EXISTS",
            "目标已存在且源是目录；覆盖目录需要人工清理，工具不会递归替换",
            status=409,
        )
    before = digest(src)
    try:
        if existed:
            os.replace(src, dst)
        else:
            shutil.move(str(src), str(dst))
    except OSError as exc:
        raise ToolError("MOVE_FAILED", f"移动失败（{type(exc).__name__}）") from None
    after = digest(dst)
    if not _same(before, after):
        _fail_verify("移动后的内容与移动前不一致", expected=before, actual=after)
    if src.exists():
        raise ToolError("FS_VERIFY_FAILED", "移动后源路径仍然存在，操作结果不可信", status=409)
    return {
        "from": relative(root, src),
        "to": relative(root, dst),
        "path": str(dst),
        "overwrote": existed,
        "verification": {"verified": True, "source_removed": True, **after},
    }


def copy_path(root: Path, source: object, dest: object, *, overwrite: bool = False) -> dict:
    src = resolve(root, source, must_exist=True)
    dst = resolve(root, dest)
    if src == root:
        raise ToolError("CANNOT_COPY_ROOT", "不能复制项目绑定目录本身", status=403)
    existed = _check_destination(root, src, dst, overwrite=overwrite)
    before = digest(src)
    try:
        if src.is_dir():
            if existed:
                raise ToolError(
                    "DEST_EXISTS",
                    "目标已存在且源是目录；覆盖目录需要人工清理，工具不会递归替换",
                    status=409,
                )
            shutil.copytree(src, dst, symlinks=False)
            files = sum(1 for item in dst.rglob("*") if item.is_file())
            size = sum(item.stat().st_size for item in dst.rglob("*") if item.is_file())
        else:
            tmp = dst.with_name(f".{dst.name}.hx17-{security.new_token(4)}.tmp")
            shutil.copy2(src, tmp)
            if existed:
                os.replace(tmp, dst)
            else:
                os.replace(tmp, dst)
            files, size = 1, dst.stat().st_size
    except ToolError:
        raise
    except OSError as exc:
        raise ToolError("COPY_FAILED", f"复制失败（{type(exc).__name__}）") from None
    after = digest(dst)
    if not _same(before, after):
        _fail_verify("复制出来的内容与源不一致", expected=before, actual=after)
    return {
        "from": relative(root, src),
        "to": relative(root, dst),
        "path": str(dst),
        "files": int(files),
        "bytes": int(size),
        "overwrote": existed,
        "verification": {"verified": True, **after},
    }


def delete_path(root: Path, raw: object, *, recursive: bool = False) -> dict:
    target = resolve(root, raw, must_exist=True)
    if target == root:
        raise ToolError("CANNOT_DELETE_ROOT", "不能删除项目绑定目录本身", status=403)
    if _is_trash(root, target):
        raise ToolError("TRASH_LOCKED", "回收区内的条目不能再次删除；需要清理由管理员处理", status=403)
    if target.is_dir() and any(target.iterdir()) and not recursive:
        raise ToolError("DIRECTORY_NOT_EMPTY", "目录非空；确实要删请显式允许递归删除", status=409)

    container = trash_root(root)
    container.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    trash_id = f"{stamp}-{security.new_token(4)}"
    entry = container / trash_id
    entry.mkdir(parents=True, exist_ok=False)
    payload = entry / "payload"
    before = digest(target)
    try:
        shutil.move(str(target), str(payload))
    except OSError as exc:
        try:
            entry.rmdir()
        except OSError:
            pass
        raise ToolError("DELETE_FAILED", f"删除失败（{type(exc).__name__}）") from None
    manifest = {
        "trash_id": trash_id,
        "original_relative": relative(root, target),
        "original_path": str(target),
        "was_dir": payload.is_dir(),
        "deleted_at": security.now_iso(),
        "note": "由 Agent 文件工具移入项目回收区，可人工找回",
    }
    try:
        (entry / TRASH_MANIFEST).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass
    after = digest(payload)
    if not _same(before, after):
        _fail_verify("回收区里的副本与原文件不一致", expected=before, actual=after)
    if target.exists():
        raise ToolError("FS_VERIFY_FAILED", "删除后原路径仍然存在，操作结果不可信", status=409)
    return {
        "deleted": True,
        "relative": manifest["original_relative"],
        "was_dir": manifest["was_dir"],
        "recoverable": True,
        "trash_id": trash_id,
        "verification": {"verified": True, "trash_copy": after},
        "trash_relative": f"{TRASH_DIRNAME}/{trash_id}/payload",
        "hint": "文件已移入本项目目录下的 .hx17-trash（附 manifest.json），可人工找回；不是不可逆删除。",
    }


# --------------------------------------------------------------------------- 回读校验
#
# 每个写操作都要**独立回读**：不看自己刚返回的统计，而是重新算目标当前的内容指纹。
# 指纹口径固定（文件=大小+sha256；目录=文件数+总字节+逐文件摘要），所以"写进去的"和
# "读回来的"可以直接比。大文件/超大目录降级成"首尾采样"，此时结果里会写明 mode=sampled。

_DIGEST_FULL_LIMIT = 64 * 1024 * 1024
_DIGEST_DIR_FILE_LIMIT = 5000


def _file_digest(path: Path) -> dict:
    size = path.stat().st_size
    digest = hashlib.sha256()
    if size <= _DIGEST_FULL_LIMIT:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return {"size": int(size), "sha256": digest.hexdigest(), "mode": "full"}
    chunk = 32 * 1024 * 1024
    with open(path, "rb") as handle:
        digest.update(handle.read(chunk))
        handle.seek(max(0, size - chunk))
        digest.update(handle.read(chunk))
    digest.update(str(size).encode("ascii"))
    return {"size": int(size), "sha256": digest.hexdigest(), "mode": "sampled"}


def digest(path: Path) -> dict:
    """内容指纹：文件看 sha256，目录看「文件数 + 总字节 + 逐文件摘要」。"""
    if not path.exists():
        return {"exists": False}
    if path.is_file():
        detail = _file_digest(path)
        return {"exists": True, "is_dir": False, "files": 1, **detail}
    digest_value = hashlib.sha256()
    files = 0
    total = 0
    truncated = False
    for item in sorted(path.rglob("*"), key=lambda entry: str(entry).casefold()):
        if not item.is_file():
            continue
        files += 1
        rel = item.relative_to(path).as_posix()
        detail = _file_digest(item)
        total += int(detail["size"])
        if files <= _DIGEST_DIR_FILE_LIMIT:
            digest_value.update(f"{rel}|{detail['size']}|{detail['sha256']}\n".encode("utf-8"))
        else:
            truncated = True
    return {
        "exists": True,
        "is_dir": True,
        "files": files,
        "size": total,
        "sha256": digest_value.hexdigest(),
        "mode": "sampled" if truncated else "full",
    }


def _same(left: dict, right: dict) -> bool:
    if not left.get("exists") or not right.get("exists"):
        return False
    if bool(left.get("is_dir")) != bool(right.get("is_dir")):
        return False
    keys = ("size", "files", "sha256") if not left.get("is_dir") else ("size", "files", "sha256")
    return all(left.get(key) == right.get(key) for key in keys)


def _fail_verify(message: str, *, expected: dict, actual: dict) -> None:
    raise ToolError(
        "FS_VERIFY_FAILED",
        f"回读校验没通过：{message}（期望 {expected.get('files')} 个文件/{expected.get('size')} 字节，"
        f"实际 {actual.get('files')} 个文件/{actual.get('size')} 字节）——操作结果与预期不一致，请人工确认",
        status=409,
    )


def verify_written(target: Path, *, expected_size: int, expected_sha: str) -> dict:
    actual = digest(target)
    ok = actual.get("exists") and int(actual.get("size") or -1) == int(expected_size) and (
        actual.get("sha256") == expected_sha
    )
    if not ok:
        _fail_verify(
            "写入后的内容与服务端准备写入的字节不一致",
            expected={"files": 1, "size": expected_size},
            actual=actual,
        )
    return {"verified": True, **actual}


def _verify_after_write(target: Path, *, text: str, appended: bool, before_size: int) -> dict:
    """写完读回来比对；追加大文件时用"大小差 + 尾部字节"代替整份读入。"""
    payload = text.encode("utf-8")
    if not appended:
        actual = target.read_bytes()
        if actual != payload:
            raise ToolError(
                "FS_VERIFY_FAILED",
                f"回读校验没通过：磁盘上是 {len(actual)} 字节，服务端准备写入 {len(payload)} 字节",
                status=409,
            )
        return {
            "verified": True,
            "bytes": len(actual),
            "sha256": hashlib.sha256(actual).hexdigest(),
            "method": "exact",
        }
    size = target.stat().st_size
    if size != before_size + len(payload):
        raise ToolError(
            "FS_VERIFY_FAILED",
            f"回读校验没通过：追加后应为 {before_size + len(payload)} 字节，实际 {size} 字节",
            status=409,
        )
    if before_size <= 8 * 1024 * 1024:
        expected = target.read_bytes()
        if not expected.endswith(payload):
            raise ToolError("FS_VERIFY_FAILED", "回读校验没通过：追加的尾部内容不一致", status=409)
        return {"verified": True, "bytes": size, "method": "exact", "appended": len(payload)}
    with open(target, "rb") as handle:
        handle.seek(max(0, size - len(payload)))
        tail = handle.read(len(payload))
    if tail != payload:
        raise ToolError("FS_VERIFY_FAILED", "回读校验没通过：追加的尾部内容不一致", status=409)
    return {"verified": True, "bytes": size, "method": "tail", "appended": len(payload)}


# --------------------------------------------------------------------------- 合并与去重


def merge_files(
    root: Path,
    sources: object,
    dest: object,
    *,
    separator: str = "\n\n",
    overwrite: bool = False,
) -> dict:
    """把多个文本文件按顺序合并成一个新文件（源文件不动），写完回读比对。"""
    if not isinstance(sources, list) or len(sources) < 2:
        raise ToolError("VALIDATION", "合并至少需要两个源文件")
    if len(sources) > 50:
        raise ToolError("VALIDATION", "一次最多合并 50 个文件")
    target = resolve(root, dest)
    if target.exists() and not overwrite:
        raise ToolError("DEST_EXISTS", "目标已存在；如需覆盖请显式允许覆盖", status=409)
    if target.exists() and target.is_dir():
        raise ToolError("DEST_IS_DIRECTORY", "目标是目录，不能当作合并结果文件", status=409)
    join = str(separator if separator is not None else "\n\n")
    chunks: list[str] = []
    used: list[str] = []
    total = 0
    for raw in sources:
        item = resolve(root, raw, must_exist=True)
        if item.is_dir():
            raise ToolError("IS_DIRECTORY", f"只能合并文件：{relative(root, item)} 是目录")
        if item == target:
            raise ToolError("SAME_PATH", "目标不能是某个源文件本身")
        if item.stat().st_size > MAX_TEXT_BYTES:
            raise ToolError("CONTENT_TOO_LARGE", f"{relative(root, item)} 超过 2MB，不适合合并")
        try:
            data = item.read_bytes()
        except OSError as exc:
            raise ToolError("READ_FAILED", f"读取失败（{type(exc).__name__}）") from None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError("NOT_TEXT", f"{relative(root, item)} 不是 UTF-8 文本，无法合并") from None
        chunks.append(text)
        used.append(relative(root, item))
        total += len(data)
    merged = join.join(chunks)
    if len(merged.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ToolError("CONTENT_TOO_LARGE", "合并结果超过 2MB，请分批合并")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not paths.is_within(Path(os.path.realpath(str(target.parent))), root):
        raise ToolError("PATH_OUTSIDE_PROJECT", "父目录在项目目录之外", status=403)
    try:
        target.write_text(merged, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise ToolError("WRITE_FAILED", f"写入失败（{type(exc).__name__}）") from None
    actual = target.read_bytes()
    if actual != merged.encode("utf-8"):
        raise ToolError("FS_VERIFY_FAILED", "回读校验没通过：合并结果与预期字节不一致", status=409)
    return {
        "path": str(target),
        "relative": relative(root, target),
        "sources": used,
        "bytes": len(actual),
        "sha256": hashlib.sha256(actual).hexdigest(),
        "verification": {"verified": True, "method": "exact", "bytes": len(actual)},
        "note": "源文件没有改动；合并结果已按字节回读确认。",
    }


_DUPLICATE_FILE_LIMIT = 5000
_DUPLICATE_BYTES_LIMIT = 2 * 1024 * 1024 * 1024


def _walk_files(root: Path, start: Path) -> tuple[list[Path], bool]:
    files: list[Path] = []
    total = 0
    capped = False
    for item in start.rglob("*"):
        if not item.is_file():
            continue
        real = Path(os.path.realpath(str(item)))
        if not paths.is_within(real, root) or _is_private(real) or _is_trash(root, real):
            continue
        total += real.stat().st_size
        if len(files) >= _DUPLICATE_FILE_LIMIT or total > _DUPLICATE_BYTES_LIMIT:
            capped = True
            break
        files.append(real)
    return files, capped


def find_duplicates(root: Path, raw: object = ".", *, limit: int = 20) -> dict:
    """按内容找重复文件（先比大小再比 sha256）；只读，不动任何文件。"""
    start = resolve(root, raw or ".", must_exist=True)
    if not start.is_dir():
        raise ToolError("NOT_A_DIRECTORY", "去重扫描需要一个目录")
    files, capped = _walk_files(root, start)
    buckets: dict[int, list[Path]] = {}
    for item in files:
        try:
            buckets.setdefault(item.stat().st_size, []).append(item)
        except OSError:
            continue
    groups: list[dict] = []
    scanned = 0
    for size, items in buckets.items():
        if len(items) < 2 or size == 0:
            continue
        by_hash: dict[str, list[Path]] = {}
        for item in items:
            scanned += 1
            try:
                detail = _file_digest(item)
            except OSError:
                continue
            by_hash.setdefault(str(detail["sha256"]), []).append(item)
        for sha, same in by_hash.items():
            if len(same) < 2:
                continue
            ordered = sorted(same, key=lambda entry: relative(root, entry).casefold())
            groups.append(
                {
                    "sha256": sha,
                    "size": int(size),
                    "count": len(ordered),
                    "reclaimable": int(size) * (len(ordered) - 1),
                    "files": [relative(root, entry) for entry in ordered],
                }
            )
    groups.sort(key=lambda group: group["reclaimable"], reverse=True)
    cap = max(1, min(int(limit or 20), 50))
    return {
        "path": relative(root, start),
        "scanned_files": len(files),
        "hashed_files": scanned,
        "capped": capped,
        "groups": groups[:cap],
        "total_groups": len(groups),
        "reclaimable_bytes": sum(group["reclaimable"] for group in groups),
        "note": (
            "按内容（大小 + sha256）判重，不是按文件名。这里只报告，不删除任何文件；"
            "要清理请用 hx_remove_duplicates（进回收区、可回读核对）。"
        ),
    }


def remove_duplicates(
    root: Path, raw: object = ".", *, keep: str = "first", dry_run: bool = False
) -> dict:
    """把重复文件里多余的移入回收区（每组保留一个），并逐组回读核对。"""
    listing = find_duplicates(root, raw, limit=50)
    if not listing["groups"]:
        return {"removed": [], "kept": [], "dry_run": bool(dry_run), "note": "没有发现重复内容。"}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    container = trash_root(root) / f"dedup-{stamp}-{security.new_token(3)}"
    removed: list[dict] = []
    kept: list[str] = []
    for group in listing["groups"]:
        order = group["files"]
        if keep == "shortest":
            order = sorted(order, key=lambda item: (len(item), item))
        elif keep == "newest":
            order = sorted(
                order,
                key=lambda item: -(
                    resolve(root, item).stat().st_mtime if resolve(root, item).exists() else 0
                ),
            )
        winner = order[0]
        kept.append(winner)
        if dry_run:
            continue
        for loser in order[1:]:
            source = resolve(root, loser, must_exist=True)
            before = digest(source)
            payload = container / "payload" / loser
            payload.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(source), str(payload))
            except OSError as exc:
                raise ToolError("MOVE_FAILED", f"移动重复文件失败（{type(exc).__name__}）") from None
            after = digest(payload)
            if not _same(before, after) or source.exists():
                _fail_verify(f"重复文件 {loser} 移入回收区后不一致", expected=before, actual=after)
            removed.append({"relative": loser, "trash_relative": f"{relative(root, payload)}"})
    if not dry_run:
        container.mkdir(parents=True, exist_ok=True)
        manifest = {
            "created_at": security.now_iso(),
            "kept": kept,
            "removed": removed,
            "note": "按内容去重：每组保留一个，其余移入回收区，可人工找回",
        }
        try:
            (container / TRASH_MANIFEST).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass
    return {
        "removed": removed,
        "kept": kept,
        "dry_run": bool(dry_run),
        "trash_relative": relative(root, container) if not dry_run else "",
        "note": (
            "每组保留一个文件，其余移入项目回收区（不是不可逆删除）；"
            "每个被移动的文件都与原内容做过指纹比对。"
        ),
    }

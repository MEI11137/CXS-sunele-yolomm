"""海巡智检 v1.8 同源启动器（Windows / Linux / macOS 通用）。

一个端口同时提供：

* ``/api/*``：Flask 应用（真实业务服务）；
* ``/`` 及其它路径：已构建的 ``frontend/dist``（SPA 回落 ``index.html``）。

对外演示用 **Waitress**（纯 Python WSGI 服务器），不是 Flask 开发服务器：
后台引擎、事件轮询和同源 Cookie 鉴权都走同一条生产路径，``debug`` 永远不会打开。

用法（在**本项目文件夹**里）：

    python deploy/serve.py                # 前台启动（默认 127.0.0.1:8080）
    python deploy/serve.py --check        # 只检查环境与构建产物
    python deploy/serve.py --status       # 查看本数据目录记录的实例
    python deploy/serve.py --stop         # 停止本数据目录记录的实例

Windows 下直接双击根目录的 ``启动海巡智检.cmd`` 即可（内部调用本文件）。

进程/端口约定：

* 默认只监听 ``127.0.0.1``，端口由 ``HX17_PORT`` 或 ``--port`` 指定（默认 8080）；
* 数据目录由 ``HX17_DATA_DIR`` 或 ``--data-dir`` 指定（默认本工程 ``var/``）；
* 启动时把 ``run/server.json``（PID、端口、令牌哈希、数据目录）写进数据目录；重复
  启动会先探测这个实例，是同一个就提示“已在运行”，不会起第二个；
* 端口被别的程序占用时只报错退出，**绝不结束未记录的进程**；
* ``--stop`` 只对“记录在案且自报身份一致”的进程发起停止，不按进程名杀 Python。

身份判定只有一份（``identity_conflict``）：**本次解析出的数据目录**、
**运行记录里的 data_dir**、**服务自报的 data_dir** 三处必须同时对上，再加上
instance 与 PID。所以从别的目录拷贝过来的 ``server.json`` 既不会被当成“已在运行”，
也不会被当成可以停止的实例；记录里的 PID 还活着但服务不响应时，同样拒绝在同目录
再起一个（否则两个进程会争用同一份数据库、把对方的轮次标成中断）。

退出码（供脚本判断，不是 HTTP 状态码）：

* ``0`` 成功；``1`` 环境检查失败（缺依赖/未构建/数据目录不可写）；
* ``3`` 记录存在但服务未响应（进程可能已退出）；
* ``4`` 运行记录属于另一个数据目录，或同目录已有实例；
* ``5`` 端口被占用（**不结束任何进程**）；
* ``6`` 记录里的 PID 仍存活但服务不响应（需人工确认）；
* ``7`` 端口上的服务与运行记录身份不一致；
* ``8`` 已请求停止但没有确认；
* ``9`` 还有进行中的对话/业务/训练，已拒绝（``--force`` 才停）；
* ``10`` 读不到任务状态（数据库损坏/不可读），已拒绝普通停止。
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_DATA_DIR = ROOT / "var"
TOKEN_HEADER = "X-HX17-Service-Token"
MIN_PYTHON = (3, 10)
NODE_FOR_LOGIC_TESTS = (22, 6)

# 后端进程启动真正 import 的包；缺任何一个都给同一条安装命令。
BACKEND_MODULES = (
    ("flask", "Flask"),
    ("itsdangerous", "itsdangerous"),
    ("cryptography", "cryptography"),
    ("requests", "requests"),
    ("mcp", "mcp"),
    ("anyio", "anyio"),
    ("PIL", "Pillow"),
    ("yaml", "PyYAML"),
    ("pypdf", "pypdf"),
    ("docx", "python-docx"),
)


# --------------------------------------------------------------------------- 状态文件


def resolve_data_dir(raw: str | None) -> Path:
    if raw:
        target = Path(raw).expanduser()
    else:
        env = (os.environ.get("HX17_DATA_DIR") or "").strip()
        target = Path(env).expanduser() if env else DEFAULT_DATA_DIR
    return target.resolve()


def state_path(data_dir: Path) -> Path:
    return data_dir / "run" / "server.json"


def load_state(data_dir: Path) -> dict | None:
    path = state_path(data_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_state(data_dir: Path, payload: dict) -> Path:
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:  # Windows 上权限位意义有限，失败不影响使用
        pass
    os.replace(tmp, path)
    return path


def remove_state(data_dir: Path) -> None:
    try:
        state_path(data_dir).unlink()
    except FileNotFoundError:
        pass


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- 探测


def _pid_alive(pid: int) -> bool:
    """只回答“这个 PID 现在是否还活着”，不用它判断身份（身份看服务自报）。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def probe(state: dict, timeout: float = 2.0) -> dict | None:
    """向记录的实例查状态；返回它自报的 data，失败返回 None。"""
    host = str(state.get("host") or DEFAULT_HOST)
    port = int(state.get("port") or 0)
    token = str(state.get("token") or "")
    if not port or not token:
        return None
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    request = urllib.request.Request(
        f"http://{host}:{port}/api/service/status",
        headers={TOKEN_HEADER: token},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else None


def _same_dir(left: object, right: object) -> bool:
    try:
        return Path(str(left)).resolve() == Path(str(right)).resolve()
    except (OSError, ValueError, RuntimeError):
        return False


def identity_conflict(state: dict, live: dict, data_dir: Path) -> tuple[int, str] | None:
    """“运行记录 + 端口上的服务”是否**就是这个数据目录**的实例。

    返回 ``(退出码, 原因)``；一致时返回 ``None``。判定只写在这一点，``start`` /
    ``status`` / ``stop`` 三个分支共用，避免各自漂移出一套口径：

    1. instance（令牌哈希前缀）与 PID 必须与记录一致——端口可能已被别的程序占用；
    2. 记录自带的 ``data_dir`` 必须是**本次解析出的目录**——拷贝来的记录指向原目录；
    3. 服务自报的 ``data_dir`` 也必须是同一个目录——防止记录被改过、或服务跑在别处。

    只要有一项对不上，就不能把它当成“我的实例”：否则 ``start`` 会把别人的轮次
    标成中断，``stop`` 会停掉别的数据目录里正在跑的服务。
    """
    if live.get("instance") != state.get("instance"):
        return 7, "端口上的服务不是这条运行记录启动的实例（instance 不一致）"
    record_pid = int(state.get("pid") or 0)
    live_pid = int(live.get("pid") or 0)
    if record_pid != live_pid:
        return 7, f"PID 不一致（记录 {record_pid}，服务自报 {live_pid}）"
    recorded = str(state.get("data_dir") or "").strip()
    actual = str(live.get("data_dir") or "").strip()
    if not recorded:
        return 4, "运行记录没有写数据目录，无法确认它属于本次目录（可能是旧记录或拷贝件）"
    if not actual:
        return 7, "服务没有自报数据目录，无法确认身份"
    if not _same_dir(recorded, data_dir):
        return 4, f"运行记录属于另一个数据目录：{recorded}（本次是 {data_dir}）"
    if not _same_dir(actual, data_dir):
        return 4, f"服务实际使用的数据目录是：{actual}（本次是 {data_dir}）"
    if not _same_dir(recorded, actual):
        return 4, f"记录（{recorded}）与服务自报（{actual}）不是同一个数据目录"
    return None


def request_shutdown(state: dict) -> bool:
    host = str(state.get("host") or DEFAULT_HOST)
    port = int(state.get("port") or 0)
    token = str(state.get("token") or "")
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    request = urllib.request.Request(
        f"http://{host}:{port}/api/service/shutdown",
        data=b"{}",
        method="POST",
        headers={TOKEN_HEADER: token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            response.read()
    except (urllib.error.URLError, OSError):
        return False
    return True


def port_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


# --------------------------------------------------------------------------- 环境检查


def _module_version(name: str) -> str:
    try:
        import importlib.metadata as metadata  # noqa: PLC0415

        return metadata.version(name)
    except Exception:
        return "?"


def check_backend() -> list[str]:
    problems: list[str] = []
    if sys.version_info < MIN_PYTHON:
        problems.append(
            f"Python 版本过低：{sys.version.split()[0]}，需要 {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+"
        )
    missing = []
    for module, package in BACKEND_MODULES:
        try:
            __import__(module)
        except Exception:
            missing.append(package)
    if missing:
        problems.append(
            "缺少后端依赖：" + "、".join(missing)
            + f"\n   → 在项目文件夹执行：\"{sys.executable}\" -m pip install -r backend/requirements.txt"
        )
    return problems


def check_waitress() -> list[str]:
    try:
        import waitress  # noqa: F401, PLC0415
    except Exception:
        return [
            "缺少 Waitress（对外启动用的 WSGI 服务器）"
            f"\n   → 执行：\"{sys.executable}\" -m pip install -r backend/requirements.txt"
        ]
    return []


def check_dist() -> list[str]:
    index = ROOT / "frontend" / "dist" / "index.html"
    if index.is_file():
        return []
    return [
        "缺少前端构建产物 frontend/dist/index.html"
        "\n   → 首次部署先双击「准备运行环境.cmd」，或在 frontend/ 执行 npm install && npm run build"
    ]


def node_version() -> tuple[int, ...] | None:
    exe = shutil.which("node")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=15, check=False
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    out = out.lstrip("v")
    parts: list[int] = []
    for chunk in out.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            break
    return tuple(parts) or None


def engine_hint() -> str:
    from backend import config  # noqa: PLC0415

    exe = config._resolve_engine_exe()
    if exe:
        return f"OpenCode：{exe}（版本闸门 {config.DEFAULT_ENGINE_VERSION}）"
    return (
        "OpenCode：未找到。网站可以启动，但对话功能会提示 ENGINE_MISSING；"
        "装好后把 opencode.exe 放进 PATH，或设置 HX17_ENGINE_EXE 指向它（建议 1.19.4）"
    )


# --------------------------------------------------------------------------- 子命令


def cmd_check(args) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    problems = check_backend() + check_waitress() + check_dist()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe_file = data_dir / ".write-probe"
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
    except OSError as exc:
        problems.append(f"数据目录不可写：{data_dir}（{exc}）")

    host = args.host or DEFAULT_HOST
    port = int(args.port or 0)
    print(f"Python：{sys.version.split()[0]}（{sys.executable}）")
    print(f"数据目录：{data_dir}")
    print(f"端口：{host}:{port}")
    print(f"前端产物：{ROOT / 'frontend' / 'dist'}")
    print(f"Waitress：{_module_version('waitress') if not check_waitress() else '缺失'}")
    node = node_version()
    if node:
        text = ".".join(str(part) for part in node)
        extra = "" if node >= NODE_FOR_LOGIC_TESTS else "（低于 22.6：npm run test:logic 不可用，构建仍可用）"
        print(f"Node：{text}{extra}")
    else:
        print("Node：未找到（只在准备阶段构建前端时需要，运行服务不需要）")
    print(engine_hint())

    if problems:
        print("\nCHECK FAILED：")
        for item in problems:
            print(f"- {item}")
        return 1
    print("\nCHECK OK：依赖、构建产物与数据目录都就绪。")
    return 0


def _run(command: list[str], cwd: Path | None = None) -> None:
    printable = " ".join(f'"{part}"' if " " in part else part for part in command)
    print(f"\n$ {printable}")
    result = subprocess.run(command, cwd=str(cwd or ROOT), check=False)
    if result.returncode != 0:
        raise SystemExit(f"命令失败（退出码 {result.returncode}）：{printable}")


def _sanitize_node_env() -> None:
    """清掉继承来的 NODE_OPTIONS。

    某些 IDE/终端会注入 ``--require <内置 shim>``；换到部署机后那个文件不存在，
    node/npm 会直接启动失败（报 MODULE_NOT_FOUND）。这只影响本进程的子进程，
    不改用户环境变量。
    """
    if os.environ.get("NODE_OPTIONS"):
        print("提示：已忽略继承来的 NODE_OPTIONS（它指向当前终端自己的 Node 插件）。")
        os.environ.pop("NODE_OPTIONS", None)


def cmd_prepare(args) -> int:
    """首次部署：装后端依赖、装前端依赖并构建。只在用户主动运行时执行。"""
    if sys.version_info < MIN_PYTHON:
        print(
            f"Python 版本过低：{sys.version.split()[0]}，需要 {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+。"
            "请安装新版本 Python 后重新运行本脚本。"
        )
        return 1
    if not args.skip_backend:
        _run([sys.executable, "-m", "pip", "install", "-r", "backend/requirements.txt"])
        if args.with_vision:
            print(
                "\n注意：视觉依赖体积大，且 torch/torchvision 必须与本机 CUDA/CPU 匹配。"
                "如果你的驱动不是 CUDA 12.x，请先按 PyTorch 官网选择 wheel 再安装。"
            )
            _run([sys.executable, "-m", "pip", "install", "-r", "backend/requirements-vision.txt"])
    if not args.skip_frontend:
        _sanitize_node_env()
        node = node_version()
        if node is None:
            print("未找到 Node.js：请安装 Node 22.6+（https://nodejs.org/）后重试。")
            return 1
        npm = shutil.which("npm.cmd") if os.name == "nt" else shutil.which("npm")
        if not npm:
            print("未找到 npm：请确认 Node.js 安装完整并把它的目录放进 PATH。")
            return 1
        if node < NODE_FOR_LOGIC_TESTS:
            print(
                f"提示：Node {'.'.join(map(str, node))} 低于 22.6，"
                "`npm run test:logic`（--experimental-strip-types）不可用；构建不受影响。"
            )
        if not (ROOT / "frontend" / "node_modules").is_dir():
            _run([npm, "install", "--no-audit", "--no-fund"], cwd=ROOT / "frontend")
        else:
            print("前端依赖已存在（frontend/node_modules），跳过 npm install。")
        _run([npm, "run", "build"], cwd=ROOT / "frontend")
    print("\n准备完成。之后双击「启动海巡智检.cmd」即可。")
    return 0


def cmd_status(args) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    state = load_state(data_dir)
    if not state:
        print(f"没有运行记录：{state_path(data_dir)}")
        return 0
    print(f"数据目录：{data_dir}")
    print(f"记录：PID {state.get('pid')} · {state.get('host')}:{state.get('port')} · 启动于 {state.get('started_at')}")
    live = probe(state)
    if not live:
        print("状态：未响应（进程可能已退出；不会结束任何进程）")
        return 3
    conflict = identity_conflict(state, live, data_dir)
    if conflict:
        code, reason = conflict
        print(f"状态：身份不一致（{reason}）")
        print(
            "这条运行记录不是本数据目录的实例：脚本不会把它报成“运行中”，"
            "也不会去停止它；请在它所属的数据目录里操作。"
        )
        return code
    print(f"状态：运行中 · PID {live.get('pid')} · 端口 {live.get('port')} · 数据目录 {data_dir}")
    return 0


class WorkStateUnknown(RuntimeError):
    """无法判断数据目录里还有没有进行中的工作。

    数据库不可读、损坏，或某条统计查询失败时抛这个，而不是返回空字典：
    “查不出来”和“确实没有任务”是两件不同的事，前者不能当作“可以随便停”。
    """


def active_work(data_dir: Path) -> dict[str, int]:
    """统计这个数据目录里还在进行的工作（只读查询，不改任何状态）。

    停止服务不该悄悄打断正在跑的对话、业务作业或训练。这里只看得到“有没有”，
    具体是谁由页面/日志呈现；是否强停由操作者用 ``--force`` 明确决定。

    打不开或读不懂数据库时抛 :class:`WorkStateUnknown`（调用方据此拒绝普通停止）。
    只以 ``mode=ro`` 只读连接查询：不迁移、不建表、不写任何一行。
    """
    db_path = data_dir / "haixun.db"
    if not db_path.is_file():
        # 全新数据目录还没有数据库，确实没有任务。
        return {}
    import sqlite3  # noqa: PLC0415

    counts: dict[str, int] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error as exc:
        raise WorkStateUnknown(f"打不开数据库 {db_path}（{exc}）") from exc
    try:
        queries = {
            "对话轮次": "SELECT COUNT(*) FROM turns WHERE status NOT IN ('completed', 'failed', 'stopped')",
            "业务作业": "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')",
            "训练任务": (
                "SELECT COUNT(*) FROM training_runs WHERE status IN ('queued', 'running')"
                " OR COALESCE(launch_state, '') IN ('preparing', 'launching')"
            ),
        }
        for label, sql in queries.items():
            try:
                row = conn.execute(sql).fetchone()
            except sqlite3.Error as exc:
                raise WorkStateUnknown(f"读取「{label}」状态失败（{exc}）") from exc
            value = int(row[0]) if row else 0
            if value:
                counts[label] = value
    finally:
        conn.close()
    return counts


def cmd_stop(args) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    state = load_state(data_dir)
    if not state:
        print("没有本实例的运行记录；不操作任何进程。")
        return 0
    live = probe(state)
    if not live:
        if _pid_alive(int(state.get("pid") or 0)):
            print(
                f"PID {state.get('pid')} 还活着，但服务没有响应。"
                "为了不误杀别的进程，脚本不会结束它；请人工确认后再处理。"
            )
            return 6
        print("记录已失效（进程不存在），清理运行记录。")
        remove_state(data_dir)
        return 0
    conflict = identity_conflict(state, live, data_dir)
    if conflict:
        code, reason = conflict
        print(f"拒绝停止：{reason}")
        print(
            "脚本不会结束身份未确认的进程；请在它所属的数据目录里停止，"
            "或人工确认后删除本目录的 run/server.json。"
        )
        return code
    if not args.force:
        try:
            busy = active_work(data_dir)
        except WorkStateUnknown as exc:
            print(
                f"无法确认是否还有进行中的任务：{exc}\n"
                "为免误停正在跑的对话/业务/训练，本次没有停止（也不会去修数据库）。\n"
                "确认服务可以停、且上面的错误已经处理的话，加 --force 再执行。"
            )
            return 10
        if busy:
            summary = "、".join(f"{name} {count} 个" for name, count in busy.items())
            print(
                f"还有正在进行的任务：{summary}。\n"
                "为免打断正在跑的对话/业务/训练，本次没有停止。\n"
                "确认要停止并接受“重启后标记为中断”的话，加 --force 再执行。"
            )
            return 9
    pid = int(live.get("pid") or 0)
    if not request_shutdown(state):
        print(f"已发出停止请求但没收到确认；请查看 PID {pid} 的控制台输出。")
        return 8
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if probe(state, timeout=1.0) is None and not _pid_alive(pid):
            remove_state(data_dir)
            print(f"已停止（PID {pid}）。")
            return 0
        time.sleep(0.3)
    print(f"已请求停止，但 PID {pid} 仍在退出中；稍后用 --status 复查。")
    return 0


def cmd_start(args) -> int:
    data_dir = resolve_data_dir(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    host = args.host or (os.environ.get("HX17_HOST") or DEFAULT_HOST).strip() or DEFAULT_HOST
    port = int(args.port or os.environ.get("HX17_PORT") or DEFAULT_PORT)

    existing = load_state(data_dir)
    if existing:
        live = probe(existing)
        if live:
            conflict = identity_conflict(existing, live, data_dir)
            if conflict:
                code, reason = conflict
                print(
                    "拒绝启动：这条运行记录（或端口上的服务）不属于本次数据目录。\n"
                    f"- {reason}\n"
                    f"- 记录文件：{state_path(data_dir)}\n"
                    "把别的目录的记录当成自己的实例会让两个进程争用同一份数据库，"
                    "并把对方正在跑的轮次标成中断，所以本次不启动、也不结束任何进程。\n"
                    "请回到它所属的数据目录操作，或人工确认后自行删除这条记录文件。"
                )
                return code
            if int(live.get("port") or 0) == port:
                print(
                    f"已在运行：http://{host}:{port}（PID {live.get('pid')}，数据目录 {data_dir}）\n"
                    "要停止请双击「停止海巡智检.cmd」，或运行 python deploy/serve.py --stop。"
                )
                return 0
            print(
                f"同一数据目录已有实例在运行（PID {live.get('pid')}，端口 {live.get('port')}）。\n"
                "请先停止它，或换一个 HX17_DATA_DIR + HX17_PORT 再启动。"
            )
            return 4
        old_pid = int(existing.get("pid") or 0)
        if _pid_alive(old_pid):
            print(
                f"运行记录里的 PID {old_pid} 仍然存在，但服务没有响应。\n"
                "同一数据目录再起一个实例会和它争用同一份数据库，并把对方正在跑的轮次标成中断；\n"
                "所以本次不启动，也不会结束那个进程。请人工确认它的状态：\n"
                f"- 已确认它不再是本服务 → 删除 {state_path(data_dir)} 后重试；\n"
                "- 它可能还在启动/卡住 → 先用 --status 复查，或换一个数据目录/端口。"
            )
            return 6
        print("上次的运行记录已失效（那个 PID 已不存在），清理记录后继续启动；不会结束任何进程。")
        remove_state(data_dir)

    problems = check_backend() + check_waitress() + check_dist()
    if problems:
        print("启动失败：")
        for item in problems:
            print(f"- {item}")
        return 1
    if not port_free(host, port):
        print(
            f"端口 {host}:{port} 已被占用。\n"
            "脚本不会结束未记录的进程：请换端口（--port 或 HX17_PORT），"
            "或确认占用者后自行处理。"
        )
        return 5

    token = secrets.token_hex(16)
    os.environ["HX17_DATA_DIR"] = str(data_dir)
    os.environ["HX17_PORT"] = str(port)
    os.environ["HX17_SERVE_DIST"] = "1"
    os.environ["HX17_SERVICE_TOKEN"] = token

    from backend.app import create_app  # noqa: PLC0415
    from waitress import create_server  # noqa: PLC0415

    try:
        app = create_app()
    except SystemExit as exc:  # 构建产物缺失等可预期问题
        print(f"启动失败：{exc}")
        return 1

    try:
        server = create_server(
            app,
            host=host,
            port=port,
            threads=int(os.environ.get("HX17_THREADS") or 8),
            ident="haixun-1.7",
            channel_timeout=300,
        )
    except OSError as exc:
        if exc.errno in {errno.EADDRINUSE, errno.EACCES, 10048, 10013}:
            print(
                f"端口 {host}:{port} 绑定失败（可能刚被别的程序占用）。"
                "脚本不会结束未记录的进程，请换端口或人工确认。"
            )
        else:
            print(f"WSGI 服务器创建失败：{exc}")
        return 5

    def request_stop() -> None:
        # 只置位，由主线程统一收尾（关闭端口、等 worker 退出、清运行记录）。
        stop_event.set()

    stop_event = threading.Event()

    app.config["HX17_STOP"] = request_stop
    state = {
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "instance": fingerprint(token),
        "token": token,
        "data_dir": str(data_dir),
        "version": app.config["HX17"].version,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": sys.executable,
    }
    path = write_state(data_dir, state)
    print(
        f"海巡智检 v{state['version']} 已启动：http://{host}:{port}\n"
        f"数据目录：{data_dir}\n运行记录：{path}\n"
        "关闭窗口或按 Ctrl+C 停止；也可以双击「停止海巡智检.cmd」。",
        flush=True,
    )

    failure: list[BaseException] = []

    def _serve() -> None:
        try:
            server.run()
        except BaseException as exc:  # noqa: BLE001 - 交给主线程统一报告
            failure.append(exc)

    worker = threading.Thread(target=_serve, name="waitress", daemon=True)
    worker.start()
    if os.name != "nt":
        # SIGTERM（systemd/docker 停止）也走同一条收尾逻辑，不做硬杀。
        try:
            signal.signal(signal.SIGTERM, signal.default_int_handler)
        except (ValueError, OSError):
            pass
    try:
        while worker.is_alive() and not stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        worker.join(timeout=10)
        current = load_state(data_dir)
        if current and current.get("pid") == os.getpid() and current.get("instance") == state["instance"]:
            remove_state(data_dir)
    if failure:
        print(f"WSGI 服务异常退出：{failure[0]!r}", file=sys.stderr)
        return 1
    print("服务已停止。", flush=True)
    return 0


# --------------------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="海巡智检 v1.8 同源启动器（Waitress WSGI，单端口）"
    )
    parser.add_argument("command", nargs="?", default="start", choices=["start", "check", "prepare", "status", "stop"])
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--check", action="store_true", help="等价于 check 子命令")
    parser.add_argument("--status", action="store_true", help="等价于 status 子命令")
    parser.add_argument("--stop", action="store_true", help="等价于 stop 子命令")
    parser.add_argument("--skip-backend", action="store_true", help="prepare 时跳过 pip 安装")
    parser.add_argument("--skip-frontend", action="store_true", help="prepare 时跳过 npm 安装与构建")
    parser.add_argument("--with-vision", action="store_true", help="prepare 时一并安装视觉依赖（torch 等）")
    parser.add_argument(
        "--force", action="store_true", help="stop 时即使还有进行中的对话/业务/训练也停止"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.check:
        args.command = "check"
    elif args.status:
        args.command = "status"
    elif args.stop:
        args.command = "stop"
    handlers = {
        "start": cmd_start,
        "check": cmd_check,
        "prepare": cmd_prepare,
        "status": cmd_status,
        "stop": cmd_stop,
    }
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    except SystemExit as exc:
        print(f"\n{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

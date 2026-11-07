"""MCP 传输层：只实现 ``initialize`` / ``tools/list`` / ``tools/call`` 三件事。

两种传输，都有**硬边界**：

* **stdio**：``subprocess.Popen([command, *args])``，参数是列表、``shell=False``，
  所以配置里的引号、分号、管道都不会被 shell 解释；子进程拿到的是**最小环境**
  （不是服务进程的完整环境），再叠加用户显式配置的 env。
* **http**：走**官方 Python SDK**（``mcp`` 包，``streamablehttp_client`` +
  ``ClientSession``）——``Mcp-Session-Id`` 与 ``MCP-Protocol-Version`` 的协商、
  SSE 流上"按请求 ID 取最终响应"都由 SDK 按真实协议完成，不再手写。
  同步侧用 anyio blocking portal 桥接。SDK 默认 httpx 客户端会跟随重定向、
  读系统代理，这里**显式关掉**（自定义 factory），沿用旧边界：
  只带用户配置的 header、不跟随重定向、不读系统代理。

共同约束：

* 总时长有上限（每轮调用都包在 ``anyio.fail_after`` 里），不会无限等；
* 任何异常都带上「哪一步失败」，由上层决定报错文案；
* 这里**不判断授权**——授权在 ``services/mcp.py``，传输层只管发消息。

``_parse_sse`` 保留为**严格**工具函数（SSE 文本里没有匹配请求 ID 的响应
就抛错，绝不把别的消息当结果）——供诊断与测试使用；生产 http 路径由 SDK
按协议匹配响应，不需要它。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_STDERR_BYTES = 8 * 1024

# 官方 MCP Python SDK（已用于私有 MCP 桥）。缺它时 http 传输显式报错，
# 绝不静默回退到手写协议（手写版正是 session/version 协商失败的反例来源）。
try:  # pragma: no cover - 环境探测
    import anyio
    import httpx
    from anyio.from_thread import BlockingPortal, start_blocking_portal
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.shared.exceptions import McpError

    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - requirements.txt 未装时
    _SDK_AVAILABLE = False

# 子进程只需要这些才能正常跑起来：PATH 用于查找动态库，TEMP/HOME 用于临时文件。
_ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
    "PYTHONIOENCODING",
)


class McpTransportError(Exception):
    """传输层失败：``step`` 说明卡在哪一步，``detail`` 已由上层脱敏。"""

    def __init__(self, code: str, message: str, *, step: str = "") -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.step = str(step)


def _deadline(timeout: float) -> float:
    return time.monotonic() + max(1.0, float(timeout))


def _remaining(deadline: float, *, step: str) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise McpTransportError("MCP_TIMEOUT", f"{step} 超时：外部服务没有在限定时间内回应", step=step)
    return left


@dataclass
class StdioSession:
    command: str
    args: list[str]
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    timeout: float = 20.0

    _process: subprocess.Popen | None = None
    _lines: queue.Queue = field(default_factory=queue.Queue, init=False)
    _stderr: list[str] = field(default_factory=list, init=False)
    _next_id: int = 0
    _closed: bool = False

    # ------------------------------------------------------------------ 生命周期

    def open(self) -> None:
        child_env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
        # JSON-RPC over stdio 的消息必须是 UTF-8。Windows 上 Python 子进程往管道写
        # stdout 默认用本地代码页（GBK），父进程按 UTF-8 解码就会乱码——这里给
        # 子进程补上协议要求的编码声明。用户显式配置的 env 优先级更高。
        child_env.setdefault("PYTHONIOENCODING", "utf-8")
        child_env.update({str(key): str(value) for key, value in self.env.items()})
        creation = 0
        if os.name == "nt":  # 子进程不弹黑窗
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._process = subprocess.Popen(  # noqa: S603 - 参数列表、shell=False
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd,
                env=child_env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                creationflags=creation,
            )
        except FileNotFoundError:
            raise McpTransportError(
                "MCP_SPAWN_FAILED", f"找不到可执行文件：{self.command}", step="spawn"
            ) from None
        except OSError as exc:
            raise McpTransportError(
                "MCP_SPAWN_FAILED", f"启动失败（{type(exc).__name__}）", step="spawn"
            ) from None
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            if len(line.encode("utf-8", "replace")) > MAX_MESSAGE_BYTES:
                self._lines.put(None)  # 超长行直接判死，不拼进内存
                return
            self._lines.put(line)
        self._lines.put(None)

    def _pump_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        total = 0
        for line in self._process.stderr:
            total += len(line)
            if total > MAX_STDERR_BYTES:
                return
            self._stderr.append(line.rstrip("\n"))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:
            pass
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:
                pass
        # 管道要显式关：否则 Windows 上会留下未关闭的句柄与 ResourceWarning。
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    def __enter__(self) -> "StdioSession":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------ 收发

    def stderr_tail(self) -> str:
        return " | ".join(self._stderr[-5:])[:600]

    def send(self, payload: dict) -> None:
        assert self._process is not None and self._process.stdin is not None
        try:
            self._process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise McpTransportError(
                "MCP_PIPE_CLOSED", f"连接已断开（{type(exc).__name__}）", step="write"
            ) from None

    def notify(self, method: str, params: dict | None = None) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict, *, deadline: float) -> dict:
        self._next_id += 1
        request_id = self._next_id
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        while True:
            left = _remaining(deadline, step=method)
            try:
                line = self._lines.get(timeout=left)
            except queue.Empty:
                raise McpTransportError(
                    "MCP_TIMEOUT", f"{method} 超时：外部服务没有在限定时间内回应", step=method
                ) from None
            if line is None:
                tail = self.stderr_tail()
                raise McpTransportError(
                    "MCP_PROCESS_EXITED",
                    "外部进程已退出，没有返回结果" + (f"（stderr: {tail}）" if tail else ""),
                    step=method,
                )
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue  # 服务端往 stdout 打日志是常见现象，忽略非 JSON 行
            if not isinstance(message, dict):
                continue
            if message.get("id") != request_id:
                continue  # 不是本次请求的响应（通知/上一次的超时响应）
            if "error" in message:
                error = message.get("error") or {}
                raise McpTransportError(
                    "MCP_PROTOCOL_ERROR",
                    str(error.get("message") or "外部服务返回了错误")[:500],
                    step=method,
                )
            result = message.get("result")
            return result if isinstance(result, dict) else {"value": result}


def _safe_http_client(headers: dict[str, str] | None = None, timeout=None, auth=None):
    """SDK 默认 httpx 客户端会跟随重定向、读系统代理——这里显式关掉。

    边界与旧手写实现一致：不跟随重定向（跳转到别的主机会把凭据带走）、
    不读系统代理变量；只带用户配置的 header。
    """
    return httpx.AsyncClient(  # noqa: S113 - timeout 由 SDK 注入
        headers=headers,
        timeout=timeout,
        auth=auth,
        follow_redirects=False,
        trust_env=False,
    )


def _cancelled_exc_classes() -> tuple[type[BaseException], ...]:
    """取"当前可能是取消异常"的类集合，**绝不抛错**。

    ``anyio.get_cancelled_exc_class()`` 依赖 sniffio 探测当前线程的事件循环——
    同步上下文（无循环）里它自己会抛 RuntimeError（NoEventLoopError），这正是
    ``_map_sdk_error`` 掩盖真实连接失败的反例。所以这里三路兜底：
    asyncio 的取消类（portal worker 跑的就是 asyncio）、anyio 能问就问、
    trio 装了也认。问不出来就用 asyncio 的，映射永远不会因为探测失败而失败。
    """
    classes: list[type[BaseException]] = [asyncio.CancelledError]
    if _SDK_AVAILABLE:
        try:
            classes.append(anyio.get_cancelled_exc_class())
        except Exception:  # noqa: BLE001 - 探测失败不允许破坏错误映射本身
            pass
    try:
        import trio  # noqa: PLC0415 - 可选后端，装了才认

        classes.append(trio.Cancelled)
    except ImportError:
        pass
    return tuple(set(classes))


try:  # Python 3.11+ 内置 ExceptionGroup。
    _GROUP_TYPES: tuple[type[BaseException], ...] = (BaseExceptionGroup,)  # noqa: F821
except NameError:  # pragma: no cover - Python 3.10
    # 3.10 上 anyio 4 会依赖 exceptiongroup backport（随 mcp 一起装），它抛出的组
    # 类型不是内置名，但语义一样；能导入就一起展平，导入不到才退化成“不展平”。
    try:
        from exceptiongroup import BaseExceptionGroup as _BackportGroup  # noqa: PLC0415

        _GROUP_TYPES = (_BackportGroup,)
    except ImportError:
        _GROUP_TYPES = ()


def _flatten_group(exc: BaseException) -> list[BaseException]:
    """把 anyio 4 / Python 3.11+ 任务组包出的 ExceptionGroup 展平成成员列表。"""
    if _GROUP_TYPES and isinstance(exc, _GROUP_TYPES):
        members: list[BaseException] = []
        for member in exc.exceptions:
            members.extend(_flatten_group(member))
        return members
    return [exc]


def _map_sdk_error(exc: BaseException, step: str) -> McpTransportError:
    """把 SDK/httpx 的异常统一翻译成带步骤的传输错误（上层只认这一种）。

    本函数在任何上下文（无事件循环 / 有事件循环 / 真实连接异常）都必须
    稳定返回一个 ``McpTransportError``，绝不在映射过程中自己抛错。
    任务组会把底层真实错误（如 ConnectError）包进 ExceptionGroup、外面包着
    取消——先展平再挑出**非取消**的那个成员，"连不上"就如实映射成
    ``MCP_UNREACHABLE``，而不是笼统的"连接被中断"。
    """
    if isinstance(exc, McpTransportError):
        return exc
    members = _flatten_group(exc)
    meaningful = [item for item in members if not isinstance(item, _cancelled_exc_classes())]
    if meaningful:
        exc = meaningful[0]
    if isinstance(exc, TimeoutError):
        return McpTransportError(
            "MCP_TIMEOUT", f"{step} 超时：外部服务没有在限定时间内回应", step=step
        )
    if isinstance(exc, _cancelled_exc_classes()):
        # 展平后仍然全是取消：原始异常确实拿不到（对端中途崩溃等），如实说。
        return McpTransportError(
            "MCP_CONNECTION_CLOSED",
            "连接被中断：外部服务连不上、被对端关闭或在请求中途崩溃",
            step=step,
        )
    if _SDK_AVAILABLE and isinstance(exc, McpError):
        detail = getattr(exc, "error", None)
        message = str(getattr(detail, "message", "") or exc)[:500]
        raw_code = getattr(detail, "code", None)
        if raw_code not in (None, ""):
            # 保留远端的原始错误码，上层与日志能对上号。
            message = f"{message}（code={raw_code}）"
        return McpTransportError("MCP_PROTOCOL_ERROR", message, step=step)
    if _SDK_AVAILABLE and isinstance(exc, httpx.HTTPStatusError):
        return McpTransportError(
            "MCP_HTTP_STATUS", f"外部服务返回 HTTP {exc.response.status_code}", step=step
        )
    if _SDK_AVAILABLE and isinstance(exc, httpx.TimeoutException):
        return McpTransportError(
            "MCP_TIMEOUT", f"{step} 超时：外部服务没有在限定时间内回应", step=step
        )
    if _SDK_AVAILABLE and isinstance(exc, httpx.ConnectError):
        return McpTransportError(
            "MCP_UNREACHABLE", f"连不上外部服务（{type(exc).__name__}）", step=step
        )
    return McpTransportError(
        "MCP_BAD_RESPONSE", f"外部服务交互失败（{type(exc).__name__}）", step=step
    )


class HttpSession:
    """官方 SDK（streamable-http）驱动的同步会话。

    实现要点：``streamablehttp_client`` / ``ClientSession`` 的上下文必须在
    **同一个任务**里进出（anyio 的 cancel scope 不允许跨任务），所以
    ``open()`` 在 portal 的事件循环里起一个常驻 worker 任务持有全部上下文，
    同步侧通过线程收件箱/发件箱与之通信——每个请求都在 worker 任务里
    包 ``anyio.fail_after``，超时抛 ``MCP_TIMEOUT``。

    * ``Mcp-Session-Id``（有状态服务）与 ``MCP-Protocol-Version`` 的协商由
      SDK 的 ``initialize()`` 真实完成——这正是旧手写版"有状态 HTTP 服务
      发现失败"反例的修复；
    * ``request()`` 只认 ``initialize`` / ``tools/list`` / ``tools/call``，
      结果按 wire 格式（camelCase 键）导出给服务层；
    * SSE 上"按请求 ID 取最终响应"由 SDK 按协议匹配（诊断用的严格
      ``_parse_sse`` 保留在模块层）；
    * ``close()`` 先通知 worker 退出（让上下文在原任务里收尾），再停 portal；
      清理路径里任何一步失败都吞掉。
    """

    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout: float = 20.0):
        self.url = str(url)
        self.headers = {str(key): str(value) for key, value in (headers or {}).items()}
        self.timeout = max(1.0, float(timeout))
        self._portal: BlockingPortal | None = None
        self._portal_cm = None
        self._inbox: queue.Queue | None = None
        self._outbox: queue.Queue | None = None
        self._worker = None
        self._closing = threading.Event()
        self._init_result = None
        self._next_id = 0

    # ------------------------------------------------------------------ 生命周期

    def open(self) -> None:
        if not _SDK_AVAILABLE:
            raise McpTransportError(
                "MCP_SDK_MISSING",
                "官方 MCP Python SDK 没有安装（backend/requirements.txt 里的 mcp）",
                step="open",
            )
        if self._portal is not None:
            return
        self._closing.clear()
        inbox: queue.Queue = queue.Queue()
        outbox: queue.Queue = queue.Queue()
        self._inbox = inbox
        self._outbox = outbox
        try:
            # anyio 4：start_blocking_portal() 返回上下文管理器，__enter__ 起线程
            # 并返回 portal；close() 里用对应的 __exit__ 收线程。
            self._portal_cm = start_blocking_portal()
            self._portal = self._portal_cm.__enter__()
        except Exception as exc:
            self._inbox = self._outbox = None
            raise _map_sdk_error(exc, "open") from None

        def _take_item():
            """worker 侧取请求（阻塞在专用线程里，绝不卡事件循环）。"""
            while True:
                try:
                    return inbox.get(timeout=0.5)
                except queue.Empty:
                    if self._closing.is_set():
                        return None

        async def _worker():
            current_rid = 0
            try:
                async with streamablehttp_client(
                    self.url,
                    headers=dict(self.headers),
                    timeout=self.timeout,
                    sse_read_timeout=self.timeout,
                    httpx_client_factory=_safe_http_client,
                ) as (read_stream, write_stream, _get_session_id):
                    async with ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=datetime.timedelta(seconds=self.timeout),
                    ) as session:
                        with anyio.fail_after(self.timeout):
                            init_result = await session.initialize()
                        outbox.put((0, None, init_result))
                        while True:
                            item = await anyio.to_thread.run_sync(_take_item, cancellable=False)
                            if item is None:
                                return
                            rid, method, params = item
                            current_rid = rid
                            try:
                                with anyio.fail_after(self.timeout):
                                    result = await self._dispatch(session, method, params)
                                outbox.put((rid, None, result))
                            except Exception as exc:  # noqa: BLE001 - 经收件箱回传
                                # 普通异常（超时/协议错误）就地回传；**取消类不在这里接**：
                                # 让它穿出两层 __aexit__，任务组才有机会把底层连接错误
                                # 打包成 ExceptionGroup 还给外层（否则原始错误永远丢失）。
                                outbox.put((rid, exc, None))
            except BaseException as exc:  # noqa: BLE001 - 经收件箱回传
                try:
                    outbox.put((current_rid, exc, None))
                except Exception:  # noqa: BLE001 - portal 已停，收件箱无人接
                    pass

        self._worker = self._portal.start_task_soon(_worker)
        try:
            _tag, exc, result = self._collect(0, wait=self.timeout + 5.0, step="initialize")
            self._init_result = result
        except McpTransportError:
            self.close()
            raise

    def close(self) -> None:
        portal, self._portal = self._portal, None
        portal_cm, self._portal_cm = getattr(self, "_portal_cm", None), None
        if portal is None:
            return
        self._closing.set()
        inbox, self._inbox = self._inbox, None
        worker, self._worker = self._worker, None
        try:
            if inbox is not None:
                inbox.put(None)
        except Exception:
            pass
        try:
            if worker is not None:
                worker.result(timeout=10)
        except Exception:
            pass
        try:
            if portal_cm is not None:
                portal_cm.__exit__(None, None, None)
            else:
                portal.stop()
        except Exception:
            pass
        self._outbox = None

    def __enter__(self) -> "HttpSession":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------ 收发

    def _collect(self, rid: int, *, wait: float, step: str):
        """等本次请求（rid）的回执；顺带丢弃历史超时请求留下的陈旧回执。"""
        assert self._outbox is not None
        deadline = time.monotonic() + wait
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise McpTransportError(
                    "MCP_TIMEOUT", f"{step} 超时：外部服务没有在限定时间内回应", step=step
                )
            try:
                tag, exc, result = self._outbox.get(timeout=left)
            except queue.Empty:
                continue
            if tag != rid:
                continue  # 陈旧回执（早前已放弃的请求）：丢弃
            if exc is not None:
                raise _map_sdk_error(exc, step) from None
            return tag, exc, result

    def notify(self, method: str, params: dict | None = None) -> None:
        # SDK 的 initialize() 已经发过 notifications/initialized；这里保持 no-op。
        return None

    def _require_worker(self, step: str) -> None:
        if self._portal is None or self._inbox is None:
            raise McpTransportError("MCP_NOT_OPEN", "会话还没有打开", step=step)

    async def _dispatch(self, session: "ClientSession", method: str, params: dict):
        if method == "initialize":
            # open() 已完成真实协商；这里返回协商结果，不再重发。
            return self._init_result
        if method == "tools/list":
            return await session.list_tools(cursor=params.get("cursor"))
        if method == "tools/call":
            return await session.call_tool(
                str(params.get("name") or ""), dict(params.get("arguments") or {})
            )
        raise McpTransportError(
            "MCP_UNSUPPORTED_METHOD",
            f"本网关只支持 initialize / tools/list / tools/call，不支持 {method}",
            step=method,
        )

    def request(self, method: str, params: dict, *, deadline: float | None = None) -> dict:
        self._require_worker(method)
        budget = self.timeout if deadline is None else max(0.5, deadline - time.monotonic())
        self._next_id += 1
        rid = self._next_id
        assert self._inbox is not None
        self._inbox.put((rid, method, params or {}))
        _tag, _exc, result = self._collect(
            rid, wait=budget + 5.0, step=method
        )
        if isinstance(result, dict):
            return result
        # pydantic 模型 → wire 格式（camelCase 键，与服务层旧契约一致）。
        return result.model_dump(by_alias=True, exclude_none=True)


def _parse_sse(text: str, request_id: Any) -> dict:
    """严格版：SSE 文本里必须找到**与请求 ID 匹配**的 JSON-RPC 响应。

    没有匹配项就直接抛错——绝不把别的 id 的消息、通知或错误响应当成
    本次结果（这正是"错误 SSE 响应 ID 被当成结果"反例的修复语义）。
    生产 http 路径由官方 SDK 按协议匹配；此函数供诊断与回归测试使用。
    """
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data_lines = [line[5:].strip() for line in block.split("\n") if line.startswith("data:")]
        if not data_lines:
            continue
        try:
            message = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("id") == request_id:
            return message
    raise McpTransportError(
        "MCP_BAD_RESPONSE", "流式响应里没有匹配请求 ID 的 JSON-RPC 结果", step="http"
    )

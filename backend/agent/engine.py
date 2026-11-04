"""OpenCode 引擎适配器。

职责只有一件事：把「一个用户 + 一个项目 + 一份个人连接」映射成一个**受控**的 opencode
子进程，并通过它自己的 HTTP API 驱动真实的模型循环、规划与工具调用。

关键约束：

* 版本固定（默认 1.19.4）。裸跑 ``opencode`` 会去读写用户全局配置并报 EEXIST，
  因此**每次启动都先注入本工程私有 XDG 目录**，连 ``--version`` 也不例外；
* 只绑 ``127.0.0.1`` 随机端口，随机基础认证口令，关闭分享、自动更新、模型自动下载、
  无关插件与外部项目配置；
* 最小环境：显式白名单，只注入本人个人连接的 base_url / key，不继承其他服务凭据；
* 模型配置通过 ``OPENCODE_CONFIG_CONTENT`` 以进程环境传递，不落盘、不进日志；
* 换连接（指纹变化）会先停掉旧进程并清掉它的私有目录，旧引擎立即失效。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from backend import config as config_module
from backend import security

ENGINE_AUTH_USER = "opencode"
PROMPT_TIMEOUT = (5.0, 30.0)
QUERY_TIMEOUT = (3.0, 15.0)

# 原生 opencode 工具一律关闭：Windows 上无法用 cwd 证明“读不到目录之外”，
# 所以文件读写全部经私有 MCP 工具，由服务端做真实路径校验。
_DENIED_PERMISSIONS = {
    "bash": "deny",
    "edit": "deny",
    "read": "deny",
    "glob": "deny",
    "grep": "deny",
    "list": "deny",
    "webfetch": "deny",
    "websearch": "deny",
    "task": "deny",
    "skill": "deny",
    "question": "deny",
    "external_directory": "deny",
    "lsp": "deny",
    "doom_loop": "deny",
    "todowrite": "deny",
    "hx17_*": "allow",
}


class EngineUnavailable(RuntimeError):
    """引擎不可用。``code`` 稳定，便于界面给出可执行提示而不是“未知错误”。

    ``status`` 只在引擎**明确回了一个 HTTP 状态码**时才有值。调用方必须靠它区分
    “这个会话在引擎里确实不存在（404）”和“网络/超时导致的不确定”，
    后者绝不能被当成 404 去重建会话并重发。
    """

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class EngineSpec:
    """一份“引擎进程要用的全部输入”。

    ``tool_token`` 是**会话级**令牌：同一会话的连续多轮共用一个引擎进程，
    因此令牌不能按轮签发（否则第二轮会拿着第一轮已撤销的令牌）。
    真正的轮次归属在工具调用时由网关按会话实时解析。
    """

    user_id: int
    project_id: str
    session_id: str
    root: Path
    fingerprint: str
    provider: str
    model: str
    models: list[str]
    base_url: str
    api_key: str
    gateway_url: str
    tool_token: str
    python: str
    vision: bool = False
    repo_root: str = str(config_module.PROJECT_ROOT)

    def key(self) -> tuple[int, str, str, bool]:
        return (
            int(self.user_id),
            str(self.session_id),
            str(self.fingerprint),
            bool(self.vision),
        )


@dataclass
class EngineHandle:
    port: int
    password: str
    process: Any = None
    work_dir: Path | None = None
    version: str = ""


@dataclass
class EngineInstance:
    spec: EngineSpec
    handle: EngineHandle
    base_url: str
    started_at: float
    last_used: float = field(default_factory=time.monotonic)
    version: str = ""

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def alive(self) -> bool:
        process = self.handle.process
        if process is None:
            return True
        return getattr(process, "poll", lambda: None)() is None


Spawner = Callable[[EngineSpec, Path], EngineHandle]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _creation_flags() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    return 0


def _safe_rmtree(path: Path) -> None:
    for _ in range(5):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.2)


class EngineManager:
    """引擎进程池：按 (用户, 项目, 连接指纹) 复用，指纹变化即淘汰旧进程。"""

    def __init__(
        self,
        settings: config_module.Settings,
        *,
        spawner: Spawner | None = None,
        tokens=None,
        auth_user: str = ENGINE_AUTH_USER,
    ) -> None:
        if spawner is not None and not settings.allow_fake_engine:
            # 生产环境必须用真实 opencode 进程；假引擎只在显式开启的测试配置里可用。
            raise RuntimeError("allow_fake_engine 未开启时不允许注入替代引擎")
        self._settings = settings
        self._spawner = spawner or self._spawn_real
        self._tokens = tokens
        self._auth_user = auth_user
        self._lock = threading.RLock()
        self._instances: dict[tuple[int, str, str, bool], EngineInstance] = {}
        self._version_cache: dict[str, str] = {}
        self.root_dir = settings.data_dir / "engines"
        self.root_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 生命周期

    def get(self, spec: EngineSpec) -> EngineInstance:
        key = spec.key()
        with self._lock:
            self._evict_stale(spec)
            existing = self._instances.get(key)
            if existing is not None:
                state = self._token_state(existing)
                if state == "revoked":
                    # 授权被**明确撤销**（换连接、停账号、删会话、撤权）时不能
                    # 偷偷换一张新令牌继续干活——那等于把安全结论当成一次超时。
                    self._instances.pop(key, None)
                    self._dispose(existing, remove_dir=True)
                    raise EngineUnavailable(
                        "SESSION_REVOKED",
                        "该会话的工具授权已被撤销，需要重新发起对话",
                    )
                if state == "active" and existing.alive() and self._healthy(existing):
                    existing.touch()
                    return existing
                # 令牌过期 / 进程没了 / 健康检查不过：重建进程，但**保留私有存储**，
                # 让这个会话的 Native 历史还能接着用（重建后由运行器校验会话是否真的在）。
                self._dispose(existing, remove_dir=False)
                self._instances.pop(key, None)
            instance = self._start(spec)
            self._instances[key] = instance
            return instance

    def _token_state(self, instance: EngineInstance) -> str:
        """缓存里的引擎还能不能继续用同一张工具令牌。"""
        tokens = self._tokens
        if tokens is None:
            return "active"
        checker = getattr(tokens, "token_state", None)
        token = instance.spec.tool_token
        if checker is None:
            # 兼容只实现 binding_for 的注入对象：只区分“还在/不在了”。
            alive = getattr(tokens, "binding_for", None)
            if alive is None:
                return "active"
            return "active" if alive(token) is not None else "expired"
        return str(checker(token))

    def storage_key(self, spec: EngineSpec) -> str:
        """这份引擎私有存储（XDG/HOME/DB/日志）的代次标识。

        **只跟“谁的哪个会话 + 哪份连接”有关，与视觉能力无关**：视觉结论只影响
        下发给引擎的模型配置，换结论需要换进程，但没必要换存储——换了就等于把这个
        会话的 Native 历史整个丢掉。同一代存储 = Native 会话 ID 仍然有意义。
        """
        digest = hashlib.sha256(
            f"{spec.user_id}:{spec.session_id}:{spec.fingerprint}".encode("utf-8")
        ).hexdigest()[:16]
        return digest

    def quarantine(self, instance: EngineInstance) -> None:
        """隔离一台状态不确定的引擎：切断旧调用能力，但保留存储。

        abort 之后引擎没有确认空闲时，旧进程里可能还有正在跑的工具调用；
        把它交给下一轮会让旧调用被记到新任务上。直接结束进程并撤销令牌，
        是唯一能保证“旧调用不会再产生副作用”的做法。存储目录保留，
        下一轮重建后历史仍可复用。
        """
        with self._lock:
            self._instances.pop(instance.spec.key(), None)
            self._dispose(instance, remove_dir=False)

    def _evict_stale(self, spec: EngineSpec) -> None:
        """同一会话换了连接或能力结论，旧进程不能继续服务新身份。"""
        stale = [
            candidate
            for candidate in self._instances.values()
            if candidate.spec.user_id == spec.user_id
            and candidate.spec.session_id == spec.session_id
            and (
                candidate.spec.fingerprint != spec.fingerprint
                or bool(candidate.spec.vision) != bool(spec.vision)
            )
        ]
        for candidate in stale:
            self._instances.pop(candidate.spec.key(), None)
            # 视觉结论变化只是换进程，存储（也就是 Native 历史）必须留着，
            # 否则用户每做一次能力检查就会被清空一次上下文。
            same_storage = self.storage_key(candidate.spec) == self.storage_key(spec)
            self._dispose(candidate, remove_dir=not same_storage)

    def stop_user(self, user_id: int) -> int:
        with self._lock:
            targets = [item for item in self._instances.values() if item.spec.user_id == int(user_id)]
            for item in targets:
                self._instances.pop(item.spec.key(), None)
                self._dispose(item, remove_dir=True)
            return len(targets)

    def stop_project(self, user_id: int, project_id: str) -> int:
        with self._lock:
            targets = [
                item
                for item in self._instances.values()
                if item.spec.user_id == int(user_id) and item.spec.project_id == str(project_id)
            ]
            for item in targets:
                self._instances.pop(item.spec.key(), None)
                self._dispose(item, remove_dir=True)
            return len(targets)

    def stop_session(self, session_id: str) -> int:
        with self._lock:
            targets = [
                item for item in self._instances.values() if item.spec.session_id == str(session_id)
            ]
            for item in targets:
                self._instances.pop(item.spec.key(), None)
                self._dispose(item, remove_dir=True)
            return len(targets)

    def sweep_idle(self, max_idle_seconds: int | None = None) -> int:
        # 注意不能用 `or`：0 是合法入参（“立刻回收全部空闲引擎”），会被当成没传。
        limit = (
            int(max_idle_seconds)
            if max_idle_seconds is not None
            else int(self._settings.engine_idle_seconds)
        )
        now = time.monotonic()
        with self._lock:
            stale = [
                item
                for item in self._instances.values()
                if now - item.last_used > limit or not item.alive()
            ]
            for item in stale:
                self._instances.pop(item.spec.key(), None)
                # 只回收进程，**不删存储**：删掉就等于把这个会话的 Native 历史一起扔了，
                # 下一轮只能在空会话里重来，用户会看到助手突然“失忆”。
                self._dispose(item, remove_dir=False)
            return len(stale)

    def shutdown(self) -> None:
        with self._lock:
            for item in list(self._instances.values()):
                self._dispose(item, remove_dir=False)
            self._instances.clear()

    def active_count(self) -> int:
        return len(self._instances)

    # ------------------------------------------------------------------ 启动

    def _instance_dir(self, spec: EngineSpec) -> Path:
        return self.root_dir / self.storage_key(spec)

    def _env(self, spec: EngineSpec, directories: dict[str, Path], password: str) -> dict[str, str]:
        base = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
            "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
            "COMSPEC": os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
            "TEMP": str(directories["tmp"]),
            "TMP": str(directories["tmp"]),
            "TMPDIR": str(directories["tmp"]),
            "HOME": str(directories["home"]),
            "USERPROFILE": str(directories["home"]),
            "XDG_CONFIG_HOME": str(directories["config"]),
            "XDG_CACHE_HOME": str(directories["cache"]),
            "XDG_DATA_HOME": str(directories["data"]),
            "XDG_STATE_HOME": str(directories["state"]),
            "OPENCODE_SERVER_PASSWORD": password,
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
            "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE": "1",
            "OPENCODE_DISABLE_EMBEDDED_WEB_UI": "1",
            # 用户项目里可能有 .opencode/ 或 opencode.json：它们能覆盖 provider、MCP、
            # 权限与插件。1.19.4 支持这个开关（本机二进制已确认包含），必须打开。
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_CONFIG_CONTENT": self._config_content(spec),
            # 只给本工程自己的变量名，不注入用户 shell 里的其他凭据。
            "HX17_GATEWAY_URL": spec.gateway_url,
            "HX17_TOOL_TOKEN": spec.tool_token,
        }
        return base

    def _config_content(self, spec: EngineSpec) -> str:
        # 只有“检查连接”用真实图像挑战验证过**这个模型**才声明图像输入。
        # 本机 1.19.4 的 provider 解析对未知模型默认 image=false，不能靠猜；
        # 也不能把某一个模型的能力写成整份配置的默认值——配置里列了多个候选模型，
        # 逐个按“是不是这一轮要用的模型”来声明。
        def describe(name: str) -> dict:
            vision = bool(spec.vision) and str(name) == str(spec.model)
            return {
                "name": name,
                "attachment": vision,
                "tool_call": True,
                "modalities": {
                    "input": ["text", "image"] if vision else ["text"],
                    "output": ["text"],
                },
            }

        models = {name: describe(name) for name in spec.models if name}
        models.setdefault(spec.model, describe(spec.model))
        bridge = Path(spec.repo_root) / "backend" / "agent" / "mcp_bridge.py"
        payload = {
            "$schema": "https://opencode.ai/config.json",
            "share": "disabled",
            "autoupdate": False,
            "permission": dict(_DENIED_PERMISSIONS),
            "provider": {
                "hx17": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "海巡个人连接",
                    "options": {"baseURL": spec.base_url, "apiKey": spec.api_key},
                    "models": models,
                }
            },
            "mcp": {
                "hx17": {
                    "type": "local",
                    # 用**绝对脚本路径**启动桥：引擎的 cwd 是用户项目目录，
                    # 若用 `-m backend.agent.mcp_bridge`，项目里的同名 backend/ 会抢先导入。
                    "command": [spec.python, "-u", str(bridge)],
                    "environment": {
                        "HX17_GATEWAY_URL": spec.gateway_url,
                        "HX17_TOOL_TOKEN": spec.tool_token,
                        "PYTHONUNBUFFERED": "1",
                        "PYTHONIOENCODING": "utf-8",
                    },
                    "enabled": True,
                    "timeout": 60000,
                }
            },
        }
        return json.dumps(payload, ensure_ascii=False)

    def _spawn_real(self, spec: EngineSpec, work_dir: Path) -> EngineHandle:
        exe = Path(self._settings.engine_exe)
        if not exe.is_file():
            raise EngineUnavailable(
                "ENGINE_MISSING",
                "找不到本机 OpenCode 可执行文件，请设置 HX17_ENGINE_EXE 指向 opencode.exe",
            )
        for name in ("home", "config", "cache", "data", "state", "tmp", "logs"):
            (work_dir / name).mkdir(parents=True, exist_ok=True)
        directories = {name: work_dir / name for name in
                       ("home", "config", "cache", "data", "state", "tmp", "logs")}
        password = self._new_password()
        env = self._env(spec, directories, password)
        version = self._check_version(exe, env)
        port = _free_port()
        log_path = directories["logs"] / "engine.log"
        log_handle = open(log_path, "ab", buffering=0)
        try:
            process = subprocess.Popen(
                [str(exe), "serve", "--port", str(port), "--hostname", "127.0.0.1"],
                cwd=str(spec.root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=_creation_flags(),
                close_fds=True,
            )
        except OSError as exc:
            log_handle.close()
            raise EngineUnavailable(
                "ENGINE_START_FAILED", f"引擎进程启动失败（{type(exc).__name__}）"
            ) from None
        finally:
            log_handle.close()
        return EngineHandle(
            port=port, password=password, process=process, work_dir=work_dir, version=version
        )

    def _new_password(self) -> str:
        return security.new_token(24)

    def _check_version(self, exe: Path, env: dict[str, str]) -> str:
        cache_key = f"{exe}|{self._settings.engine_version}"
        cached = self._version_cache.get(cache_key)
        if cached:
            return cached
        try:
            result = subprocess.run(
                [str(exe), "--version"],
                env=env,
                capture_output=True,
                timeout=60,
                creationflags=_creation_flags(),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise EngineUnavailable(
                "ENGINE_VERSION_UNKNOWN", "无法读取引擎版本，已停止启动以免行为不可预期"
            ) from None
        text = (result.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        actual = text[-1].strip() if text else ""
        if result.returncode != 0 or not actual:
            raise EngineUnavailable("ENGINE_VERSION_UNKNOWN", "引擎版本检查失败（退出码非零）")
        expected = self._settings.engine_version
        if self._settings.engine_strict_version and actual != expected:
            raise EngineUnavailable(
                "ENGINE_VERSION_MISMATCH",
                f"引擎版本是 {actual}，本工程只在 {expected} 上验证过；"
                "如确认兼容，可用 HX17_ENGINE_STRICT_VERSION=0 放宽",
            )
        self._version_cache[cache_key] = actual
        return actual

    def _start(self, spec: EngineSpec) -> EngineInstance:
        if self._tokens is not None and not spec.tool_token:
            # 会话级令牌：跨轮复用，引擎进程销毁时一并撤销。
            spec = dataclasses.replace(
                spec,
                tool_token=self._tokens.issue_session(
                    user_id=spec.user_id,
                    session_id=spec.session_id,
                    project_id=spec.project_id,
                    fingerprint=spec.fingerprint,
                ),
            )
        work_dir = self._instance_dir(spec)
        work_dir.mkdir(parents=True, exist_ok=True)
        handle = self._spawner(spec, work_dir)
        instance = EngineInstance(
            spec=spec,
            handle=handle,
            base_url=f"http://127.0.0.1:{handle.port}",
            started_at=time.monotonic(),
            version=handle.version or self._settings.engine_version,
        )
        deadline = time.monotonic() + 90
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if not instance.alive():
                break
            try:
                self._request(instance, "GET", "/global/health", timeout=(1.5, 3.0))
                return instance
            except EngineUnavailable as exc:
                last_error = exc
                time.sleep(0.5)
        self._dispose(instance, remove_dir=False)
        if last_error is not None:
            raise EngineUnavailable(
                "ENGINE_NOT_READY", "引擎进程已启动但健康检查未通过，请查看服务端引擎日志"
            ) from None
        raise EngineUnavailable("ENGINE_EXIT", "引擎进程启动后立即退出，请查看服务端引擎日志")

    def _dispose(self, instance: EngineInstance, *, remove_dir: bool) -> None:
        if self._tokens is not None and instance.spec.tool_token:
            try:
                self._tokens.revoke(instance.spec.tool_token, reason="engine_disposed")
            except TypeError:
                # 注入对象只实现旧签名时退化为普通撤销。
                try:
                    self._tokens.revoke(instance.spec.tool_token)
                except Exception:
                    pass
            except Exception:
                pass
        process = instance.handle.process
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
            except OSError:
                pass
        if remove_dir:
            directory = instance.handle.work_dir or self._instance_dir(instance.spec)
            _safe_rmtree(directory)

    def _healthy(self, instance: EngineInstance) -> bool:
        try:
            self._request(instance, "GET", "/global/health", timeout=(1.0, 2.5))
            return True
        except EngineUnavailable:
            return False

    # ------------------------------------------------------------------ HTTP

    def _request(
        self,
        instance: EngineInstance,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        timeout: tuple[float, float] = QUERY_TIMEOUT,
    ) -> Any:
        url = instance.base_url + path
        try:
            response = requests.request(
                method,
                url,
                json=json_body,
                auth=(self._auth_user, instance.handle.password),
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.Timeout:
            raise EngineUnavailable("ENGINE_TIMEOUT", "引擎响应超时") from None
        except requests.RequestException:
            raise EngineUnavailable("ENGINE_UNREACHABLE", "无法连接引擎进程") from None
        if response.status_code == 204 or not response.content:
            return None
        if response.status_code >= 400:
            raise EngineUnavailable(
                "ENGINE_HTTP_ERROR",
                f"引擎返回 HTTP {response.status_code}",
                status=response.status_code,
            )
        try:
            return response.json()
        except ValueError:
            return None

    # ------------------------------------------------------------------ 会话 API

    def create_session(self, instance: EngineInstance, title: str) -> str:
        data = self._request(
            instance, "POST", "/session", json_body={"title": title[:80]}, timeout=PROMPT_TIMEOUT
        )
        session_id = (data or {}).get("id")
        if not isinstance(session_id, str) or not session_id.startswith("ses"):
            raise EngineUnavailable("ENGINE_BAD_RESPONSE", "引擎创建的会话 ID 不合法")
        return session_id

    def prompt_async(
        self, instance: EngineInstance, session_id: str, *, text: str, system: str, model: str
    ) -> None:
        self._request(
            instance,
            "POST",
            f"/session/{session_id}/prompt_async",
            json_body={
                "parts": [{"type": "text", "text": text}],
                "model": {"providerID": "hx17", "modelID": model},
                "system": system,
            },
            timeout=PROMPT_TIMEOUT,
        )

    def messages(self, instance: EngineInstance, session_id: str, *, limit: int = 60) -> list[dict]:
        data = self._request(
            instance, "GET", f"/session/{session_id}/message?limit={int(limit)}"
        )
        return data if isinstance(data, list) else []

    def status(self, instance: EngineInstance) -> dict:
        data = self._request(instance, "GET", "/session/status")
        return data if isinstance(data, dict) else {}

    def abort(self, instance: EngineInstance, session_id: str) -> bool:
        try:
            result = self._request(instance, "POST", f"/session/{session_id}/abort")
        except EngineUnavailable:
            return False
        return bool(result)

    def session_exists(self, instance: EngineInstance, session_id: str) -> bool:
        """这个 Native 会话**在当前引擎里**是否真的存在。

        只有引擎明确回 404 才返回 False；超时、连接不上、HTTP 5xx 一律抛出，
        由调用方按“不确定”处理。把不确定当 404 去重建会话，会把用户的历史和
        已经完成的工作全部丢掉再重发一遍。
        """
        try:
            data = self._request(instance, "GET", f"/session/{session_id}", timeout=QUERY_TIMEOUT)
        except EngineUnavailable as exc:
            if exc.status == 404:
                return False
            raise
        return isinstance(data, dict) and str(data.get("id") or "") == str(session_id)

    def session_state(self, instance: EngineInstance, session_id: str) -> str:
        entry = self.status(instance).get(session_id)
        if not isinstance(entry, dict):
            # 引擎在会话空闲后不再返回该 key，这是“空闲”而不是“未知”。
            return "idle"
        return str(entry.get("type") or "idle")


__all__ = [
    "EngineHandle",
    "EngineInstance",
    "EngineManager",
    "EngineSpec",
    "EngineUnavailable",
]
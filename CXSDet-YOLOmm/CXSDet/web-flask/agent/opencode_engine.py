"""固定版本 OpenCode HTTP 引擎；每个站点会话独立进程、目录和凭据。

不复用用户在 4096 打开的个人 OpenCode，也不导入其免费模型/全局 Key。
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from agent.errors import AgentError, Code


def local_origin(value):
    parts = urllib.parse.urlsplit(value)
    if (parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}):
        raise AgentError(Code.VALIDATION, "OpenCode 业务桥地址必须是本机 HTTP 根地址")
    return value.rstrip("/")


def binary_path():
    configured = os.environ.get("OPENCODE_BIN")
    value = configured or shutil.which("opencode") or ("E:/OpenCode/bin/opencode.exe" if os.name == "nt" else "")
    if not value or not Path(value).is_file():
        raise AgentError(Code.TOOL_BACKEND_UNAVAILABLE, "找不到 OpenCode；请安装 v1.19.4，并设置 OPENCODE_BIN", http_status=503)
    return str(Path(value).resolve())


class OpenCodeEngine:
    def __init__(self, actor, session, credentials, root, origin):
        self.owner_id = actor.user_id
        self.site_session = session["id"]
        self.project_id = session["project_id"]
        self.credentials = credentials
        self.root = Path(root)
        self.workspace = self.root / "workspace"
        self.token = secrets.token_urlsafe(32)
        self.password = secrets.token_urlsafe(32)
        self.origin = local_origin(origin)
        self.process = None
        self.worker = None
        self.remote_session = None
        self.task_id = None
        self.generation = 0
        self.busy = False
        self.lock = threading.RLock()
        self.seen_parts = set()
        self.native_actions = {}
        self.catalogue_key = ""
        self.fingerprint = hashlib.sha256(json.dumps(credentials.__dict__, sort_keys=True).encode()).hexdigest()
        self.native_enabled = actor.is_admin
        self.port = None

    def redact(self, value):
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if not isinstance(value, str):
            return value
        for secret in (self.credentials.api_key, self.token, self.password):
            if secret:
                value = value.replace(secret, "[redacted]")
        return value

    def configuration(self):
        # 命令拥有操作系统账号权限：仅本机管理员可以逐次确认，普通用户不能启用。
        permissions = {"*": "deny", "haixun_*": "allow", "read": "allow",
            "glob": "allow", "grep": "allow", "list": "allow", "external_directory": "deny",
            "edit": "ask" if self.native_enabled else "deny",
            "bash": "ask" if self.native_enabled else "deny", "todowrite": "allow", "todoread": "allow"}
        return {"$schema": "https://opencode.ai/config.json", "share": "disabled",
            "autoupdate": False, "enabled_providers": ["haixun"],
            "model": "haixun/" + self.credentials.model,
            "small_model": "haixun/" + self.credentials.model,
            "provider": {"haixun": {"npm": "@ai-sdk/openai-compatible", "name": "本人模型连接",
                "options": {"baseURL": self.credentials.api_url.rstrip("/"), "apiKey": self.credentials.api_key},
                "models": {self.credentials.model: {"name": self.credentials.model,
                    "limit": {"context": 64000, "output": 8192}}}}},
            "permission": permissions,
            "agent": {"build": {"steps": 32}},
            "mcp": {"haixun": {"type": "local", "command": [sys.executable,
                str(Path(__file__).with_name("opencode_mcp.py"))],
                "environment": {"HAIXUN_BRIDGE_ORIGIN": self.origin,
                                "HAIXUN_BRIDGE_TOKEN": self.token}, "enabled": True}}}

    def start(self):
        executable = binary_path()
        version = subprocess.run([executable, "--version"], capture_output=True, text=True,
            timeout=12, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if version.returncode or version.stdout.strip() != "1.19.4":
            raise AgentError(Code.TOOL_BACKEND_UNAVAILABLE, "此适配器已验证 OpenCode 1.19.4；请使用对应版本", http_status=503)
        for folder in ("workspace", "config", "data", "cache", "state", "temp"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        # 最小继承环境，避免把站点或其他服务的 Key 自动传给命令工具。
        keep = {"PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE",
                "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "HTTP_PROXY", "HTTPS_PROXY",
                "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
                "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS"}
        env = {key: value for key, value in os.environ.items() if key in keep}
        for variable, folder in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state"), ("TEMP", "temp"), ("TMP", "temp")):
            env[variable] = str(self.root / folder)
        env.update(OPENCODE_CONFIG_CONTENT=json.dumps(self.configuration(), ensure_ascii=False),
            OPENCODE_DISABLE_AUTOUPDATE="1", OPENCODE_DISABLE_MODELS_FETCH="1",
            OPENCODE_DISABLE_DEFAULT_PLUGINS="1", OPENCODE_DISABLE_PROJECT_CONFIG="1",
            OPENCODE_SERVER_PASSWORD=self.password, OPENCODE_SERVER_USERNAME="haixun")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        # 不将子进程 stdout/stderr 落盘，防止 SDK 异常回显凭据。
        self.process = subprocess.Popen([executable, "serve", "--hostname", "127.0.0.1", "--port", str(self.port)],
            cwd=self.workspace, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        for _ in range(100):
            if self.process.poll() is not None:
                raise AgentError(Code.TOOL_BACKEND_UNAVAILABLE, "OpenCode 启动失败，请核对版本和配置", http_status=503)
            try:
                if self.request("GET", "/global/health").get("healthy"):
                    self.remote_session = self.request("POST", "/session", {"title": "海巡智检会话"})["id"]
                    return
            except AgentError:
                time.sleep(.1)
        self.close()
        raise AgentError(Code.TOOL_BACKEND_UNAVAILABLE, "OpenCode 本机服务启动超时", http_status=503)

    def request(self, method, path, payload=None, timeout=10):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        auth = base64.b64encode(("haixun:" + self.password).encode()).decode()
        separator = "&" if "?" in path else "?"
        url = f"http://127.0.0.1:{self.port}" + path + separator + urllib.parse.urlencode({"directory": str(self.workspace)})
        request = urllib.request.Request(url, data=data, method=method,
            headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except (urllib.error.URLError, ValueError, TimeoutError) as exc:
            status = getattr(exc, "code", None)
            raise AgentError(Code.MODEL_CALL_FAILED,
                "OpenCode 本机通信失败" + (f"（HTTP {status}）" if status else "；请检查本机进程"),
                http_status=502) from None

    def abort(self):
        self.generation += 1
        if self.process and self.process.poll() is None and self.remote_session:
            try:
                self.request("POST", f"/session/{self.remote_session}/abort", {})
            except AgentError:
                pass
        self.busy = False

    def close(self):
        self.generation += 1
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=12)
        self.busy = False


class EnginePool:
    # ponytail: 每会话一进程，本机默认最多 4 个；多租户部署需独立 OS 沙箱/资源调度。
    def __init__(self):
        self.engines = {}
        self.tokens = {}
        self.lock = threading.RLock()
        atexit.register(self.close)

    def get(self, actor, session, credentials, root, origin, *, catalogue_key=""):
        # 同一会话首次并发请求只能启动一次；MCP 回调不取得此启动锁。
        with self.lock:
            return self._get_locked(actor, session, credentials, root, origin, catalogue_key=catalogue_key)

    def _get_locked(self, actor, session, credentials, root, origin, *, catalogue_key):
        key = (actor.user_id, session["id"])
        fingerprint = hashlib.sha256(json.dumps(credentials.__dict__, sort_keys=True).encode()).hexdigest()
        with self.lock:
            engine = self.engines.get(key)
            if engine and (engine.fingerprint != fingerprint or engine.catalogue_key != catalogue_key
                           or not engine.process or engine.process.poll() is not None):
                self.tokens.pop(engine.token, None)
                engine.close()
                self.engines.pop(key, None)
                engine = None
            if engine:
                return engine
            if len(self.engines) >= 4:
                idle = next((item for item in self.engines.values() if not item.busy), None)
                if not idle:
                    raise AgentError(Code.TASK_STATE_CONFLICT, "本机 OpenCode 正在处理四个会话，请稍后再试", http_status=409)
                self.tokens.pop(idle.token, None)
                self.engines.pop((idle.owner_id, idle.site_session), None)
                idle.close()
            engine = OpenCodeEngine(actor, session, credentials, root, origin)
            engine.catalogue_key = catalogue_key
            self.engines[key] = engine
            self.tokens[engine.token] = engine
        try:
            engine.start()
        except Exception:
            self.tokens.pop(engine.token, None)
            self.engines.pop(key, None)
            engine.close()
            raise
        return engine

    def find(self, actor, session_id):
        return self.engines.get((actor.user_id, session_id))

    def close(self):
        for engine in list(self.engines.values()):
            engine.close()
        self.engines.clear()
        self.tokens.clear()

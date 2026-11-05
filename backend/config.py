"""海巡智检 v1.7 后端配置。

设计约束（WB17-00 / WB17-01）：

* 数据目录在 **应用工厂创建时** 一次性解析成 :class:`Settings`，之后所有请求只从
  ``app.config["HX17"]`` 读取；请求期间再改环境变量不会切库。
* 代码里不出现固定口令、API Key、默认账号；初始化账号只能由本机 CLI 创建。
* 数据库、会话签名密钥、连接加密密钥都在同一个隔离数据目录内，且已被 .gitignore 覆盖。
* 公开接口（``/api/meta``、``/api/health``）不得输出 data_dir、端口等内部信息。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIRNAME = "var"
DEFAULT_PORT = 5080
DEFAULT_SESSION_HOURS = 12
STAGE = "WB17-04"

# OpenCode 引擎：版本与可执行文件都可用环境变量覆盖，便于换机/换版本；
# 默认值只对应本机已核验的组合（1.19.4 + E:/OpenCode/bin/opencode.exe）。
DEFAULT_ENGINE_EXE = "E:/OpenCode/bin/opencode.exe"
DEFAULT_ENGINE_VERSION = "1.19.4"

# 功能开关：True 表示“本轮真实现并有测试覆盖”，False 表示“界面必须显示未实现”。
FEATURES: dict[str, bool] = {
    "auth": True,
    "users": True,
    "projects": True,
    "sessions": True,
    "message_history": True,
    "directories": True,
    "personal_connection": True,
    "agent_runtime": True,
    "agent_tools": True,
    # 命令执行能力本轮不开放：无法在不引入 OS 沙箱的前提下证明范围。
    "agent_shell": False,
    # WB17-03 接入：资产、数据集、模型、检测记录、AI 核查均为真实实现。
    "assets": True,
    "detection": True,
    "ai_review": True,
    "datasets": True,
    "models": True,
    "business_jobs": True,
    # WB17-04 接入：人工复核版本、反馈池与数据回流（真实文件）。
    "review": True,
    "feedback": True,
    "reflow": True,
    # 训练评估在 WB17-05；未实现就不宣称。
    "training": False,
}


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} 必须是整数") from None
    if not minimum <= value <= maximum:
        raise SystemExit(f"{name} 超出 {minimum}-{maximum}")
    return value


def version() -> str:
    """读取仓库根目录 VERSION；缺失时返回 unknown，不猜版本号。"""
    try:
        return (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _resolve_data_dir(raw: str | os.PathLike[str] | None) -> Path:
    if raw:
        target = Path(str(raw)).expanduser()
    else:
        env = (os.environ.get("HX17_DATA_DIR") or "").strip()
        target = Path(env).expanduser() if env else PROJECT_ROOT / DEFAULT_DATA_DIRNAME
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    return target


@dataclass(frozen=True)
class Settings:
    """不可变的运行配置快照，存进 ``app.config["HX17"]``。"""

    service: str
    version: str
    stage: str
    data_dir: Path
    db_path: Path
    secret_dir: Path
    port: int
    serve_dist: bool
    cookie_secure: bool
    session_ttl_hours: int
    engine_exe: str = DEFAULT_ENGINE_EXE
    engine_python: str = ""
    engine_version: str = DEFAULT_ENGINE_VERSION
    engine_strict_version: bool = True
    engine_idle_seconds: int = 1800
    turn_timeout_seconds: int = 900
    # 只允许测试夹具打开：生产环境不得接受假引擎/假成功。
    allow_fake_engine: bool = False
    features: Mapping[str, bool] = field(default_factory=lambda: dict(FEATURES))

    def public(self) -> dict:
        """可公开的运行元信息：不含任何绝对路径、端口与凭据配置。"""
        return {
            "service": self.service,
            "version": self.version,
            "stage": self.stage,
            "serve_dist": self.serve_dist,
            "features": dict(self.features),
            "engine_version": self.engine_version,
        }


def load_settings(
    *,
    data_dir: str | os.PathLike[str] | None = None,
    port: int | None = None,
    serve_dist: bool | None = None,
    cookie_secure: bool | None = None,
) -> Settings:
    """解析运行配置。只在应用工厂里调用一次。"""
    resolved = _resolve_data_dir(data_dir)
    secret_dir = resolved / "secrets"
    secret_dir.mkdir(parents=True, exist_ok=True)
    if port is None:
        port = _env_int("HX17_PORT", DEFAULT_PORT, minimum=1, maximum=65535)
    if serve_dist is None:
        serve_dist = _env_flag("HX17_SERVE_DIST")
    if cookie_secure is None:
        # 本机 http 联调默认关闭；放到 HTTPS 入口后面时用 HX17_COOKIE_SECURE=1 打开。
        cookie_secure = _env_flag("HX17_COOKIE_SECURE")
    return Settings(
        service="haixun-1.7-backend",
        version=version(),
        stage=STAGE,
        data_dir=resolved,
        db_path=resolved / "haixun.db",
        secret_dir=secret_dir,
        port=port,
        serve_dist=bool(serve_dist),
        cookie_secure=bool(cookie_secure),
        session_ttl_hours=_env_int(
            "HX17_SESSION_HOURS", DEFAULT_SESSION_HOURS, minimum=1, maximum=720
        ),
        engine_exe=(os.environ.get("HX17_ENGINE_EXE") or DEFAULT_ENGINE_EXE).strip(),
        engine_python=(
            os.environ.get("HX17_ENGINE_PYTHON") or sys.executable or ""
        ).strip(),
        engine_version=(
            os.environ.get("HX17_ENGINE_VERSION") or DEFAULT_ENGINE_VERSION
        ).strip(),
        engine_strict_version=_env_flag("HX17_ENGINE_STRICT_VERSION", default=True),
        engine_idle_seconds=_env_int(
            "HX17_ENGINE_IDLE_SECONDS", 1800, minimum=60, maximum=86400
        ),
        turn_timeout_seconds=_env_int(
            "HX17_TURN_TIMEOUT", 900, minimum=30, maximum=7200
        ),
        allow_fake_engine=_env_flag("HX17_ALLOW_FAKE_ENGINE"),
        features=dict(FEATURES),
    )

"""Agent 运行时配置。

全部通过环境变量覆盖，便于本机、测试与他机部署使用同一份代码。
加密主密钥单独落在独立文件里（见 :mod:`agent.crypto`），不与业务数据库同处。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]

# DeepSeek 官方兼容地址，仅作为"预填默认值"，不携带任何 Key。
DEFAULT_DEEPSEEK_API_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AgentSettings:
    """一次调用内使用的配置快照。"""

    secret_path: str
    secret_key_override: str
    max_steps: int
    time_budget_seconds: int
    context_max_chars: int
    recent_message_limit: int
    action_ttl_seconds: int
    readonly_retry_limit: int
    allow_stub_tools: bool
    default_provider_url: str
    default_provider_model: str

    @property
    def max_readonly_retries(self) -> int:
        """只读、不计费查询的自动重试次数上限（方案：最多一次）。"""
        return 1 if self.readonly_retry_limit > 1 else max(0, self.readonly_retry_limit)


def settings() -> AgentSettings:
    """每次按当前环境变量构造配置，测试可直接 monkeypatch。"""
    secret_path = os.environ.get("AGENT_SECRET_PATH") or str(
        _BACKEND_ROOT / "agent_secret.key"
    )
    return AgentSettings(
        secret_path=secret_path,
        secret_key_override=os.environ.get("AGENT_SECRET_KEY", ""),
        max_steps=_env_int("AGENT_MAX_STEPS", 8),
        time_budget_seconds=_env_int("AGENT_TIME_BUDGET_SECONDS", 120),
        context_max_chars=_env_int("AGENT_CONTEXT_MAX_CHARS", 24000),
        recent_message_limit=_env_int("AGENT_RECENT_MESSAGE_LIMIT", 12),
        action_ttl_seconds=_env_int("AGENT_ACTION_TTL_SECONDS", 1800),
        readonly_retry_limit=_env_int("AGENT_READONLY_RETRY_LIMIT", 1),
        allow_stub_tools=_env_flag("AGENT_ALLOW_STUB", True),
        default_provider_url=os.environ.get(
            "AGENT_DEFAULT_PROVIDER_URL", DEFAULT_DEEPSEEK_API_URL
        ),
        default_provider_model=os.environ.get(
            "AGENT_DEFAULT_PROVIDER_MODEL", DEFAULT_DEEPSEEK_MODEL
        ),
    )

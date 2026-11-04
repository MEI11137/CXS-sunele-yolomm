"""个人模型连接：保存、掩码展示、能力检查。

安全与计费约束：

* Key 用 AES-GCM 加密后才入库，接口只回 :func:`mask_secret` 的掩码；
* 能力检查只在用户点击“检查连接”时发生，每次最多 3 次请求（chat / vision / tools），
  单次超时 (连接 6s, 读取 25s)，**不自动重试**，避免隐式扣费；
* 检查结果与“当时的 base_url + model + key 指纹”绑定，任一变化即视为过期；
* 只给服务端用的 :func:`server_credentials` 不暴露任何 HTTP 路由。
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import struct
import time
import zlib
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from backend import crypto, security
from backend.errors import ApiError, not_found

# 预设只填地址，不声明能力：DeepSeek 文本模型不能因为“是 DeepSeek”就当作多模态。
PROVIDERS: list[dict] = [
    {
        "id": "deepseek",
        "label": "DeepSeek（OpenAI 兼容）",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "note": "仅预填接口地址；视觉与工具能力必须用“检查连接”实测，未通过前不要传图。",
    },
    {
        "id": "custom",
        "label": "自定义 OpenAI 兼容接口",
        "base_url": "",
        "models": [],
        "note": "适用于任何实现 /chat/completions 的自建或第三方网关。",
    },
]

PROBE_TIMEOUT = (6.0, 25.0)
MODEL_MAX = 120
BASE_URL_MAX = 300
CONNECTION_NOT_FOUND = "尚未保存个人模型连接"


def _tiny_png_base64() -> str:
    """用标准库拼一张 1x1 白色 PNG，供视觉能力探测使用。"""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    raw = b"\x00\xff\xff\xff"
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode("ascii")


_TINY_PNG_B64 = _tiny_png_base64()


# --------------------------------------------------------------------------- 入参规范化


def normalize_base_url(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        raise ApiError(400, "BAD_BASE_URL", "请填写接口地址，例如 https://api.deepseek.com/v1")
    if len(text) > BASE_URL_MAX:
        raise ApiError(400, "BAD_BASE_URL", "接口地址过长")
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"}:
        raise ApiError(400, "BAD_BASE_URL", "接口地址必须以 http:// 或 https:// 开头")
    if not parts.netloc:
        raise ApiError(400, "BAD_BASE_URL", "接口地址缺少主机名")
    if "@" in parts.netloc:
        raise ApiError(400, "BAD_BASE_URL", "接口地址里不能带用户名或密码")
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        # 允许用户直接粘贴完整端点，统一收敛到 base_url。
        path = path[: -len("/chat/completions")].rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def normalize_model(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        raise ApiError(400, "VALIDATION", "模型 ID 不能为空（填服务商文档里的真实模型名）")
    if len(text) > MODEL_MAX:
        raise ApiError(400, "VALIDATION", "模型 ID 过长")
    if any(char.isspace() for char in text):
        raise ApiError(400, "VALIDATION", "模型 ID 里不能有空格")
    return text


def normalize_provider(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    return text[:40] or "custom"


def _validate_key(raw: Any) -> str:
    text = str(raw or "").strip()
    if len(text) < 8:
        raise ApiError(400, "BAD_API_KEY", "API Key 看起来不完整（至少 8 个字符）")
    if len(text) > 512:
        raise ApiError(400, "BAD_API_KEY", "API Key 过长")
    if any(char in text for char in "\r\n\t "):
        raise ApiError(400, "BAD_API_KEY", "API Key 里不能有空白字符")
    return text


def _aad(user_id: int) -> bytes:
    return f"hx17.connection.user:{int(user_id)}".encode("utf-8")


# --------------------------------------------------------------------------- 探测


@dataclass
class ProbeResponse:
    status: int  # 0 表示网络层失败
    body: dict | None
    text: str
    latency_ms: int


Transport = Callable[[str, dict, dict, tuple[float, float]], ProbeResponse]


def http_transport(
    url: str, headers: dict, payload: dict, timeout: tuple[float, float]
) -> ProbeResponse:
    """真实 HTTP 传输：单次请求、无重试、不跟随跳转。

    上游正文一律不保留（只解析 JSON 结构），避免代理把 Authorization 回显进数据库。
    """
    import requests

    started = time.monotonic()
    try:
        response = requests.post(
            url, headers=headers, json=payload, timeout=timeout, allow_redirects=False
        )
    except requests.RequestException as exc:  # 连接失败/超时/证书错误都归类为 status=0
        return ProbeResponse(0, None, redact(f"{type(exc).__name__}: {exc}"), _elapsed_ms(started))
    try:
        body = response.json()
    except ValueError:
        body = None
    return ProbeResponse(
        response.status_code,
        body if isinstance(body, dict) else None,
        "",
        _elapsed_ms(started),
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _safe_call(
    transport: Transport,
    url: str,
    headers: dict,
    payload: dict,
    timeout: tuple[float, float],
    *,
    secrets: tuple[str, ...] = (),
) -> ProbeResponse:
    try:
        return transport(url, headers, payload, timeout)
    except Exception as exc:  # 注入的传输实现出错也不能把 500 抛给前端
        return ProbeResponse(
            0, None, redact(f"{type(exc).__name__}: {exc}", *secrets), 0
        )


def _first_choice(body: dict | None) -> dict | None:
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0]
    return None


def _message_of(choice: dict | None) -> dict:
    if not choice:
        return {}
    message = choice.get("message")
    return message if isinstance(message, dict) else {}


# 证据里只保留“状态 / 耗时 / 环节 / 固定说明”。
# 上游或代理可能在 error.message、HTML 错误页里回显 Authorization，所以正文一律不落库、
# 不回传；这里按状态码给固定文字，再用 :func:`redact` 兜一层。
_STATUS_NOTES: dict[int, tuple[str, str]] = {
    0: ("network_error", "网络不可达或超时，没有拿到 HTTP 响应"),
    400: ("bad_request", "请求被拒绝：参数或模型名可能不被支持"),
    401: ("unauthorized", "认证被拒绝：服务端不接受该凭据"),
    402: ("payment_required", "账户余额不足或需要付费"),
    403: ("forbidden", "访问被拒绝：凭据没有该接口权限"),
    404: ("not_found", "接口路径或模型不存在"),
    405: ("method_not_allowed", "上游不接受该请求方法"),
    408: ("timeout", "上游处理超时"),
    429: ("rate_limited", "请求频率或配额受限"),
}
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]{4,}")
_KEYLIKE_RE = re.compile(r"\b(?:sk|ak)-[A-Za-z0-9._\-]{4,}", re.IGNORECASE)


def _outcome(status: int) -> tuple[str, str]:
    if status == 200:
        return "ok", "调用成功"
    if status in _STATUS_NOTES:
        return _STATUS_NOTES[status]
    if 500 <= status <= 599:
        return "upstream_error", f"上游服务异常（HTTP {status}）"
    return "unexpected_status", f"上游返回了未预期的状态码（HTTP {status}）"


def redact(text: Any, *secrets: str) -> str:
    """把已知 Key、Bearer 片段和 sk- 形态的串替换掉；再截断长度。"""
    out = "" if text is None else str(text)
    for secret in secrets:
        if secret:
            out = out.replace(str(secret), "***")
    out = _BEARER_RE.sub(r"\1***", out)
    out = _KEYLIKE_RE.sub("***", out)
    return out[:80]


def _describe(response: ProbeResponse, *, step: str, secrets: tuple[str, ...] = ()) -> dict:
    choice = _first_choice(response.body)
    code, note = _outcome(response.status)
    return {
        "step": step,
        "status": response.status,
        "outcome": code,
        "latency_ms": response.latency_ms,
        "finish_reason": redact((choice or {}).get("finish_reason"), *secrets) or None,
        # 固定说明，不是上游原文。
        "note": note,
    }


_STATE_SUMMARIES: dict[str, str] = {
    "rejected": "认证失败：上游以 401/403 明确拒绝了该凭据（未继续探测，避免无意义请求）",
    "payment_required": "无法确认认证：上游返回 402（余额或计费问题），不能据此认为 Key 有效",
    "rate_limited": "无法确认认证：上游返回 429（频率或配额受限），本次不判定 Key 有效",
    "upstream_error": "上游异常（5xx）：认证状态未验证，请稍后重试",
    "network_error": "网络不可达：没有拿到 HTTP 响应，认证与能力均未验证",
    "unverified": "未能确认认证：chat 调用未成功（可能是模型名或参数问题），未继续探测",
}


def _auth_state(response: ProbeResponse) -> str:
    """认证判定：只有正向证据才算通过；否则区分失败 / 未验证 / 上游异常。"""
    status = response.status
    if status == 0:
        return "network_error"
    if status in (401, 403):
        return "rejected"
    if status == 402:
        return "payment_required"
    if status == 429:
        return "rate_limited"
    if 500 <= status <= 599:
        return "upstream_error"
    if status == 200:
        choice = _first_choice(response.body)
        if choice is not None and _message_of(choice):
            return "verified"
    return "unverified"


def probe(
    *,
    base_url: str,
    api_key: str,
    model: str,
    transport: Transport | None = None,
    timeout: tuple[float, float] = PROBE_TIMEOUT,
) -> dict:
    """实测认证 / chat / vision / tools，并留下可复核的证据。"""
    send = transport or http_transport
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    evidence: dict[str, Any] = {}
    capabilities = {"auth": False, "chat": False, "vision": False, "tools": False}

    chat = _safe_call(
        send,
        url,
        headers,
        {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "temperature": 0,
            "stream": False,
        },
        timeout,
        secrets=(api_key,),
    )
    evidence["chat"] = _describe(chat, step="chat", secrets=(api_key,))

    # 认证状态：只有“拿到 200 且返回了 assistant 消息”才算正向证据。
    # 402/429/5xx 只说明上游没正常服务，绝不能当成“Key 已被接受”。
    auth_state = _auth_state(chat)
    capabilities["auth"] = auth_state == "verified"
    if auth_state != "verified":
        code, note = _outcome(chat.status)
        evidence["auth"] = {"status": chat.status, "state": auth_state, "note": note}
        return {
            **capabilities,
            "auth_state": auth_state,
            "evidence": evidence,
            "checked_at": security.now_iso(),
            "summary": _STATE_SUMMARIES.get(auth_state, note),
        }

    capabilities["chat"] = True

    vision = _safe_call(
        send,
        url,
        headers,
        {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "这张图是什么颜色？只回答颜色。"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{_TINY_PNG_B64}"},
                        },
                    ],
                }
            ],
            "max_tokens": 8,
            "temperature": 0,
            "stream": False,
        },
        timeout,
        secrets=(api_key,),
    )
    vision_choice = _first_choice(vision.body)
    vision_text = _message_of(vision_choice).get("content")
    evidence["vision"] = _describe(vision, step="vision", secrets=(api_key,))
    capabilities["vision"] = bool(
        vision.status == 200 and vision_choice is not None and str(vision_text or "").strip()
    )

    tools = _safe_call(
        send,
        url,
        headers,
        {
            "model": model,
            "messages": [{"role": "user", "content": "调用 hx_probe 返回当前状态。"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "hx_probe",
                        "description": "返回探针状态",
                        "parameters": {
                            "type": "object",
                            "properties": {"state": {"type": "string"}},
                            "required": [],
                        },
                    },
                }
            ],
            "tool_choice": "auto",
            "max_tokens": 16,
            "temperature": 0,
            "stream": False,
        },
        timeout,
        secrets=(api_key,),
    )
    tools_choice = _first_choice(tools.body)
    tool_calls = _message_of(tools_choice).get("tool_calls")
    evidence["tools"] = _describe(tools, step="tools", secrets=(api_key,))
    evidence["tools"]["tool_calls"] = bool(tool_calls)
    # 200 且返回结构完整即视为“接受 tools 参数”；是否真的发起调用记录在证据里。
    capabilities["tools"] = bool(tools.status == 200 and tools_choice is not None)

    summary_parts = [
        "认证通过（200 且返回了 chat 回复）",
        "chat 可用" if capabilities["chat"] else "chat 不可用",
        "视觉可用" if capabilities["vision"] else "视觉未通过",
        "工具调用可用" if capabilities["tools"] else "工具调用未通过",
    ]
    return {
        **capabilities,
        "auth_state": "verified",
        "evidence": evidence,
        "checked_at": security.now_iso(),
        "summary": "；".join(summary_parts),
    }


# --------------------------------------------------------------------------- 存取


def _row(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM connections WHERE user_id = ?", (int(user_id),)).fetchone()


def get_row(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    row = _row(conn, user_id)
    if row is None:
        raise ApiError(404, "CONNECTION_NOT_FOUND", CONNECTION_NOT_FOUND)
    return row


def view(conn: sqlite3.Connection, user_id: int) -> dict | None:
    row = _row(conn, user_id)
    if row is None:
        return None
    try:
        capabilities = json.loads(row["capabilities"] or "{}")
    except ValueError:
        capabilities = {}
    if not isinstance(capabilities, dict):
        capabilities = {}
    stale = (
        not capabilities
        or row["checked_fingerprint"] != row["key_fingerprint"]
        or row["checked_base_url"] != row["base_url"]
        or row["checked_model"] != row["model"]
        or row["checked_provider"] != row["provider"]
    )
    return {
        "provider": row["provider"],
        "base_url": row["base_url"],
        "model": row["model"],
        "display_name": row["display_name"],
        # 只给掩码；完整 Key 与密文都不出现在响应里。
        "key_hint": row["key_hint"],
        "key_set": True,
        "capabilities": capabilities,
        "capabilities_stale": bool(stale),
        "checked_at": row["checked_at"],
        "updated_at": row["updated_at"],
    }


def save(
    conn: sqlite3.Connection,
    user_id: int,
    payload: dict,
    *,
    key: bytes,
) -> dict:
    provider = normalize_provider(payload.get("provider"))
    base_url = normalize_base_url(payload.get("base_url"))
    model = normalize_model(payload.get("model"))
    display_name = str(payload.get("display_name") or "").strip()[:60]
    raw_key = payload.get("api_key")
    existing = _row(conn, user_id)
    now = security.now_iso()

    if raw_key is None or str(raw_key).strip() == "":
        if existing is None:
            raise ApiError(400, "BAD_API_KEY", "首次保存必须填写 API Key")
        key_cipher = existing["key_cipher"]
        key_hint = existing["key_hint"]
        key_fingerprint = existing["key_fingerprint"]
    else:
        api_key = _validate_key(raw_key)
        key_cipher = crypto.encrypt_text(api_key, key, aad=_aad(user_id))
        key_hint = security.mask_secret(api_key)
        key_fingerprint = security.fingerprint(api_key)

    changed = (
        existing is None
        or existing["provider"] != provider
        or existing["base_url"] != base_url
        or existing["model"] != model
        or existing["key_fingerprint"] != key_fingerprint
    )

    if existing is None:
        conn.execute(
            """
            INSERT INTO connections (user_id, provider, base_url, model, display_name, key_cipher,
                                     key_hint, key_fingerprint, capabilities, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)
            """,
            (int(user_id), provider, base_url, model, display_name, key_cipher,
             key_hint, key_fingerprint, now, now),
        )
    elif changed:
        # 地址/模型/Key 变了，旧的能力结论立刻作废，避免拿旧结论放行新配置。
        conn.execute(
            """
            UPDATE connections
               SET provider = ?, base_url = ?, model = ?, display_name = ?, key_cipher = ?,
                   key_hint = ?, key_fingerprint = ?, capabilities = '{}', checked_at = NULL,
                   checked_base_url = NULL, checked_model = NULL, checked_provider = NULL,
                   checked_fingerprint = NULL, updated_at = ?
             WHERE user_id = ?
            """,
            (provider, base_url, model, display_name, key_cipher, key_hint,
             key_fingerprint, now, int(user_id)),
        )
    else:
        conn.execute(
            "UPDATE connections SET display_name = ?, updated_at = ? WHERE user_id = ?",
            (display_name, now, int(user_id)),
        )
    conn.commit()
    return view(conn, user_id) or {}


def delete(conn: sqlite3.Connection, user_id: int) -> bool:
    cursor = conn.execute("DELETE FROM connections WHERE user_id = ?", (int(user_id),))
    conn.commit()
    return cursor.rowcount > 0


def check(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    key: bytes,
    transport: Transport | None = None,
) -> dict:
    row = get_row(conn, user_id)
    api_key = crypto.decrypt_text(row["key_cipher"], key, aad=_aad(user_id))
    # 先把“这次检查针对哪套配置”拍下来，探测期间连接若被改动，结果必须丢弃，
    # 否则旧结论会被写成新连接的能力。
    snapshot = (
        row["base_url"],
        row["model"],
        row["provider"],
        row["key_fingerprint"],
    )
    result = probe(
        base_url=row["base_url"], api_key=api_key, model=row["model"], transport=transport
    )
    now = security.now_iso()
    cursor = conn.execute(
        """
        UPDATE connections
           SET capabilities = ?, checked_at = ?, checked_base_url = ?, checked_model = ?,
               checked_provider = ?, checked_fingerprint = ?, updated_at = ?
         WHERE user_id = ? AND base_url = ? AND model = ? AND provider = ? AND key_fingerprint = ?
        """,
        (
            json.dumps(
                {k: v for k, v in result.items() if k != "summary"}, ensure_ascii=False
            ),
            now,
            *snapshot,
            now,
            int(user_id),
            *snapshot,
        ),
    )
    conn.commit()
    data = view(conn, user_id)
    if data is None:
        # 探测期间连接被删除：不重建、不写回，按“结论已丢弃”处理。
        return {
            "applied": False,
            "auth_state": "superseded",
            "capabilities": {},
            "summary": "检查期间连接已被删除，本次结论已丢弃",
        }
    if cursor.rowcount == 0:
        data["applied"] = False
        data["auth_state"] = "superseded"
        data["summary"] = "检查期间连接配置已被修改，本次结论已丢弃，请重新检查"
        return data
    data["applied"] = True
    data["summary"] = result.get("summary", "")
    return data


def server_credentials(conn: sqlite3.Connection, user_id: int, *, key: bytes) -> dict:
    """仅供服务端（后续 Agent 引擎 / AI 核查）取用当前用户凭据。

    不得经任何 HTTP 路由返回；调用方负责不落日志、不回显。
    """
    row = get_row(conn, user_id)
    return {
        "provider": row["provider"],
        "base_url": row["base_url"],
        "model": row["model"],
        "api_key": crypto.decrypt_text(row["key_cipher"], key, aad=_aad(user_id)),
    }


def has_capability(conn: sqlite3.Connection, user_id: int, name: str) -> bool:
    data = view(conn, user_id)
    if not data or data["capabilities_stale"]:
        return False
    return bool(data["capabilities"].get(name))

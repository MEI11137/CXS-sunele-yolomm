"""个人大模型连接的生命周期管理（B-02）。

* 每个用户维护一个当前连接，保存后立即可用于聊天与旧 AI 核查功能；
* Key 加密落库，查询接口只返回 ``key_hint`` 与指纹；
* 严格不做"全局 Key 回退"：用户没配就是 ``AGENT_MODEL_NOT_CONFIGURED``；
* MCP 等工具的凭据走独立的命名空间（:func:`store_credential`），
  与大模型 Key 分开管理，也不向模型暴露通用取密钥工具。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from agent.crypto import SecretBox, fingerprint, mask_secret
from agent.errors import AgentError, Code
from agent.identity import ActorContext
from agent.llm.provider import (
    CAPABILITY_CHAT,
    CAPABILITY_KEYS,
    OpenAIGateway,
    ProviderCredentials,
    STATUS_OK,
    STATUS_UNCHECKED,
    summarize_capabilities,
)
from agent.store import connect, dumps, loads, new_id, now, query_one

ALLOWED_PROVIDERS = {"deepseek", "custom"}
CREDENTIAL_NAMESPACES = {"mcp", "dataset_tool", "document_tool"}
MODEL_NAMESPACE = "model"

# 测试与联调可注入客户端工厂（签名与 openai.OpenAI 一致）
_gateway_factory: Optional[Callable[..., Any]] = None


def configure_gateway_factory(factory: Optional[Callable[..., Any]]) -> None:
    global _gateway_factory
    _gateway_factory = factory


def _normalize_url(api_url: str) -> str:
    url = (api_url or "").strip().rstrip("/")
    if not url:
        raise AgentError(Code.VALIDATION, "接口地址不能为空")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise AgentError(Code.VALIDATION, "接口地址必须以 http:// 或 https:// 开头")
    return url


def save_connection(
    actor: ActorContext,
    *,
    provider: str,
    api_url: str,
    model: str,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """新增或更新本人连接。``api_key`` 为空时保留原 Key（仅限更新场景）。"""
    provider = (provider or "").strip().lower()
    if provider not in ALLOWED_PROVIDERS:
        raise AgentError(Code.VALIDATION, "服务商只能是 deepseek 或 custom")
    url = _normalize_url(api_url)
    model_name = (model or "").strip()
    if not model_name:
        raise AgentError(Code.VALIDATION, "模型名称不能为空")

    conn = connect()
    try:
        existing = query_one(
            conn,
            "SELECT * FROM agent_model_connection WHERE user_id = ?",
            (actor.user_id,),
        )
        key_text = (api_key or "").strip()
        if not key_text:
            if not existing:
                raise AgentError(Code.VALIDATION, "首次配置必须填写 API Key")
            key_text = SecretBox().open(existing["key_cipher"])
        cipher = SecretBox().seal(key_text)
        stamp = now()
        if existing:
            conn.execute(
                "UPDATE agent_model_connection SET provider = ?, api_url = ?, model = ?, "
                "key_cipher = ?, key_hint = ?, key_fingerprint = ?, status = 'unchecked', "
                "capabilities_json = '{}', failure_reason = NULL, updated_at = ? "
                "WHERE user_id = ?",
                (
                    provider,
                    url,
                    model_name,
                    cipher,
                    mask_secret(key_text),
                    fingerprint(key_text),
                    stamp,
                    actor.user_id,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO agent_model_connection "
                "(user_id, provider, api_url, model, key_cipher, key_hint, "
                " key_fingerprint, status, capabilities_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'unchecked', '{}', ?, ?)",
                (
                    actor.user_id,
                    provider,
                    url,
                    model_name,
                    cipher,
                    mask_secret(key_text),
                    fingerprint(key_text),
                    stamp,
                    stamp,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return get_connection_view(actor)


def get_connection_view(actor: ActorContext) -> Optional[Dict[str, Any]]:
    """脱敏视图：任何情况下都不返回完整 Key。"""
    conn = connect()
    try:
        row = query_one(
            conn,
            "SELECT * FROM agent_model_connection WHERE user_id = ?",
            (actor.user_id,),
        )
    finally:
        conn.close()
    if not row:
        return None
    capabilities = loads(row.get("capabilities_json"))
    summary = summarize_capabilities(capabilities)
    return {
        "configured": True,
        "provider": row["provider"],
        "api_url": row["api_url"],
        "model": row["model"],
        "key_hint": row["key_hint"],
        "key_fingerprint": row["key_fingerprint"],
        "status": row["status"],
        "capabilities": capabilities,
        "missing_capabilities": summary["missing_capabilities"],
        "failure_reason": row.get("failure_reason"),
        "last_checked_at": row.get("last_checked_at"),
        "updated_at": row["updated_at"],
    }


def delete_connection(actor: ActorContext) -> bool:
    conn = connect()
    try:
        cursor = conn.execute(
            "DELETE FROM agent_model_connection WHERE user_id = ?", (actor.user_id,)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def _credentials(actor: ActorContext) -> ProviderCredentials:
    """仅供服务端内部使用；不得经任何工具或接口返回到前端/模型。"""
    conn = connect()
    try:
        row = query_one(
            conn,
            "SELECT * FROM agent_model_connection WHERE user_id = ?",
            (actor.user_id,),
        )
    finally:
        conn.close()
    if not row:
        raise AgentError(
            Code.MODEL_NOT_CONFIGURED,
            "当前账号尚未配置可用的大模型连接，请先在模型设置中填写并检查",
            http_status=409,
        )
    try:
        api_key = SecretBox().open(row["key_cipher"])
    except AgentError as exc:
        raise AgentError(
            Code.MODEL_NOT_CONFIGURED,
            "已保存的凭据无法解密，请重新填写并保存",
            http_status=409,
            details={"reason": str(exc)},
        ) from exc
    return ProviderCredentials(
        provider=row["provider"],
        api_url=row["api_url"],
        model=row["model"],
        api_key=api_key,
    )


def model_client(actor: ActorContext) -> OpenAIGateway:
    """按**当前用户**创建客户端；没有个人连接时直接失败，不回退到任何全局 Key。"""
    return OpenAIGateway(_credentials(actor), client_factory=_gateway_factory)


def capabilities(actor: ActorContext) -> Dict[str, Any]:
    view = get_connection_view(actor)
    return (view or {}).get("capabilities", {})


def require_capability(actor: ActorContext, capability: str) -> None:
    entry = capabilities(actor).get(capability) or {}
    if entry.get("status") == STATUS_OK:
        return
    if entry.get("status") == STATUS_UNCHECKED or not entry:
        raise AgentError(
            Code.MODEL_CAPABILITY_MISSING,
            f"当前连接尚未验证「{capability}」能力，请先在模型设置中执行能力检查",
            http_status=409,
            details={"capability": capability, "status": entry.get("status", STATUS_UNCHECKED)},
        )
    raise AgentError(
        Code.MODEL_CAPABILITY_MISSING,
        entry.get("message") or f"当前模型不支持「{capability}」",
        http_status=409,
        details={"capability": capability, "reason": entry.get("reason", "")},
    )


def check_connection(actor: ActorContext) -> Dict[str, Any]:
    """真实执行能力检测并持久化结果。"""
    gateway = model_client(actor)
    caps = gateway.probe_all()
    summary = summarize_capabilities(caps)
    first_failure = ""
    for key in (CAPABILITY_CHAT,) + CAPABILITY_KEYS:
        entry = caps.get(key) or {}
        if entry.get("status") != STATUS_OK and entry.get("message"):
            first_failure = entry["message"]
            break
    conn = connect()
    try:
        conn.execute(
            "UPDATE agent_model_connection SET status = ?, capabilities_json = ?, "
            "failure_reason = ?, last_checked_at = ?, updated_at = ? WHERE user_id = ?",
            (
                summary["status"],
                dumps(caps),
                first_failure or None,
                now(),
                now(),
                actor.user_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    view = get_connection_view(actor) or {}
    view["checked_now"] = True
    return view


# --------------------------------------------------------------------------- #
# 独立凭据（MCP / 数据集工具等），与大模型 Key 分开
# --------------------------------------------------------------------------- #
def _check_namespace(namespace: str) -> str:
    clean = (namespace or "").strip().lower()
    if clean == MODEL_NAMESPACE:
        raise AgentError(
            Code.VALIDATION, "大模型 Key 只能通过模型设置维护，不能写入工具凭据"
        )
    if clean.split(":")[0] not in CREDENTIAL_NAMESPACES:
        raise AgentError(Code.VALIDATION, "不支持的凭据命名空间")
    return clean


def store_credential(
    actor: ActorContext, namespace: str, name: str, value: str
) -> Dict[str, Any]:
    ns = _check_namespace(namespace)
    clean_name = (name or "").strip()
    if not clean_name or not (value or "").strip():
        raise AgentError(Code.VALIDATION, "凭据名称和内容不能为空")
    cipher = SecretBox().seal(value.strip())
    hint = mask_secret(value.strip())
    stamp = now()
    conn = connect()
    try:
        existing = query_one(
            conn,
            "SELECT id FROM agent_credential WHERE owner_id = ? AND namespace = ? AND name = ?",
            (actor.user_id, ns, clean_name),
        )
        if existing:
            conn.execute(
                "UPDATE agent_credential SET value_cipher = ?, hint = ?, updated_at = ? "
                "WHERE id = ?",
                (cipher, hint, stamp, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO agent_credential "
                "(id, owner_id, namespace, name, value_cipher, hint, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id("cred"), actor.user_id, ns, clean_name, cipher, hint, stamp, stamp),
            )
        conn.commit()
    finally:
        conn.close()
    return {"namespace": ns, "name": clean_name, "hint": hint}


def read_credential(actor: ActorContext, namespace: str, name: str) -> Optional[str]:
    """服务端内部读取；只允许在工具适配层内调用，不对外提供接口。"""
    ns = _check_namespace(namespace)
    conn = connect()
    try:
        row = query_one(
            conn,
            "SELECT value_cipher FROM agent_credential "
            "WHERE owner_id = ? AND namespace = ? AND name = ?",
            (actor.user_id, ns, name),
        )
    finally:
        conn.close()
    if not row:
        return None
    return SecretBox().open(row["value_cipher"])


def delete_credential(actor: ActorContext, namespace: str, name: str) -> bool:
    ns = _check_namespace(namespace)
    conn = connect()
    try:
        cursor = conn.execute(
            "DELETE FROM agent_credential WHERE owner_id = ? AND namespace = ? AND name = ?",
            (actor.user_id, ns, name),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def import_legacy_local_connection(actor: ActorContext) -> Dict[str, Any]:
    """把本机 ``algo/llm/config.local.json`` 导入**当前管理员自己**的账号。

    只允许管理员显式触发；导入后不会成为其他用户的默认连接，也不会写日志明文。
    """
    if not actor.is_admin:
        raise AgentError(Code.FORBIDDEN, "只有管理员可以导入本机历史连接", http_status=403)
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "algo" / "llm" / "config.local.json"
    if not path.is_file():
        raise AgentError(Code.NOT_FOUND, "本机没有可导入的历史连接配置", http_status=404)
    import json

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise AgentError(Code.VALIDATION, "历史连接配置无法解析") from exc
    api_key = (raw.get("api_key") or "").strip()
    if not api_key:
        raise AgentError(Code.VALIDATION, "历史配置里没有可导入的 Key")
    return save_connection(
        actor,
        provider="custom",
        api_url=raw.get("api_url") or "https://api.deepseek.com",
        model=raw.get("model") or "deepseek-chat",
        api_key=api_key,
    )

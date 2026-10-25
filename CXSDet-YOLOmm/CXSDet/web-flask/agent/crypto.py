"""个人凭据的最小加密存储。

设计约束（对应任务书 B-02）：

* 只用标准库实现，避免给队友增加安装依赖；
* 主密钥存放在**独立文件**（``agent_secret.key``）或 ``AGENT_SECRET_KEY``
  环境变量里，与业务数据库分开保管，数据库被单独拷走时无法解出 Key；
* 构造为 HMAC-SHA256 计数器流加密 + Encrypt-then-MAC，并带版本前缀，
  后续替换为 AES-GCM 时可按前缀兼容旧数据；
* 对外只返回脱敏提示（``key_hint``）与指纹，不回传完整 Key。

该实现仅用于"本地单机 + 个人 Key"的低风险场景，正式产品应换成
KMS/DPAPI 一类的平台密钥托管。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from typing import Optional

from agent.config import settings
from agent.errors import AgentError, Code

_VERSION_TAG = b"AG1"
_NONCE_BYTES = 16
_TAG_BYTES = 32
_KEY_BYTES = 32


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(
            hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        )
        counter += 1
    return bytes(out[:length])


def _load_or_create_master_key(path: str, override: str) -> bytes:
    if override:
        try:
            raw = bytes.fromhex(override.strip())
        except ValueError:
            raw = base64.urlsafe_b64decode(override.strip().encode("ascii"))
        if len(raw) < 16:
            raise AgentError(Code.SECRET_STORE_FAILED, "AGENT_SECRET_KEY 长度不足")
        return raw

    target = os.path.abspath(path)
    if os.path.isfile(target):
        with open(target, "rb") as handle:
            raw = bytes.fromhex(handle.read().decode("ascii").strip())
        if len(raw) < 16:
            raise AgentError(Code.SECRET_STORE_FAILED, "主密钥文件内容损坏")
        return raw

    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    raw = secrets.token_bytes(_KEY_BYTES)
    with open(target, "w", encoding="ascii") as handle:
        handle.write(raw.hex())
    try:  # 尽力收紧权限；Windows 上 chmod 语义有限，失败不阻断
        os.chmod(target, 0o600)
    except OSError:
        pass
    return raw


def _master_key() -> bytes:
    cfg = settings()
    return _load_or_create_master_key(cfg.secret_path, cfg.secret_key_override)


class SecretBox:
    """加密保存 / 解密读取字符串。"""

    def __init__(self, master_key: Optional[bytes] = None) -> None:
        self._key = master_key or _master_key()

    def _keys(self, salt: bytes) -> tuple[bytes, bytes]:
        enc = _hkdf(self._key, salt, b"agent-secret-enc")
        mac = _hkdf(self._key, salt, b"agent-secret-mac")
        return enc, mac

    def seal(self, plaintext: str) -> str:
        if plaintext is None:
            raise AgentError(Code.SECRET_STORE_FAILED, "没有可保存的凭据内容")
        nonce = secrets.token_bytes(_NONCE_BYTES)
        enc_key, mac_key = self._keys(nonce)
        payload = plaintext.encode("utf-8")
        cipher = bytes(
            a ^ b for a, b in zip(payload, _keystream(enc_key, nonce, len(payload)))
        )
        tag = hmac.new(mac_key, _VERSION_TAG + nonce + cipher, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(
            _VERSION_TAG + nonce + cipher + tag
        ).decode("ascii")

    def open(self, token: str) -> str:
        try:
            raw = base64.urlsafe_b64decode(token.encode("ascii"))
        except Exception as exc:  # noqa: BLE001 - 统一转成稳定错误码
            raise AgentError(Code.SECRET_STORE_FAILED, "凭据格式无法识别") from exc
        if not raw.startswith(_VERSION_TAG) or len(raw) < 3 + _NONCE_BYTES + _TAG_BYTES:
            raise AgentError(Code.SECRET_STORE_FAILED, "凭据版本不受支持")
        body = raw[3:]
        nonce = body[:_NONCE_BYTES]
        cipher = body[_NONCE_BYTES:-_TAG_BYTES]
        tag = body[-_TAG_BYTES:]
        enc_key, mac_key = self._keys(nonce)
        expected = hmac.new(
            mac_key, _VERSION_TAG + nonce + cipher, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(tag, expected):
            raise AgentError(Code.SECRET_STORE_FAILED, "凭据校验失败，可能被篡改")
        payload = bytes(
            a ^ b for a, b in zip(cipher, _keystream(enc_key, nonce, len(cipher)))
        )
        return payload.decode("utf-8")


def fingerprint(secret: str) -> str:
    """不可逆指纹，用于判断"是否换了 Key"而不保存原文。"""
    return hashlib.sha256(("agent:" + (secret or "")).encode("utf-8")).hexdigest()[:32]


def mask_secret(secret: str) -> str:
    """脱敏展示，例如 ``sk-a****9f2c``。"""
    text = (secret or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return text[0] + "*" * (len(text) - 1)
    return f"{text[:4]}****{text[-4:]}"

"""个人模型连接凭据的加密存储。

* 用 AES-256-GCM（``cryptography``）加密 API Key，nonce 随机，密文带版本前缀；
* 主密钥单独存放在 ``<data_dir>/secrets/connection.key``，与 SQLite 分文件保管；
* 数据库被单独拷走时无法解出 Key；接口侧只返回掩码。
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.errors import ApiError

_PREFIX = "gcm1"
_NONCE_BYTES = 12


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def encrypt_text(plaintext: str, key: bytes, *, aad: bytes | None = None) -> str:
    if not plaintext:
        raise ValueError("不允许加密空文本")
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), aad)
    return f"{_PREFIX}.{_b64e(nonce)}.{_b64e(sealed)}"


def decrypt_text(token: str, key: bytes, *, aad: bytes | None = None) -> str:
    if not token or not token.startswith(_PREFIX + "."):
        raise ApiError(500, "CREDENTIAL_UNREADABLE", "凭据格式无法识别，请重新保存连接")
    try:
        _, nonce_b64, sealed_b64 = token.split(".", 2)
        plaintext = AESGCM(key).decrypt(_b64d(nonce_b64), _b64d(sealed_b64), aad)
    except (InvalidTag, ValueError):
        # 主密钥被替换或密文被篡改：不返回半截内容，直接要求重填。
        raise ApiError(500, "CREDENTIAL_UNREADABLE", "凭据无法解密（主密钥可能已更换），请重新填写") from None
    return plaintext.decode("utf-8")


def load_key(path: Path) -> bytes:
    """读取连接加密主密钥（由 security.load_or_create_key 生成）。"""
    from backend.security import load_or_create_key

    return load_or_create_key(path)

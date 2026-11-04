"""口令哈希、签名令牌与密钥文件。

只依赖标准库：

* 口令用 ``hashlib.scrypt``（不可用时回退 PBKDF2-HMAC-SHA256），存成带算法前缀的字符串，
  便于以后换参数而不用清库；
* 令牌用 Flask 已依赖的 itsdangerous 签名（不自己维护协议），服务端只验签，
  真正的吊销/过期在 ``auth_sessions`` 表里判定；
* 主密钥从独立文件加载，缺失时用「完整内容 + 原子硬链接」创建；已有文件损坏或
  读不到时**明确失败**，绝不覆盖，避免毁掉旧密文的解密关系。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from itsdangerous import BadSignature, URLSafeSerializer

# scrypt 参数：n=2^14, r=8, p=1 约 16MB 内存，单机登录足够。
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_PBKDF2_ITERATIONS = 240_000
_KEY_BYTES = 32
# Windows 上 os.open 默认是文本模式，会把密钥里的 0x0A 写成 0x0D 0x0A（32 字节变 33 字节），
# 必须显式要求二进制；POSIX 没有这个标志。
_O_BINARY = getattr(os, "O_BINARY", 0)


# --------------------------------------------------------------------------- 时间


def now_iso() -> str:
    """UTC ISO 时间，固定格式便于按字符串排序。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_epoch() -> int:
    return int(time.time())


# --------------------------------------------------------------------------- 口令


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    try:
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=_KEY_BYTES,
            maxmem=64 * 1024 * 1024,
        )
        return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(derived)}"
    except (ValueError, OSError):
        derived = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS, dklen=_KEY_BYTES
        )
        return f"pbkdf2${_PBKDF2_ITERATIONS}${_b64e(salt)}${_b64e(derived)}"


def verify_password(password: str, stored: str) -> bool:
    if not stored or not password:
        return False
    parts = stored.split("$")
    try:
        if parts[0] == "scrypt" and len(parts) == 6:
            _, n, r, p, salt_b64, hash_b64 = parts
            derived = hashlib.scrypt(
                password.encode("utf-8"),
                salt=_b64d(salt_b64),
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=len(_b64d(hash_b64)),
                maxmem=128 * 1024 * 1024,
            )
            return hmac.compare_digest(derived, _b64d(hash_b64))
        if parts[0] == "pbkdf2" and len(parts) == 4:
            _, iterations, salt_b64, hash_b64 = parts
            derived = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                _b64d(salt_b64),
                int(iterations),
                dklen=len(_b64d(hash_b64)),
            )
            return hmac.compare_digest(derived, _b64d(hash_b64))
    except (ValueError, KeyError):
        return False
    return False


_DUMMY_HASH = hash_password("dummy-password-for-timing")


def verify_password_or_dummy(password: str, stored: str | None) -> bool:
    """账号不存在时也跑一次哈希，避免通过响应时间枚举用户名。"""
    if not stored:
        verify_password(password, _DUMMY_HASH)
        return False
    return verify_password(password, stored)


# --------------------------------------------------------------------------- 令牌

# itsdangerous 负责编码/签名/篡改检测；我们只额外约束“载荷必须是 dict 且未过期”。
_TOKEN_SALT = "hx17.session"
_TOKEN_MAX_LENGTH = 4096


def _token_serializer(key: bytes) -> URLSafeSerializer:
    return URLSafeSerializer(key, salt=_TOKEN_SALT, signer_kwargs={"digest_method": hashlib.sha256})


def sign_token(payload: dict[str, Any], key: bytes) -> str:
    return _token_serializer(key).dumps(payload)


def verify_token(token: str, key: bytes) -> dict[str, Any] | None:
    """验签并检查过期；任何非法输入都返回 None（调用方按未登录处理）。

    以前在 try 之外做 ``body.encode("ascii")``，畸形 Unicode 令牌会冒出
    ``UnicodeEncodeError`` 变成 500；改用 itsdangerous 后编码与验签都在其内部完成，
    这里再兜住长度、类型与载荷结构。
    """
    if not isinstance(token, str):
        return None
    if not token or len(token) > _TOKEN_MAX_LENGTH:
        return None
    try:
        payload = _token_serializer(key).loads(token)
    except (BadSignature, ValueError, TypeError, UnicodeError):
        # BadPayload / BadData / 无效 base64 / 非 UTF-8 都是 BadSignature 子类。
        return None
    except Exception:  # 防御：任何解析层的意外异常都不能变成 500
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int) or exp <= now_epoch():
        return None
    return payload


# --------------------------------------------------------------------------- 密钥文件


class KeyFileError(RuntimeError):
    """主密钥文件损坏、不可读或无法创建。

    调用方必须让启动失败：静默换新密钥会让旧库里的密文永远解不开。
    异常文本只带路径与原因，**绝不包含密钥内容**。
    """


def _read_key_file(path: Path, length: int) -> bytes | None:
    """读取并校验主密钥；文件不存在返回 None，存在但异常则抛错（绝不覆盖）。"""
    try:
        if not path.exists():
            return None
        data = path.read_bytes()
    except OSError as exc:
        raise KeyFileError(
            f"主密钥文件无法读取（{type(exc).__name__}）：{path}。"
            "为避免破坏已有数据的解密关系，已停止启动；请检查文件权限或从备份恢复。"
        ) from None
    if len(data) != length:
        raise KeyFileError(
            f"主密钥文件长度异常（{len(data)} 字节，期望 {length}）：{path}。"
            "该文件可能已损坏，不会被自动覆盖；请从备份恢复，或确认后手动删除并重建数据目录。"
        )
    return data


def _harden(path: Path) -> None:
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


def _remove_quietly(path: Path) -> None:
    """删除临时文件；Windows 上杀毒/索引可能短暂占用，做有界重试后放弃。"""
    for _ in range(3):
        try:
            os.unlink(str(path))
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.01)


def _create_key_file(path: Path, key: bytes) -> bool:
    """原子地创建密钥文件。

    先把完整内容写进同目录临时文件并 fsync，再用 ``os.link`` 抢占最终文件名：
    链接成功即说明文件内容已经在位，别的实例永远不会读到“半个密钥”。
    返回 True 表示本进程创建成功，False 表示别的实例已经建好。
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
    try:
        os.write(fd, key)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        try:
            os.link(str(tmp), str(path))
            return True
        except FileExistsError:
            return False
        except PermissionError:
            # Windows 上目标已存在时偶尔报“拒绝访问”而不是 FileExistsError。
            if path.exists():
                return False
            return _create_key_file_exclusive(path, key)
        except OSError:
            # 文件系统不支持硬链接（如部分网络盘）时的退化路径：排他创建。
            return _create_key_file_exclusive(path, key)
    finally:
        _remove_quietly(tmp)


def _create_key_file_exclusive(path: Path, key: bytes) -> bool:
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(fd, key)
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def load_or_create_key(path: Path, *, length: int = _KEY_BYTES) -> bytes:
    """加载主密钥；仅在文件确实不存在时创建。

    * 已有文件长度正确 → 原样返回，不重写、不改动；
    * 已有文件损坏/不可读 → 抛 :class:`KeyFileError`，绝不覆盖；
    * 首次创建用原子链接；并发时只有一方创建成功，其余读取同一份结果；
    * 不循环重试：创建中若读到不完整内容，按失败处理。
    """
    if length <= 0:
        raise ValueError("length 必须为正数")
    path = Path(path)
    existing = _read_key_file(path, length)
    if existing is not None:
        return existing

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise KeyFileError(
            f"无法创建密钥目录（{type(exc).__name__}）：{path.parent}"
        ) from None

    key = secrets.token_bytes(length)
    try:
        created = _create_key_file(path, key)
    except OSError as exc:
        raise KeyFileError(
            f"无法创建主密钥文件（{type(exc).__name__}）：{path}"
        ) from None
    if created:
        _harden(path)
        return key

    # 另一个实例先创建：只读一次，读到损坏就失败，不做无限重试。
    data = _read_key_file(path, length)
    if data is None:
        raise KeyFileError(
            f"主密钥文件在创建过程中消失：{path}。请检查是否有其他进程在清理数据目录。"
        )
    return data


# --------------------------------------------------------------------------- 杂项


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def new_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def mask_secret(secret: str) -> str:
    """只保留极少量字符用于辨认，绝不回传完整 Key。"""
    text = secret.strip()
    if len(text) <= 8:
        return "****"
    return f"{text[:3]}****{text[-4:]}"


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:32]

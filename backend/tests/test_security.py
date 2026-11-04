"""密钥文件与会话令牌的边界用例。

对应独立审查复现的两个反例：

* 已有密钥过短/不可读时被静默覆盖（会毁掉旧密文的解密关系）；
* 畸形 Unicode 令牌在 try 之外触发 ``UnicodeEncodeError`` 变成 500。
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from backend import security
from backend.tests.support import Harness, login


class KeyFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hx17-key-")
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_valid_key_is_reused_not_rewritten(self):
        path = self.base / "secrets" / "connection.key"
        first = security.load_or_create_key(path)
        self.assertEqual(len(first), 32)
        self.assertEqual(path.read_bytes(), first)
        # 再次加载拿到同一份，且文件内容不变。
        self.assertEqual(security.load_or_create_key(path), first)
        self.assertEqual(path.read_bytes(), first)

    def test_short_key_is_not_overwritten(self):
        path = self.base / "short.key"
        path.write_bytes(b"SHORT")
        with self.assertRaises(security.KeyFileError):
            security.load_or_create_key(path)
        # 关键：文件必须保持原样，不能被换成新密钥。
        self.assertEqual(path.read_bytes(), b"SHORT")

    def test_unreadable_key_is_not_recreated(self):
        path = self.base / "not-a-file.key"
        path.mkdir()
        with self.assertRaises(security.KeyFileError):
            security.load_or_create_key(path)
        self.assertTrue(path.is_dir())

    def test_error_message_never_contains_key_material(self):
        path = self.base / "short.key"
        payload = b"0123456789abcdef"
        path.write_bytes(payload)
        with self.assertRaises(security.KeyFileError) as caught:
            security.load_or_create_key(path)
        self.assertNotIn(payload.decode(), str(caught.exception))

    def test_key_file_is_written_binary_safe(self):
        # Windows 的 os.open 默认文本模式会把 0x0A 写成 0x0D 0x0A，密钥会多出 1 字节；
        # 这里用含 \n、\r 的固定字节验证写入后完全一致。
        key = bytes(range(32))
        self.assertIn(10, key)
        path = self.base / "binary.key"
        self.assertTrue(security._create_key_file(path, key))
        self.assertEqual(path.read_bytes(), key)
        self.assertEqual(len(path.read_bytes()), 32)
        self.assertEqual(security.load_or_create_key(path), key)

    def test_concurrent_creation_converges_on_one_key(self):
        path = self.base / "race" / "session.key"
        results: list[bytes] = []
        errors: list[str] = []
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            try:
                results.append(security.load_or_create_key(path))
            except Exception as exc:  # pragma: no cover - 失败即测试失败
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(path.read_bytes(), results[0])
        # 创建用的临时文件必须清理干净。
        self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_incomplete_file_is_not_repaired(self):
        # 模拟“另一个进程刚创建还没写完”：读到不完整内容必须失败，而不是覆盖或等待。
        path = self.base / "partial.key"
        path.write_bytes(b"\x00" * 8)
        with self.assertRaises(security.KeyFileError):
            security.load_or_create_key(path)
        self.assertEqual(path.stat().st_size, 8)


class TokenBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.key = b"k" * 32
        self.valid = security.sign_token(
            {"sid": "s1", "sub": 1, "tv": 1, "exp": security.now_epoch() + 600}, self.key
        )

    def test_roundtrip(self):
        payload = security.verify_token(self.valid, self.key)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["sid"], "s1")

    def test_wrong_key_fails(self):
        self.assertIsNone(security.verify_token(self.valid, b"z" * 32))

    def test_tampered_token_fails(self):
        self.assertIsNone(security.verify_token(self.valid[:-2] + "aa", self.key))

    def test_malformed_values_return_none_instead_of_raising(self):
        cases = {
            "unicode_body": "v1.测试.abc",
            "unicode_mac": "v1.eyJhIjoxfQ.测试",
            "invalid_base64": "!!!.!!!.!!!",
            "empty": "",
            "dots_only": "...",
            "too_long": "a" * 5000,
            "whitespace": "   ",
            "non_str": 12345,
            "none": None,
        }
        for name, token in cases.items():
            with self.subTest(name=name):
                self.assertIsNone(security.verify_token(token, self.key))

    def test_signed_payload_must_be_dict(self):
        token = security._token_serializer(self.key).dumps([1, 2, 3])
        self.assertIsNone(security.verify_token(token, self.key))
        token = security._token_serializer(self.key).dumps("just-a-string")
        self.assertIsNone(security.verify_token(token, self.key))

    def test_expiry_and_exp_type_are_enforced(self):
        expired = security.sign_token({"exp": security.now_epoch() - 1}, self.key)
        self.assertIsNone(security.verify_token(expired, self.key))
        for bad in (True, "9999999999", None, 1.5):
            token = security.sign_token({"exp": bad}, self.key)
            self.assertIsNone(security.verify_token(token, self.key), repr(bad))
        missing = security.sign_token({"sid": "s1"}, self.key)
        self.assertIsNone(security.verify_token(missing, self.key))


class MalformedCookieHttpTest(unittest.TestCase):
    """畸形 Cookie 必须走 401，接口不能 500。"""

    def setUp(self):
        self.harness = Harness()
        self.harness.create_user("alice")
        self.client = self.harness.client()

    def tearDown(self):
        self.harness.close()

    def test_malformed_cookie_is_unauthenticated_not_500(self):
        for token in ("v1.测试.abc", "v1.eyJhIjoxfQ.测试", "!!!", "", "a" * 5000):
            with self.subTest(token=token[:12]):
                client = self.harness.client()
                client.set_cookie("hx17_session", token)
                response = client.get("/api/auth/me")
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.get_json()["error"], "AUTH_REQUIRED")

    def test_revoked_and_disabled_sessions_are_rejected(self):
        client = self.harness.client()
        login(client, "alice")
        self.assertEqual(client.get("/api/auth/me").status_code, 200)

        connection = self.harness.conn()
        try:
            connection.execute("UPDATE auth_sessions SET revoked_at = 'x'")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(client.get("/api/auth/me").status_code, 401)

        fresh = self.harness.client()
        login(fresh, "alice")
        connection = self.harness.conn()
        try:
            connection.execute("UPDATE users SET status = 'disabled'")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(fresh.get("/api/auth/me").status_code, 401)

    def test_expired_session_row_is_rejected(self):
        client = self.harness.client()
        login(client, "alice")
        connection = self.harness.conn()
        try:
            connection.execute("UPDATE auth_sessions SET expires_at = 1")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(client.get("/api/auth/me").status_code, 401)


if __name__ == "__main__":
    unittest.main()

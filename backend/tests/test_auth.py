"""登录态、停用、过期、CSRF 与改密。"""

from __future__ import annotations

import unittest

from backend import security
from backend.services import users
from backend.tests.support import Harness, api, body, csrf_headers, login


class LoginTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.harness.create_user("alice", role="admin")
        self.harness.create_user("bob")
        self.client = self.harness.client()

    def tearDown(self):
        self.harness.close()

    def test_login_sets_httponly_cookie_and_no_token_in_body(self):
        self.client.get("/api/meta")
        response = self.client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct-horse-9"},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(response.status_code, 200)
        payload = body(response)
        self.assertEqual(payload["data"]["user"]["username"], "alice")
        self.assertEqual(payload["data"]["user"]["role"], "admin")
        self.assertNotIn("token", str(payload).lower())
        self.assertNotIn("password", str(payload).lower())

        cookies = " ".join(response.headers.getlist("Set-Cookie"))
        self.assertIn("hx17_session=", cookies)
        self.assertIn("HttpOnly", cookies)
        self.assertIn("SameSite=Lax", cookies)

    def test_wrong_password_and_unknown_user_share_error(self):
        self.client.get("/api/meta")
        wrong = self.client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "not-the-password"},
            headers=csrf_headers(self.client),
        )
        unknown = self.client.post(
            "/api/auth/login",
            json={"username": "nobody", "password": "not-the-password"},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(wrong.get_json()["error"], "AUTH_INVALID")
        self.assertEqual(unknown.get_json()["error"], "AUTH_INVALID")

    def test_disabled_account_cannot_login(self):
        connection = self.harness.conn()
        try:
            user = users.find_by_username(connection, "bob")
            users.set_status(connection, int(user["id"]), "disabled")
        finally:
            connection.close()
        self.client.get("/api/meta")
        response = self.client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "correct-horse-9"},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "ACCOUNT_DISABLED")

    def test_logout_revokes_session_server_side(self):
        login(self.client, "alice")
        sid_cookie = self.client.get_cookie("hx17_session").value
        self.assertEqual(api(self.client, "POST", "/api/auth/logout", {}).status_code, 200)
        # 把旧令牌塞回去也必须失败：吊销发生在服务端，不依赖客户端删 Cookie。
        self.client.set_cookie("hx17_session", sid_cookie)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_tampered_token_is_rejected(self):
        login(self.client, "alice")
        token = self.client.get_cookie("hx17_session").value
        self.client.set_cookie("hx17_session", token[:-3] + "aaa")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_garbage_token_is_rejected(self):
        self.client.set_cookie("hx17_session", "v1.not-a-token")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_server_side_expiry_beats_token_expiry(self):
        login(self.client, "alice")
        connection = self.harness.conn()
        try:
            connection.execute("UPDATE auth_sessions SET expires_at = 1000")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_disabling_account_kills_existing_session(self):
        login(self.client, "bob")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        connection = self.harness.conn()
        try:
            user = users.find_by_username(connection, "bob")
            users.set_status(connection, int(user["id"]), "disabled")
        finally:
            connection.close()
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_password_reset_invalidates_tokens(self):
        login(self.client, "alice")
        connection = self.harness.conn()
        try:
            user = users.find_by_username(connection, "alice")
            users.reset_password(connection, int(user["id"]), "another-strong-1")
        finally:
            connection.close()
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)


class CsrfTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.harness.create_user("alice", role="admin")
        self.client = self.harness.client()
        login(self.client, "alice")

    def tearDown(self):
        self.harness.close()

    def test_state_changing_without_csrf_header_is_rejected(self):
        response = self.client.post("/api/projects", json={"name": "无令牌"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "CSRF_FAILED")

    def test_wrong_csrf_header_is_rejected(self):
        response = self.client.post(
            "/api/projects", json={"name": "错令牌"}, headers={"X-CSRF-Token": "wrong"}
        )
        self.assertEqual(response.status_code, 403)

    def test_cross_origin_write_is_rejected(self):
        headers = csrf_headers(self.client)
        headers["Origin"] = "https://evil.example"
        response = self.client.post("/api/projects", json={"name": "跨站"}, headers=headers)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "CSRF_ORIGIN")

    def test_form_post_is_rejected(self):
        headers = csrf_headers(self.client)
        response = self.client.post(
            "/api/projects", data="name=x", headers=headers, content_type="application/x-www-form-urlencoded"
        )
        self.assertEqual(response.status_code, 415)

    def test_safe_get_issues_csrf_cookie(self):
        fresh = self.harness.client()
        fresh.get("/api/meta")
        self.assertIsNotNone(fresh.get_cookie("hx17_csrf"))


class PasswordChangeTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.harness.create_user("alice")
        self.other = self.harness.client()
        self.client = self.harness.client()
        login(self.other, "alice", "correct-horse-9")
        login(self.client, "alice", "correct-horse-9")

    def tearDown(self):
        self.harness.close()

    def test_change_password_keeps_current_and_drops_other_device(self):
        response = api(
            self.client,
            "POST",
            "/api/auth/password",
            {"current_password": "correct-horse-9", "new_password": "brand-new-pass-2"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        self.assertEqual(self.other.get("/api/auth/me").status_code, 401)
        # 新密码可登录，旧密码不可。
        fresh = self.harness.client()
        fresh.get("/api/meta")
        ok = fresh.post(
            "/api/auth/login",
            json={"username": "alice", "password": "brand-new-pass-2"},
            headers=csrf_headers(fresh),
        )
        self.assertEqual(ok.status_code, 200)
        stale = self.harness.client()
        stale.get("/api/meta")
        bad = stale.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct-horse-9"},
            headers=csrf_headers(stale),
        )
        self.assertEqual(bad.status_code, 401)

    def test_wrong_current_password(self):
        response = api(
            self.client,
            "POST",
            "/api/auth/password",
            {"current_password": "wrong-one-123", "new_password": "brand-new-pass-2"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "WRONG_PASSWORD")

    def test_weak_new_password_is_rejected(self):
        response = api(
            self.client,
            "POST",
            "/api/auth/password",
            {"current_password": "correct-horse-9", "new_password": "short"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "WEAK_PASSWORD")


class NoRegistrationTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.client = self.harness.client()

    def tearDown(self):
        self.harness.close()

    def test_there_is_no_register_endpoint(self):
        self.client.get("/api/meta")
        response = self.client.post(
            "/api/auth/register",
            json={"username": "mallory", "password": "whatever-123"},
            headers=csrf_headers(self.client),
        )
        self.assertEqual(response.status_code, 404)

    def test_login_throttle_after_repeated_failures(self):
        self.harness.create_user("alice")
        self.client.get("/api/meta")
        statuses = []
        for _ in range(9):
            response = self.client.post(
                "/api/auth/login",
                json={"username": "alice", "password": "wrong-password-1"},
                headers=csrf_headers(self.client),
            )
            statuses.append(response.status_code)
        self.assertEqual(statuses[-1], 429)
        self.assertEqual(statuses[0], 401)

    def test_password_hash_is_not_reversible_marker(self):
        self.harness.create_user("carol")
        connection = self.harness.conn()
        try:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE username = 'carol'"
            ).fetchone()
        finally:
            connection.close()
        self.assertNotIn("correct-horse-9", row["password_hash"])
        self.assertTrue(row["password_hash"].startswith(("scrypt$", "pbkdf2$")))
        self.assertTrue(security.verify_password("correct-horse-9", row["password_hash"]))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from tests.postgres_fixture import postgres_for_test
from src.bot_security.passwords import hash_password
from src.bot_security.store import SecurityError, SecurityStore

PASSWORD = "local-test-password-only"
QQ = "123456789"
BOT = "987654321"


class AccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.store = SecurityStore(postgres_for_test(self), b"x" * 32, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.admin = self.store.bootstrap("kenneth", PASSWORD, QQ)

    def test_password_is_hashed_and_cookie_session_revoked_on_logout(self):
        with self.store.transaction() as db:
            encoded = db.execute("SELECT password_hash FROM admin_accounts").fetchone()[0]
        self.assertTrue(encoded.startswith("$argon2id$"))
        self.assertNotIn(PASSWORD, encoded)
        result = self.store.login("kenneth", PASSWORD, "test")
        account = self.store.session(result["session"], result["csrf"])
        self.assertEqual(account["role"], "admin")
        self.assertNotIn("password_hash", account)
        with self.assertRaises(SecurityError):
            self.store.session(result["session"], "wrong-csrf")
        self.store.logout(result["session"])
        with self.assertRaises(SecurityError):
            self.store.session(result["session"])

    def test_missing_disabled_and_wrong_password_use_same_error(self):
        for username, password in (("missing", PASSWORD), ("kenneth", "wrong")):
            with self.assertRaisesRegex(SecurityError, "账户或密码不正确"):
                self.store.login(username, password, "test")
        with self.store.transaction() as db:
            db.execute("UPDATE admin_accounts SET enabled=0")
        with self.assertRaisesRegex(SecurityError, "账户或密码不正确"):
            self.store.login("kenneth", PASSWORD, "test")

    def test_login_throttled_and_expires(self):
        for _ in range(10):
            with self.assertRaises(SecurityError):
                self.store.login("kenneth", "wrong", "test")
        with self.assertRaisesRegex(SecurityError, "频繁"):
            self.store.login("kenneth", PASSWORD, "test")
        self.now += 601
        result = self.store.login("kenneth", PASSWORD, "test", lifetime=2)
        self.now += 3
        with self.assertRaises(SecurityError):
            self.store.session(result["session"])

    def test_account_change_revokes_sessions_and_qq_binding(self):
        credentials = self.store.login("kenneth", PASSWORD, "test")
        self.store.apply_account_change(self.admin, {"action": "update", "account_id": self.admin["account_id"],
            "expected_version": 1, "changes": {"qq_id": "333333333"}})
        self.assertIsNone(self.store.account_for_qq(QQ))
        with self.assertRaises(SecurityError):
            self.store.session(credentials["session"])

    def test_last_admin_cannot_be_disabled(self):
        self.store.apply_account_change(self.admin, {"action": "create", "username": "viewer",
            "password_hash": hash_password(PASSWORD), "role": "member", "qq_id": None})
        with self.assertRaisesRegex(SecurityError, "最后一个管理员"):
            self.store.apply_account_change(self.admin, {"action": "update", "account_id": self.admin["account_id"],
                "expected_version": 1, "changes": {"enabled": False}})
        with self.assertRaises(SecurityError):
            self.store.bootstrap("hacker", PASSWORD, "333333333")


if __name__ == "__main__":
    unittest.main()

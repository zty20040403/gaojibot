from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from src.bot_security.passwords import hash_password
from src.bot_security.service import MobileAuthorization, approval_command
from src.bot_security.store import SecurityError, SecurityStore

PASSWORD = "local-test-password-only"
QQ = "123456789"
BOT = "987654321"


class AccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "auth.sqlite3"
        self.now = 1000
        self.store = SecurityStore(self.path, b"x" * 32, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.admin = self.store.bootstrap("kenneth", PASSWORD, QQ)

    def proposal(self, *, account=None, payload=None):
        request, code = self.store.propose(account or self.admin, bot_id=BOT, kind="test",
            payload=payload or {"action": "restart", "unit": "example.service"}, summary="重启测试服务")
        self.store.delivered(request["approval_id"], request["code_generation"], True)
        return request, code

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

    def test_six_digits_preserve_leading_zero_and_not_persisted(self):
        with patch("src.bot_security.store.secrets.randbelow", return_value=42):
            request, code = self.proposal()
        self.assertEqual(code, "000042")
        with self.store.transaction() as db:
            rows = db.execute("SELECT * FROM admin_approvals").fetchall()
        self.assertNotIn(code, json.dumps([dict(row) for row in rows]))
        self.store.confirm(request["approval_id"], QQ, BOT, code)
        self.assertEqual(self.store.get(request["approval_id"], self.admin["account_id"])["status"], "queued")

    def test_only_bound_qq_bot_and_operation_can_confirm(self):
        request, code = self.proposal()
        other, _ = self.proposal(payload={"action": "delete"})
        for identifier, qq, bot in ((request["approval_id"], "222222222", BOT), (request["approval_id"], QQ, "111111111"), (other["approval_id"], QQ, BOT)):
            with self.assertRaises(SecurityError):
                self.store.confirm(identifier, qq, bot, code)
        self.store.confirm(request["approval_id"], QQ, BOT, code)
        with self.assertRaises(SecurityError):
            self.store.confirm(request["approval_id"], QQ, BOT, code)

    def test_code_only_selects_its_operation_not_the_newest(self):
        with patch("src.bot_security.store.secrets.randbelow", side_effect=[42, 43]):
            first, code = self.proposal()
            second, _ = self.proposal(payload={"action": "another"})
        for qq, bot in (("222222222", BOT), (QQ, "111111111")):
            with self.assertRaises(SecurityError):
                self.store.confirm(None, qq, bot, code)
        accepted = self.store.confirm(None, QQ, BOT, code)
        self.assertEqual(accepted["approval_id"], first["approval_id"])
        self.assertEqual(self.store.get(second["approval_id"], self.admin["account_id"])["status"], "pending")
        with self.assertRaises(SecurityError):
            self.store.confirm(None, QQ, BOT, code)

    def test_code_only_collision_never_approves_multiple_or_consumed_target(self):
        with patch("src.bot_security.store.secrets.randbelow", return_value=42):
            first, code = self.proposal()
            second, _ = self.proposal(payload={"action": "another"})
        with self.assertRaisesRegex(SecurityError, "多项操作"):
            self.store.confirm(None, QQ, BOT, code)
        self.assertIsNone(self.store.claim("worker"))
        self.store.confirm(first["approval_id"], QQ, BOT, code)
        with self.assertRaisesRegex(SecurityError, "多项操作"):
            self.store.confirm(None, QQ, BOT, code)
        self.assertEqual(self.store.get(second["approval_id"], self.admin["account_id"])["status"], "pending")

    def test_code_only_lockout_expiry_and_resend(self):
        with patch("src.bot_security.store.secrets.randbelow", side_effect=[42, 43]):
            first, code = self.proposal()
            second, _ = self.proposal(payload={"action": "another"})
        for _ in range(5):
            with self.assertRaises(SecurityError):
                self.store.confirm(None, QQ, BOT, "999999")
        for item in (first, second):
            self.assertEqual(self.store.get(item["approval_id"], self.admin["account_id"])["status"], "locked")
        with patch("src.bot_security.store.secrets.randbelow", return_value=44):
            request, new_code = self.store.resend(first["approval_id"], QQ, BOT)
        self.store.delivered(request["approval_id"], request["code_generation"], True)
        with self.assertRaises(SecurityError):
            self.store.confirm(None, QQ, BOT, code)
        self.now += 180
        with self.assertRaises(SecurityError):
            self.store.confirm(None, QQ, BOT, new_code)
        self.assertIsNone(self.store.claim("worker"))

    def test_wrong_codes_lock_at_five_and_expiry_is_enforced(self):
        request, code = self.proposal()
        wrong = "111111" if code != "111111" else "222222"
        for _ in range(5):
            with self.assertRaises(SecurityError):
                self.store.confirm(request["approval_id"], QQ, BOT, wrong)
        self.assertEqual(self.store.get(request["approval_id"], self.admin["account_id"])["status"], "locked")
        with self.assertRaises(SecurityError):
            self.store.confirm(request["approval_id"], QQ, BOT, code)
        other, other_code = self.proposal(payload={"action": "another"})
        self.now += 180
        with self.assertRaises(SecurityError):
            self.store.confirm(other["approval_id"], QQ, BOT, other_code)

    def test_resend_never_reuses_any_previous_six_digits(self):
        with patch("src.bot_security.store.secrets.randbelow", return_value=42):
            request, first = self.proposal()
        with patch("src.bot_security.store.secrets.randbelow", side_effect=[42, 43]):
            resent, second = self.store.resend(request["approval_id"], QQ, BOT)
        self.store.delivered(resent["approval_id"], resent["code_generation"], True)
        with patch("src.bot_security.store.secrets.randbelow", side_effect=[42, 43, 44]):
            newest, third = self.store.resend(request["approval_id"], QQ, BOT)
        self.store.delivered(newest["approval_id"], newest["code_generation"], True)
        for old in (first, second):
            with self.assertRaises(SecurityError):
                self.store.confirm(request["approval_id"], QQ, BOT, old)
        self.store.confirm(request["approval_id"], QQ, BOT, third)

    def test_restarting_store_does_not_revive_consumed_code(self):
        request, code = self.proposal()
        self.store.confirm(request["approval_id"], QQ, BOT, code)
        restarted = SecurityStore(self.path, b"x" * 32, clock=lambda: self.now)
        self.addCleanup(restarted.close)
        with self.assertRaises(SecurityError):
            restarted.confirm(request["approval_id"], QQ, BOT, code)
        self.assertIsNotNone(restarted.claim("worker"))
        self.assertIsNone(self.store.claim("other-worker"))

    def test_parallel_confirmations_across_stores_queue_once(self):
        request, code = self.proposal()
        second = SecurityStore(self.path, b"x" * 32, clock=lambda: self.now)
        self.addCleanup(second.close)
        def confirm(index):
            try:
                (self.store if index % 2 else second).confirm(request["approval_id"] if index % 2 else None, QQ, BOT, code)
                return True
            except SecurityError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(confirm, range(12)))
        self.assertEqual(sum(outcomes), 1)
        self.assertIsNotNone(second.claim("one"))
        self.assertIsNone(self.store.claim("two"))

    def test_mutating_payload_after_challenge_invalidates_it(self):
        request, code = self.proposal()
        with self.store.transaction() as db:
            db.execute("UPDATE admin_approvals SET payload_json=? WHERE approval_id=?", ('{"action":"delete"}', request["approval_id"]))
        with self.assertRaises(SecurityError):
            self.store.confirm(request["approval_id"], QQ, BOT, code)
        self.assertIsNone(self.store.claim("worker"))

    def test_logout_cancels_queued_operation(self):
        credentials = self.store.login("kenneth", PASSWORD, "test")
        account = self.store.session(credentials["session"])
        request, code = self.proposal(account=account)
        self.store.confirm(request["approval_id"], QQ, BOT, code)
        self.store.logout(credentials["session"])
        self.assertIsNone(self.store.claim("worker"))

    def test_account_change_revokes_sessions_and_qq_binding(self):
        credentials = self.store.login("kenneth", PASSWORD, "test")
        request, code = self.proposal()
        self.store.apply_account_change(self.admin, {"action": "update", "account_id": self.admin["account_id"], "expected_version": 1, "changes": {"qq_id": "333333333"}})
        self.assertIsNone(self.store.account_for_qq(QQ))
        with self.assertRaises(SecurityError):
            self.store.session(credentials["session"])
        with self.assertRaises(SecurityError):
            self.store.confirm(request["approval_id"], QQ, BOT, code)

    def test_member_cannot_propose_and_last_admin_cannot_be_disabled(self):
        member = self.store.apply_account_change(self.admin, {"action": "create", "username": "viewer", "password_hash": hash_password(PASSWORD), "role": "member", "qq_id": None})
        with self.assertRaises(SecurityError):
            self.proposal(account=member)
        with self.assertRaisesRegex(SecurityError, "最后一个管理员"):
            self.store.apply_account_change(self.admin, {"action": "update", "account_id": self.admin["account_id"], "expected_version": 1, "changes": {"enabled": False}})
        with self.assertRaises(SecurityError):
            self.store.bootstrap("hacker", PASSWORD, "333333333")

    def test_unknown_execution_never_replayed_after_lease_loss(self):
        request, code = self.proposal()
        self.store.confirm(request["approval_id"], QQ, BOT, code)
        self.assertIsNotNone(self.store.claim("old-worker"))
        self.now += 61
        self.assertIsNone(self.store.claim("new-worker"))
        self.assertEqual(self.store.get(request["approval_id"], self.admin["account_id"])["status"], "needs_attention")


class MobileAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = SecurityStore(":memory:", b"z" * 32)
        self.addCleanup(self.store.close)
        self.account = self.store.bootstrap("kenneth", PASSWORD, QQ)
        self.sent = []
        async def sender(bot, qq, text):
            self.sent.append((bot, qq, text))
        self.mobile = MobileAuthorization(self.store, sender=sender, bot_selector=lambda: BOT)

    async def test_phone_confirmation_executes_after_browser_disappears(self):
        import re
        result = await self.mobile.propose(self.account, kind="test", payload={"action": "restart"}, summary="重启示例")
        self.assertNotIn("code", result)
        code = re.search(r"一次性口令：([0-9]{6})", self.sent[-1][2])[1]
        calls = []
        async def execute(request):
            calls.append(request["payload"])
            return {"ok": True}
        self.mobile.executors["test"] = execute
        self.assertFalse(await self.mobile.run_once())
        group = await self.mobile.handle_message(qq_id=QQ, bot_id=BOT, private=False, text=f"确认 {result['approval_id']} {code}")
        self.assertIn("只接受", group)
        self.assertFalse(await self.mobile.run_once())
        response = await self.mobile.handle_message(qq_id=QQ, bot_id=BOT, private=True, text=code)
        self.assertIn("已授权", response)
        self.assertTrue(self.mobile.wake.is_set())
        self.assertTrue(await self.mobile.run_once())
        self.assertFalse(await self.mobile.run_once())
        self.assertEqual(calls, [{"action": "restart"}])
        self.assertNotIn(code, self.sent[-1][2])

    async def test_delivery_failure_cannot_be_approved_or_executed(self):
        async def failed(*_args):
            raise OSError("transport failure")
        self.mobile.sender = failed
        with self.assertRaisesRegex(SecurityError, "发送失败"):
            await self.mobile.propose(self.account, kind="test", payload={}, summary="测试")
        self.assertFalse(await self.mobile.run_once())
        self.assertEqual(self.store.pending(self.account["account_id"])[0]["status"], "delivery_failed")

    def test_confirmation_command_recognizes_group_and_private_syntax(self):
        self.assertTrue(approval_command("确认 AP-123456789ABC 000042"))
        self.assertTrue(approval_command("/confirm AP-123456789ABC wrong"))
        self.assertFalse(approval_command("普通聊天"))
        self.assertTrue(approval_command(" 000042 ", private=True))
        self.assertFalse(approval_command("000042"))
        self.assertFalse(approval_command("金额 000042", private=True))


if __name__ == "__main__":
    unittest.main()

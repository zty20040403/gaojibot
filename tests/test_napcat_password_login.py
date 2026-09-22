import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location(
    "napcat_password_login", Path(__file__).parents[1] / "nix/napcat-password-login.py",
)
login = importlib.util.module_from_spec(spec)
spec.loader.exec_module(login)


class PasswordLoginTests(unittest.TestCase):
    def test_readiness_waits_for_local_webui_without_login(self):
        with patch.object(login.socket, "create_connection", side_effect=ConnectionRefusedError):
            self.assertFalse(login.webui_ready(6100))
        with patch.object(login.socket, "create_connection") as connect:
            self.assertTrue(login.webui_ready(6100))
            connect.assert_called_once_with(("127.0.0.1", 6100), timeout=2)

    def setUp(self):
        self.state = {"schema": 1, "uin": "123456789", "port": 6100, "blocked": "", "attempts": []}
        self.saved = []
        self.password = Mock(return_value="private-password")
        self.client = Mock()
        self.client.online_account.return_value = "123456789"

    def run_login(self, responses):
        self.client.call.side_effect = responses
        return login.run_once(self.client, self.state, self.password,
                              lambda value: self.saved.append(copy.deepcopy(value)), 100000)

    def test_password_login_requires_verified_matching_account(self):
        self.assertEqual(self.run_login([
            {"isLogin": False, "isOffline": False}, {}, {"isLogin": True},
        ]), "online")
        self.client.call.assert_any_call("QQLogin/PasswordLogin", {
            "uin": "123456789", "passwordMd5": hashlib.md5(b"private-password").hexdigest(),
        })
        self.assertEqual(self.saved[0]["blocked"], "attempt_unconfirmed")
        self.assertEqual(self.saved[0]["attempts"], [100000])
        self.assertEqual(self.state["blocked"], "")
        self.assertNotIn("private-password", json.dumps(self.saved))
        self.assertNotIn(hashlib.md5(b"private-password").hexdigest(), json.dumps(self.saved))

    def test_challenges_stop_until_human_verifies(self):
        for response, reason in [({"needCaptcha": True, "proofWaterUrl": "secret"}, "captcha_required"),
                                 ({"needNewDevice": True, "jumpUrl": "secret"}, "device_confirmation_required")]:
            with self.subTest(reason=reason):
                self.state.update(blocked="", attempts=[])
                self.assertEqual(self.run_login([{"isLogin": False, "isOffline": False}, response]), reason)
                self.assertEqual(self.state["blocked"], reason)
                self.password.reset_mock()
                self.assertEqual(self.run_login([{"isLogin": False, "isOffline": False}]), "manual_required")
                self.password.assert_not_called()
        self.assertNotIn("secret", json.dumps(self.saved))

    def test_lost_response_or_api_rejection_is_not_replayed(self):
        self.assertEqual(self.run_login([
            {"isLogin": False, "isOffline": False}, login.LoginError("api_unavailable"),
        ]), "login_not_verified")
        self.password.reset_mock()
        self.assertEqual(self.run_login([{"isLogin": False, "isOffline": False}]), "manual_required")
        self.password.assert_not_called()

    def test_submission_is_not_success_and_later_login_clears_latch(self):
        self.assertEqual(self.run_login([
            {"isLogin": False, "isOffline": False}, {}, {"isLogin": False, "isOffline": False},
        ]), "login_submitted_not_verified")
        self.state["notification"] = {"status": "sent"}
        self.password.reset_mock()
        self.assertEqual(self.run_login([{"isLogin": True}]), "online")
        self.password.assert_not_called()
        self.assertEqual(self.state["attempts"], [100000])
        self.assertEqual(self.state["blocked"], "")
        self.assertNotIn("notification", self.state)

    def test_different_online_account_is_never_replaced(self):
        self.client.online_account.return_value = "987654321"
        self.assertEqual(self.run_login([{"isLogin": True}]), "different_account_online")
        self.password.assert_not_called()

    def test_stale_webui_flag_is_not_treated_as_verified_online(self):
        self.client.online_account.side_effect = login.LoginError("online_unverified")
        with self.assertRaises(login.LoginError):
            self.run_login([{"isLogin": True}])
        self.password.assert_not_called()
        self.assertFalse(self.saved)

    def test_waits_for_existing_session_and_fails_closed_on_unknown(self):
        for response, expected in [
            ({"isLogin": False, "isOffline": True}, "waiting_for_existing_session"),
            ({"isLogin": False, "isOffline": False, "loginPhase": "initializing"}, "waiting_for_existing_session"),
            ({"qrLoginAccepted": True}, "waiting_for_existing_session"),
            ({}, "unknown_login_state"),
        ]:
            with self.subTest(response=response):
                self.assertEqual(self.run_login([response]), expected)
                self.password.assert_not_called()

    def test_security_warning_blocks_login_but_expired_session_can_attempt(self):
        self.assertEqual(self.run_login([
            {"isLogin": False, "isOffline": False, "loginError": "设备存在外挂，风险提示"},
        ]), "security_confirmation_required")
        self.password.assert_not_called()
        self.state["blocked"] = ""
        self.assertEqual(self.run_login([
            {"isLogin": False, "isOffline": False, "loginError": "用户身份已失效，请重新登录"},
            {"needCaptcha": True},
        ]), "captcha_required")

    def test_limits_and_clock_rollback(self):
        for attempts, expected in [([99999], "rate_limited"), ([20000, 50000, 90000], "rate_limited"),
                                   ([100001], "clock_changed")]:
            with self.subTest(attempts=attempts):
                self.state["attempts"] = attempts
                self.assertEqual(self.run_login([{"isLogin": False, "isOffline": False}]), expected)
                self.password.assert_not_called()

    def test_pre_submission_persistence_failure_prevents_login(self):
        self.client.call.return_value = {"isLogin": False, "isOffline": False}
        with self.assertRaises(OSError):
            login.run_once(self.client, self.state, self.password, Mock(side_effect=OSError()), 100000)
        self.assertEqual(self.client.call.call_count, 1)

    def test_private_files_state_validation_and_crash_latch(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            self.state.update(blocked="attempt_unconfirmed", attempts=[100000])
            login.save_state(path, self.state)
            self.assertEqual(login.load_state(path, "123456789", 6100), self.state)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(login.LoginError):
                login.load_state(path, "987654321", 6100)
            path.chmod(0o644)
            with self.assertRaises(login.LoginError):
                login.read_private(path)
            path.chmod(0o600)
            link = Path(folder) / "symlink"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                login.read_private(link)
            path.write_text('{"schema":1}')
            with self.assertRaises(login.LoginError):
                login.load_state(path, "123456789", 6100)

    def test_api_errors_do_not_reveal_credentials_and_two_factor_stops(self):
        client = login.Client("webui-secret", 6100)
        response = io.BytesIO(json.dumps({"code": -1, "message": "sensitive-response"}).encode())
        client.http = Mock()
        client.http.open.return_value = response
        with self.assertRaisesRegex(login.LoginError, "^api_rejected$"):
            client.call("QQLogin/PasswordLogin", {"passwordMd5": "secret"})
        client.call = Mock(return_value={"require2FA": True, "Credential": "not-usable"})
        with self.assertRaisesRegex(login.LoginError, "management_auth_required"):
            client.authenticate()
        self.assertEqual(client.credential, "")
        self.assertIsNone(login.NoRedirect().redirect_request(None, None, 302, "", {}, "http://example.com"))

    def test_initialization_requires_hidden_interactive_input(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "password"
            with patch.object(login.sys.stdin, "isatty", return_value=False):
                with self.assertRaises(login.LoginError):
                    login.init_password(path)
            self.assertFalse(path.exists())
            with patch.object(login.sys.stdin, "isatty", return_value=True), \
                    patch.object(login.getpass, "getpass", side_effect=["test-secret", "test-secret"]):
                login.init_password(path)
            self.assertEqual(login.read_private(path), "test-secret")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class ManualNotificationTests(unittest.TestCase):
    def setUp(self):
        self.state = {"blocked": "device_confirmation_required", "attempts": [100000]}
        self.saved = []
        self.client = Mock()
        self.client.online_account.return_value = "987654321"
        self.client.call.side_effect = [
            {"retcode": 0, "data": {"user_id": 111111}},
            {"retcode": 0, "data": {"message_id": -12345}},
        ]
        self.factory = Mock(return_value=self.client)

    def notify(self, now=100010, save=None):
        return login.notify_manual_required(
            self.state, self.factory, sender_uin="987654321", port=6099,
            group_id="222222", user_id="111111", now=now,
            save=save or (lambda value: self.saved.append(copy.deepcopy(value))),
        )

    def test_notice_mentions_only_owner_and_persists_before_send(self):
        def call(endpoint, payload):
            self.assertEqual(endpoint, "Debug/call")
            if payload["action"] == "get_group_member_info":
                return {"retcode": 0, "data": {"user_id": 111111}}
            self.assertEqual(self.saved[-1]["notification"]["status"], "sending")
            self.assertEqual(payload["params"]["group_id"], 222222)
            segments = payload["params"]["message"]
            self.assertEqual(segments[0], {"type": "at", "data": {"qq": "111111"}})
            self.assertIn("设备确认", segments[1]["data"]["text"])
            self.assertNotIn("http", segments[1]["data"]["text"])
            return {"retcode": 0, "data": {"message_id": -12345}}

        self.client.call.side_effect = call
        self.assertEqual(self.notify(), "sent")
        self.assertEqual(self.state["notification"]["message_id"], "-12345")
        self.state = json.loads(json.dumps(self.state))
        self.assertIsNone(self.notify(now=200000))
        self.factory.assert_called_once()

    def test_preflight_failure_retries_without_sending(self):
        self.client.authenticate.side_effect = login.LoginError("api_unavailable")
        self.assertEqual(self.notify(), "waiting_for_sender")
        self.client.call.assert_not_called()
        self.assertIsNone(self.notify(now=100200))
        self.factory.assert_called_once()
        self.client.authenticate.side_effect = None
        self.assertEqual(self.notify(now=100310), "sent")

    def test_wrong_sender_or_recipient_never_sends(self):
        self.client.online_account.return_value = "555555"
        self.assertEqual(self.notify(), "waiting_for_sender")
        self.client.call.assert_not_called()
        self.client.online_account.return_value = "987654321"
        self.client.call.side_effect = [{"retcode": 0, "data": {"user_id": 555555}}]
        self.assertEqual(self.notify(now=100310), "waiting_for_sender")
        self.assertEqual(self.client.call.call_count, 1)

    def test_ambiguous_receipt_and_crash_are_not_replayed(self):
        self.client.call.side_effect = [
            {"retcode": 0, "data": {"user_id": 111111}}, login.LoginError("api_unavailable"),
        ]
        self.assertEqual(self.notify(), "unconfirmed")
        self.assertIsNone(self.notify(now=200000))
        self.state["notification"]["status"] = "sending"
        self.assertIsNone(self.notify(now=300000))
        self.factory.assert_called_once()

    def test_save_failure_prevents_side_effect(self):
        with self.assertRaises(OSError):
            self.notify(save=Mock(side_effect=OSError()))
        self.assertEqual(self.client.call.call_count, 1)

    def test_no_incident_is_noop_and_new_incident_can_notify(self):
        self.state["blocked"] = ""
        self.assertIsNone(self.notify())
        self.factory.assert_not_called()
        self.state["blocked"] = "captcha_required"
        self.assertEqual(self.notify(), "sent")
        self.state["attempts"].append(200000)
        self.client.call.side_effect = [
            {"retcode": 0, "data": {"user_id": 111111}},
            {"retcode": 0, "data": {"message_id": 12346}},
        ]
        self.assertEqual(self.notify(now=200010), "sent")


if __name__ == "__main__":
    unittest.main()

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
        self.password.reset_mock()
        self.assertEqual(self.run_login([{"isLogin": True}]), "online")
        self.password.assert_not_called()
        self.assertEqual(self.state["attempts"], [100000])
        self.assertEqual(self.state["blocked"], "")

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


if __name__ == "__main__":
    unittest.main()

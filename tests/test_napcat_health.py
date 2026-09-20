import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("napcat_health", Path(__file__).parents[1] / "nix/napcat-health.py")
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class NapCatHealthTests(unittest.TestCase):
    def test_waiting_for_qr_does_not_call_uninitialized_onebot(self):
        client = Mock()
        client.open.side_effect = [
            io.BytesIO(json.dumps({"code": 0, "data": {"Credential": "test"}}).encode()),
            io.BytesIO(json.dumps({"code": 0, "data": {
                "isLogin": False, "isOffline": False, "qrcodeurl": "test-qr",
            }}).encode()),
        ]
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps({"token": "test-token"}))
            with patch.object(health.urllib.request, "build_opener", return_value=client):
                self.assertEqual(health.probe(config, 6100), "login_required")
        self.assertEqual(client.open.call_count, 2)

    def test_actual_account_beats_stale_webui_error(self):
        self.assertEqual(health.classify({"loginError": "用户身份已失效"}, {"online": True, "good": True}), "online")
        self.assertEqual(health.classify({}, {"online": False, "good": True}), "transport_offline")
        self.assertEqual(health.classify({"loginError": "用户身份已失效"}, {"online": False}), "login_required")

    def test_login_and_manual_stop_never_restart(self):
        for status in ("login_required", "management_auth_required", "stopped", "unknown"):
            state, restart = health.decide({"failures": 50}, status, 100000)
            self.assertFalse(restart)
            self.assertEqual(state["failures"], 0)

    def test_three_failures_then_persisted_cooldown(self):
        state = {}
        for index in range(3):
            state, restart = health.decide(state, "unresponsive", 100000 + index * 30)
            self.assertEqual(restart, index == 2)
        for index in range(1, 10):
            state, restart = health.decide(state, "unresponsive", 100060 + index * 30)
            self.assertFalse(restart)

    def test_daily_budget_survives_online_transitions(self):
        state = {"restarts": [50000, 51000, 52000], "last_restart": 52000}
        state, _ = health.decide(state, "online", 60000)
        for index in range(5):
            state, restart = health.decide(state, "transport_offline", 60100 + index * 30)
            self.assertFalse(restart)
        _, restart = health.decide(state, "transport_offline", 200000)
        self.assertTrue(restart)

    def test_metrics_do_not_claim_process_is_account(self):
        state, _ = health.decide({}, "login_required", 100000)
        output = health.metrics(state)
        self.assertIn("gaoji_qq_online 0\n", output)
        self.assertIn("gaoji_qq_login_required 1\n", output)


if __name__ == "__main__":
    unittest.main()

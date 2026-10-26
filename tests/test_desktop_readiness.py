import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.local_runtime.readiness import connection_status


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "state").mkdir()
        self.path = self.root / "state/desktop-health.json"
        self.config = {"settings": {"bot_qq": "12345678"}}
        self.services = [{"name": "bot", "state": "running"}]

    def check(self, record):
        self.path.write_text(json.dumps(record))
        with patch("src.local_runtime.readiness.time.time", return_value=100):
            return connection_status(self.root, self.config, self.services)

    def test_requires_running_container_current_account_and_recent_heartbeat(self):
        value = {"qq": "12345678", "updated_at": 98, "connected": True}
        self.assertEqual(self.check(value), "connected")
        self.assertEqual(self.check({**value, "connected": False}), "disconnected")
        self.assertEqual(self.check({**value, "updated_at": 10}), "stale")
        self.assertEqual(self.check({**value, "updated_at": 110}), "stale")
        self.assertEqual(self.check({**value, "qq": "87654321"}), "wrong_account")
        self.assertEqual(connection_status(self.root, self.config, []), "not_running")

    def test_invalid_and_missing_heartbeat_never_claim_ready(self):
        self.assertEqual(connection_status(self.root, self.config, self.services), "unavailable")
        for value in ([], {}, {"updated_at": True}, {"updated_at": 99, "qq": "12345678", "connected": "false"}):
            self.assertEqual(self.check(value), "unavailable")
        self.path.write_text("x" * 4097)
        self.assertEqual(connection_status(self.root, self.config, self.services), "unavailable")
        self.path.unlink()
        self.path.symlink_to(self.root / "private")
        self.assertEqual(connection_status(self.root, self.config, self.services), "unavailable")

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


spec = importlib.util.spec_from_file_location(
    "native_config", Path(__file__).parents[1] / "nix/napcat-native-config.py",
)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class NativeConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.onebot = self.directory / "onebot11_123456789.json"
        self.webui = self.directory / "webui.json"
        self.token = self.directory / "credential"
        self.token.write_text("x" * 48)
        self.url = "ws://127.0.0.1:18080/onebot/v11/ws"
        self.previous = "ws://host.docker.internal:18080/onebot/v11/ws"

    def configure(self):
        native.configure(self.directory, "123456789", self.url, self.previous,
                         "127.0.0.1", 6100, self.token)

    def test_fresh_instance_gets_private_loopback_configuration(self):
        self.configure()
        onebot = json.loads(self.onebot.read_text())
        client = onebot["network"]["websocketClients"][0]
        self.assertEqual(client["url"], self.url)
        self.assertEqual(client["token"], "x" * 48)
        self.assertFalse(client["reportSelfMessage"])
        webui = json.loads(self.webui.read_text())
        self.assertEqual((webui["host"], webui["port"]), ("127.0.0.1", 6100))
        self.assertGreaterEqual(len(webui["token"]), 32)
        for path in [self.onebot, self.webui]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_migration_keeps_identity_and_unrelated_settings(self):
        self.onebot.write_text(json.dumps({"musicSignUrl": "custom", "network": {
            "websocketClients": [
                {"name": "qq-deepseek-bot", "url": self.previous, "enable": True},
                {"name": "unused", "url": "ws://other.invalid", "enable": False},
            ], "httpServers": [],
        }}))
        self.webui.write_text(json.dumps({"token": "existing-secret", "host": "::", "port": 6099,
                                        "twoFactorSecret": "keep-private", "loginRate": 10}))
        self.configure()
        onebot = json.loads(self.onebot.read_text())
        self.assertEqual(onebot["musicSignUrl"], "custom")
        clients = onebot["network"]["websocketClients"]
        self.assertEqual(len(clients), 2)
        self.assertEqual(clients[0]["url"], self.url)
        self.assertFalse(clients[1]["enable"])
        webui = json.loads(self.webui.read_text())
        self.assertEqual(webui["token"], "existing-secret")
        self.assertEqual(webui["twoFactorSecret"], "keep-private")
        before = (self.onebot.read_bytes(), self.webui.read_bytes(), self.onebot.stat().st_ino)
        self.configure()
        self.assertEqual(before, (self.onebot.read_bytes(), self.webui.read_bytes(), self.onebot.stat().st_ino))

    def test_unexpected_or_duplicate_clients_stop_without_writing(self):
        for clients in [
            [{"url": "ws://other.invalid", "enable": True}],
            [{"url": self.url}, {"url": self.previous}],
        ]:
            with self.subTest(clients=clients):
                self.onebot.write_text(json.dumps({"network": {"websocketClients": clients}}))
                before = self.onebot.read_bytes()
                with self.assertRaises(ValueError):
                    self.configure()
                self.assertEqual(self.onebot.read_bytes(), before)
                self.assertFalse(self.webui.exists())

    def test_bad_credential_does_not_write(self):
        self.token.write_text("short")
        with self.assertRaises(ValueError):
            self.configure()
        self.assertFalse(self.onebot.exists())
        self.assertFalse(self.webui.exists())

    def test_malformed_webui_is_not_replaced(self):
        self.webui.write_text("invalid-json")
        with self.assertRaises(ValueError):
            self.configure()
        self.assertEqual(self.webui.read_text(), "invalid-json")
        self.assertFalse(self.onebot.exists())


if __name__ == "__main__":
    unittest.main()

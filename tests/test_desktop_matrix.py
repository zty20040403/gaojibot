from __future__ import annotations

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import unquote

from src.local_runtime.backups import BackupStore, verify_archive
from src.local_runtime.components import ComponentError, DEFAULTS, component_keys, validate_components
from src.local_runtime.entrypoint import main
from src.local_runtime.instances import InstanceError, InstanceStore
from src.local_runtime.runtime import runtime_environment
from src.local_runtime.setup_clients import SetupError, probe_matrix


KEY = "synthetic-matrix-access-token-not-real"
USER = "@fixture:example.test"
ROOM = "!fixture:example.test"


def options():
    return {"enabled": True, "base_url": "https://matrix.invalid", "user_id": USER,
        "routes": [{"id": "a" * 32, "qq_group": "12345678", "room_id": ROOM}]}


class MatrixSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = InstanceStore(Path(temporary.name))
        config = self.store.create("matrix-test")
        self.identifier = config["id"]
        self.settings = config["settings"]
        self.settings.update(bot_qq="23456789", admin_qq="34567890", default_model="test", group_ids=["12345678"],
            models=[{"id": "test", "model": "fixture", "base_url": "https://model.invalid/v1", "api_key_required": False}])
        self.settings["components"] = deepcopy(DEFAULTS)
        self.settings["components"]["matrix"] = options()

    def save(self, keys=None):
        return self.store.update(self.identifier, self.settings, expected_revision=self.store.read(self.identifier)["revision"], service_keys=keys or {})

    def test_explicit_mapping_and_disabled_secret_exclusion(self):
        self.settings["components"]["matrix"]["routes"].extend([
            {"id": "b" * 32, "qq_group": "12345678", "room_id": "!opaque-room-hash"},
            {"id": "c" * 32, "qq_group": "34567890", "room_id": "!other:example.test"},
        ])
        self.settings["group_ids"].append("34567890")
        self.save({"matrix": KEY})
        env = runtime_environment(*self.store.snapshot(self.identifier))
        self.assertEqual(env["AI_MATRIX_ENABLED"], "true")
        self.assertEqual(env["AI_MATRIX_ACCESS_TOKEN"], KEY)
        self.assertEqual(env["AI_MATRIX_USER_ID"], USER)
        self.assertEqual(env["AI_MATRIX_APPSERVICE_TOKEN"], "")
        routes = json.loads(env["AI_MIRROR_ROUTES_JSON"])
        self.assertEqual(len(routes), 2)
        self.assertEqual([endpoint["id"] for endpoint in routes[0]["endpoints"]], ["12345678", ROOM, "!opaque-room-hash"])
        self.assertEqual(routes[0]["endpoints"][0]["bot_user_id"], "23456789")
        self.assertNotIn(KEY, json.dumps(self.store.public_status(self.identifier)))
        inactive = runtime_environment(*self.store.snapshot(self.identifier), configured=False)
        self.assertNotIn(KEY, json.dumps(inactive))
        self.assertEqual(inactive["AI_MIRROR_ROUTES_JSON"], "[]")
        self.settings["components"]["matrix"]["enabled"] = False
        self.save()
        self.assertEqual(runtime_environment(*self.store.snapshot(self.identifier))["AI_MATRIX_ENABLED"], "false")

    def test_enabled_matrix_needs_credentials_and_enabled_qq_group(self):
        self.save()
        with self.assertRaisesRegex(ComponentError, "Token"):
            runtime_environment(*self.store.snapshot(self.identifier))
        self.settings["group_ids"] = []
        self.save({"matrix": KEY})
        with self.assertRaisesRegex(ComponentError, "已启用"):
            runtime_environment(*self.store.snapshot(self.identifier))
        self.settings["allow_all_groups"] = True
        self.save()
        runtime_environment(*self.store.snapshot(self.identifier))
        self.settings["components"]["matrix"]["routes"] = []
        self.save()
        with self.assertRaisesRegex(ComponentError, "会话映射"):
            runtime_environment(*self.store.snapshot(self.identifier))

    def test_account_or_endpoint_change_does_not_inherit_key(self):
        self.save({"matrix": KEY})
        for field, value in (("base_url", "https://different.invalid"), ("user_id", "@another:example.test")):
            original = self.settings["components"]["matrix"][field]
            self.settings["components"]["matrix"][field] = value
            with self.assertRaisesRegex(InstanceError, "重新填写密钥"):
                self.save()
            self.settings["components"]["matrix"][field] = original
        self.settings["components"]["matrix"]["user_id"] = "@another:example.test"
        self.save({"matrix": ""})

    def test_invalid_routes_and_legacy_configuration(self):
        old = deepcopy(DEFAULTS)
        del old["matrix"]
        self.assertEqual(validate_components(old)["matrix"], DEFAULTS["matrix"])
        bad_route = [
            {"id": "invalid"}, {"qq_group": "abc"}, {"room_id": "#alias:example.test"},
            {"room_id": "!room with space"}, {"host_path": "/root"},
        ]
        for change in bad_route:
            value = options()
            value["routes"][0].update(change)
            with self.subTest(change=change), self.assertRaises(InstanceError):
                validate_components({"matrix": value})
        for change in ({"enabled": 1}, {"base_url": "https://token@bad.invalid"}, {"user_id": "https://bad.invalid"},
            {"routes": [options()["routes"][0], {**options()["routes"][0], "id": "b" * 32, "qq_group": "34567890"}]}):
            with self.subTest(change=change), self.assertRaises(InstanceError):
                validate_components({"matrix": {**options(), **change}})
        with self.assertRaises(ComponentError):
            component_keys({"matrix": "token\nheader"})

    def test_private_backup_keeps_configuration_and_matching_token(self):
        self.save({"matrix": KEY})
        directory = self.store.directory(self.identifier)
        (directory / "config/admin.secret").write_bytes(b"x" * 32)
        dump = directory / "dump"
        dump.write_bytes(b"PGDMPfixture")
        backups = BackupStore(directory)
        record = backups.publish("b" * 32, *self.store.snapshot(self.identifier), dump=dump, checksum="a" * 64, database_bytes=0)
        archived = verify_archive(backups.path(record["id"]) / "snapshot.tar", expected_checksum="a" * 64, max_bytes=record["bytes"])
        self.assertEqual(archived["snapshot"]["private"]["component_key_sets"]["2"]["matrix"], KEY)
        self.assertNotIn(KEY, json.dumps(record))


class MatrixProbeTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.mode = "ready"
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                route = unquote(self.path)
                fixture.calls.append((route, self.headers.get("Authorization")))
                status = 200
                if route.endswith("/account/whoami"):
                    body = {"user_id": "@wrong:example.test" if fixture.mode == "wrong_user" else USER}
                elif route.endswith("/joined_rooms"):
                    body = {"joined_rooms": [] if fixture.mode == "not_joined" else [ROOM]}
                elif route.endswith("/state/m.room.encryption/"):
                    if fixture.mode == "encrypted":
                        body = {"algorithm": "m.megolm.v1.aes-sha2"}
                    else:
                        status, body = 404, {"errcode": "M_NOT_FOUND", "error": "No state"}
                        if fixture.mode == "unknown404": body = {"detail": "Not Found"}
                elif route.endswith("/state/m.room.power_levels/"):
                    body = {"users": {}, "events_default": 50 if fixture.mode == "muted" else 0}
                else:
                    status, body = 500, {}
                if fixture.mode == "invalid_token":
                    status, body = 401, {"error": KEY}
                if fixture.mode == "redirect":
                    status, body = 302, {}
                data = json.dumps(body).encode()
                self.send_response(status)
                if status == 302: self.send_header("Location", "https://must-not-contact.invalid")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join()))
        self.options = {**options(), "base_url": f"http://127.0.0.1:{self.server.server_port}/gateway"}

    def test_real_readonly_identity_membership_encryption_and_permissions(self):
        result = probe_matrix(self.options, KEY)
        self.assertEqual(result["rooms"], 1)
        self.assertEqual(len(self.calls), 4)
        self.assertTrue(all(path.startswith("/gateway/_matrix/client/v3/") and token == "Bearer " + KEY for path, token in self.calls))
        self.assertNotIn(KEY, json.dumps(result))

    def test_errors_and_encryption_are_not_reported_as_ready(self):
        for mode in ("wrong_user", "not_joined", "encrypted", "muted", "unknown404", "invalid_token", "redirect"):
            self.mode = mode
            with self.subTest(mode=mode), self.assertRaises(SetupError) as error:
                probe_matrix(self.options, KEY)
            self.assertNotIn(KEY, str(error.exception))

    def test_probe_entrypoint_never_loads_live_runtime_configuration(self):
        payload = {"options": self.options, "key": KEY}
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", output), \
            patch("src.local_runtime.entrypoint.load_environment") as load:
            self.assertEqual(main(["probe-matrix"]), 0)
            load.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["data"]["rooms"], 1)
        self.assertNotIn(KEY, output.getvalue())


if __name__ == "__main__":
    unittest.main()

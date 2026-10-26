from __future__ import annotations

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, unquote, urlsplit

from src.local_runtime.backups import BackupStore, verify_archive
from src.local_runtime.components import ComponentError, DEFAULTS, component_keys, validate_components
from src.local_runtime.entrypoint import main
from src.local_runtime.instances import InstanceError, InstanceStore
from src.local_runtime.manager import Controller
from src.local_runtime.runtime import runtime_environment
from src.local_runtime.setup_clients import SetupError, probe_imessage


KEY = "synthetic password+&?中文"
CHAT = "iMessage;+;chat-fixture"


def options():
    return {"enabled": True, "base_url": "https://mac.invalid", "bot_handle": "bot@example.invalid",
        "routes": [{"id": "a" * 32, "qq_group": "12345678", "chat_guid": CHAT}]}


class IMessageSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = InstanceStore(Path(temporary.name))
        config = self.store.create("imessage-test")
        self.identifier = config["id"]
        self.settings = config["settings"]
        self.settings.update(bot_qq="23456789", admin_qq="34567890", default_model="test", group_ids=["12345678"],
            models=[{"id": "test", "model": "fixture", "base_url": "https://model.invalid/v1", "api_key_required": False}])
        self.settings["components"] = deepcopy(DEFAULTS)
        self.settings["components"]["imessage"] = options()

    def save(self, keys=None):
        return self.store.update(self.identifier, self.settings, expected_revision=self.store.read(self.identifier)["revision"], service_keys=keys or {})

    def test_matrix_and_imessage_merge_without_duplicate_canonical_group(self):
        self.settings["components"]["matrix"] = {"enabled": True, "base_url": "https://matrix.invalid", "user_id": "@test:example.invalid",
            "routes": [{"id": "b" * 32, "qq_group": "12345678", "room_id": "!room:example.invalid"}]}
        self.save({"matrix": "matrix-fixture-key", "imessage": KEY})
        env = runtime_environment(*self.store.snapshot(self.identifier))
        bundles = json.loads(env["AI_MIRROR_ROUTES_JSON"])
        self.assertEqual(len(bundles), 1)
        self.assertEqual([endpoint["platform"] for endpoint in bundles[0]["endpoints"]], ["onebot-v11", "matrix", "imessage"])
        self.assertEqual(env["AI_IMESSAGE_PASSWORD"], KEY)
        self.assertEqual(env["AI_IMESSAGE_POLL_ENABLED"], "true")
        self.assertEqual(env["AI_IMESSAGE_SEND_METHOD"], "apple-script")
        self.assertEqual(env["AI_IMESSAGE_WEBHOOK_TOKEN"], "")
        self.assertNotIn(KEY, json.dumps(self.store.public_status(self.identifier), ensure_ascii=False))
        self.settings["components"]["imessage"]["enabled"] = False
        self.save()
        env = runtime_environment(*self.store.snapshot(self.identifier))
        self.assertEqual(env["AI_IMESSAGE_POLL_ENABLED"], "false")
        self.assertNotIn(KEY, json.dumps(env, ensure_ascii=False))
        self.assertEqual(len(json.loads(env["AI_MIRROR_ROUTES_JSON"])[0]["endpoints"]), 2)

    def test_disabled_instance_excludes_bridge_password_and_routes(self):
        self.save({"imessage": KEY})
        env = runtime_environment(*self.store.snapshot(self.identifier), configured=False)
        self.assertEqual(env["AI_IMESSAGE_ENABLED"], "false")
        self.assertEqual(env["AI_MIRROR_ROUTES_JSON"], "[]")
        self.assertNotIn(KEY, json.dumps(env, ensure_ascii=False))

    def test_endpoint_or_account_change_requires_explicit_key_replacement(self):
        self.save({"imessage": KEY})
        for field, value in (("base_url", "https://different.invalid"), ("bot_handle", "other@example.invalid")):
            original = self.settings["components"]["imessage"][field]
            self.settings["components"]["imessage"][field] = value
            with self.assertRaisesRegex(InstanceError, "重新填写密钥"):
                self.save()
            self.settings["components"]["imessage"][field] = original
        self.settings["components"]["imessage"]["base_url"] = "https://different.invalid"
        self.save({"imessage": ""})

    def test_missing_password_routes_or_qq_permission_blocks_activation(self):
        self.save()
        with self.assertRaisesRegex(ComponentError, "密码"):
            runtime_environment(*self.store.snapshot(self.identifier))
        self.settings["group_ids"] = []
        self.save({"imessage": KEY})
        with self.assertRaisesRegex(ComponentError, "已启用"):
            runtime_environment(*self.store.snapshot(self.identifier))
        self.settings["allow_all_groups"] = True
        self.save()
        runtime_environment(*self.store.snapshot(self.identifier))
        self.settings["components"]["imessage"]["routes"] = []
        self.save()
        with self.assertRaisesRegex(ComponentError, "会话映射"):
            runtime_environment(*self.store.snapshot(self.identifier))

    def test_invalid_and_legacy_configuration(self):
        legacy = deepcopy(DEFAULTS)
        del legacy["imessage"]
        self.assertEqual(validate_components(legacy)["imessage"], DEFAULTS["imessage"])
        for change in ({"chat_guid": "SMS;-;+12345"}, {"chat_guid": "iMessage;bad\nheader"},
                {"qq_group": "wrong"}, {"id": "wrong"}, {"path": "/root"}):
            value = options()
            value["routes"][0].update(change)
            with self.assertRaises(InstanceError):
                validate_components({"imessage": value})
        duplicate = options()
        duplicate["routes"].append({**duplicate["routes"][0], "id": "b" * 32, "qq_group": "34567890"})
        with self.assertRaises(InstanceError):
            validate_components({"imessage": duplicate})
        with self.assertRaises(ComponentError):
            component_keys({"imessage": "bad\npassword"})

    def test_private_backup_keeps_matching_password(self):
        self.save({"imessage": KEY})
        directory = self.store.directory(self.identifier)
        (directory / "config/admin.secret").write_bytes(b"x" * 32)
        dump = directory / "dump"
        dump.write_bytes(b"PGDMPfixture")
        backups = BackupStore(directory)
        record = backups.publish("b" * 32, *self.store.snapshot(self.identifier), dump=dump, checksum="a" * 64, database_bytes=0)
        archive = verify_archive(backups.path(record["id"]) / "snapshot.tar", expected_checksum="a" * 64, max_bytes=record["bytes"])
        self.assertEqual(archive["snapshot"]["private"]["component_key_sets"]["2"]["imessage"], KEY)
        self.assertNotIn(KEY, json.dumps(record, ensure_ascii=False))

    def test_manager_keeps_saved_password_bound_to_endpoint_and_account(self):
        self.save({"imessage": KEY})
        controller = object.__new__(Controller)
        controller.store, controller.identifier = self.store, self.identifier
        controller.runtime_probe = Mock(return_value={"chats": 1})
        self.assertEqual(controller.imessage({"options": options(), "key": None}), {"chats": 1})
        controller.runtime_probe.assert_called_once_with("probe-imessage", {"options": options(), "key": KEY})
        for field in ("base_url", "bot_handle"):
            with self.assertRaisesRegex(SetupError, "重新填写密钥"):
                controller.imessage({"options": {**options(), field: "changed"}, "key": None})
        self.assertEqual(controller.runtime_probe.call_count, 1)


class IMessageProbeTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.mode = [], "ready"
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                target = urlsplit(self.path)
                fixture.calls.append((unquote(target.path), parse_qs(target.query)))
                status, data = 200, {"status": 200, "data": {"guid": CHAT}}
                if target.path.endswith("/message"):
                    data.update(data=[], metadata={"total": 0})
                if fixture.mode == "wrong_chat" and not target.path.endswith("/message"):
                    data["data"]["guid"] = "another-chat"
                elif fixture.mode == "bad_pagination" and target.path.endswith("/message"):
                    data.pop("metadata")
                elif fixture.mode in ("bad_key", "redirect"):
                    status, data = (401 if fixture.mode == "bad_key" else 302), {"error": KEY}
                content = json.dumps(data).encode()
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "https://must-not-contact.invalid")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join()))
        self.options = {**options(), "base_url": f"http://127.0.0.1:{self.server.server_port}/gateway"}

    def test_probe_reads_only_explicit_chat_and_preserves_encoded_password(self):
        result = probe_imessage(self.options, KEY)
        self.assertEqual(result["chats"], 1)
        self.assertFalse(result["send_verified"])
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(path.startswith("/gateway/api/v1/chat/" + CHAT) and query["password"] == [KEY] for path, query in self.calls))
        query = self.calls[-1][1]
        self.assertEqual(query["after"], query["before"])
        self.assertEqual(query["limit"], ["1"])
        self.assertNotIn(KEY, json.dumps(result, ensure_ascii=False))

    def test_probe_errors_do_not_expose_credentials_or_claim_readiness(self):
        for mode in ("wrong_chat", "bad_pagination", "bad_key", "redirect"):
            self.mode = mode
            with self.subTest(mode=mode), self.assertRaises(SetupError) as error:
                probe_imessage(self.options, KEY)
            self.assertNotIn(KEY, str(error.exception))

    def test_probe_entrypoint_does_not_load_real_runtime_secrets(self):
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps({"options": self.options, "key": KEY}))), \
                patch("sys.stdout", output), patch("src.local_runtime.entrypoint.load_environment") as load:
            self.assertEqual(main(["probe-imessage"]), 0)
            load.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["data"]["chats"], 1)

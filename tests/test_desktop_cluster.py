from __future__ import annotations

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.cluster_control.capabilities import capability_manifest
from src.local_runtime.backups import BackupStore, verify_archive
from src.local_runtime.cluster_credentials import cluster_credentials
from src.local_runtime.components import ComponentError, DEFAULTS, component_keys, validate_components
from src.local_runtime.entrypoint import load_environment, main
from src.local_runtime.files import write_json
from src.local_runtime.instances import InstanceError, InstanceStore
from src.local_runtime.runtime import runtime_environment
from src.local_runtime.setup_clients import SetupError, probe_cluster


KEY = "synthetic-cluster-key-not-a-real-token-01234567"


def manifest():
    return {"backend": "ops", "catalog_version": 1, "checked_at": 1,
        "capabilities": capability_manifest({}), "error": None}


class ClusterSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = InstanceStore(Path(temporary.name))
        config = self.store.create("cluster-test")
        self.identifier = config["id"]
        self.settings = config["settings"]
        self.settings.update(bot_qq="12345678", admin_qq="87654321", default_model="test",
            models=[{"id": "test", "model": "fixture", "base_url": "https://model.invalid/v1", "api_key_required": False}])
        self.settings["features"]["cluster"] = True
        self.settings["components"] = deepcopy(DEFAULTS)
        self.settings["components"]["cluster"]["base_url"] = "https://cluster.invalid"

    def save(self, keys=None):
        return self.store.update(self.identifier, self.settings,
            expected_revision=self.store.read(self.identifier)["revision"], service_keys=keys or {})

    def test_group_scopes_and_private_runtime_credential(self):
        self.save({"cluster": KEY})
        self.assertNotIn(KEY, json.dumps(self.store.public_status(self.identifier)))
        env = runtime_environment(*self.store.snapshot(self.identifier))
        self.assertEqual(env["AI_CLUSTER_ENABLED"], "true")
        self.assertEqual(env["AI_CLUSTER_CONTROL_URL"], "https://cluster.invalid")
        self.assertEqual(env["AI_FLEET_ALLOWED_GROUPS"], "")
        self.assertEqual(env["AI_FLEET_LOG_ALLOWED_GROUPS"], "")
        self.assertEqual(env["AI_DESKTOP_CLUSTER_TOKEN"], KEY)
        self.assertTrue(env["ONEBOT_ACCESS_TOKEN"])
        self.assertTrue(env["ONEBOT_SECRET"])
        self.assertEqual(env["AI_ADMIN_SECRET_FILE"], "/run/gaoji/admin.secret")
        self.settings["components"]["cluster"]["allowed_groups"] = ["12345678"]
        self.save()
        with self.assertRaisesRegex(ComponentError, "已启用"):
            runtime_environment(*self.store.snapshot(self.identifier))
        self.settings["group_ids"] = ["12345678"]
        self.settings["components"]["cluster"]["log_allowed_groups"] = ["12345678"]
        self.save()
        env = runtime_environment(*self.store.snapshot(self.identifier))
        self.assertEqual(env["AI_FLEET_ALLOWED_GROUPS"], "12345678")
        self.assertEqual(env["AI_FLEET_LOG_ALLOWED_GROUPS"], "12345678")
        inactive = runtime_environment(*self.store.snapshot(self.identifier), configured=False)
        self.assertEqual(inactive["AI_CLUSTER_ENABLED"], "false")
        self.assertNotIn(KEY, json.dumps(inactive))
        self.settings["features"]["cluster"] = False
        self.save()
        disabled = runtime_environment(*self.store.snapshot(self.identifier))
        self.assertNotIn(KEY, json.dumps(disabled))
        self.assertEqual(disabled["AI_CLUSTER_CONTROL_URL"], "")

    def test_endpoint_change_requires_explicit_key_and_old_settings_default(self):
        self.save({"cluster": KEY})
        self.settings["components"]["cluster"]["base_url"] = "https://different.invalid"
        with self.assertRaisesRegex(InstanceError, "重新填写密钥"):
            self.save()
        self.save({"cluster": ""})
        with self.assertRaisesRegex(ComponentError, "Token"):
            runtime_environment(*self.store.snapshot(self.identifier))
        legacy = deepcopy(DEFAULTS)
        del legacy["cluster"]
        self.assertEqual(validate_components(legacy)["cluster"], DEFAULTS["cluster"])

    def test_invalid_configuration(self):
        for change in ({"timeout_seconds": True}, {"timeout_seconds": 31}, {"command": "sh"},
            {"base_url": "https://user:secret@cluster.invalid"}, {"base_url": "https://cluster.invalid?key=value"},
            {"allowed_groups": ["123;sh"]}, {"allowed_groups": [123456]}, {"log_allowed_groups": ["12345678"]}):
            with self.subTest(change=change), self.assertRaises(InstanceError):
                validate_components({"cluster": {**DEFAULTS["cluster"], **change}})
        for key in ("short", "a" * 513, "a" * 32 + "\r\n", "中" * 32, "a" * 32 + " x"):
            with self.subTest(key=key), self.assertRaises(ComponentError):
                component_keys({"cluster": key})

    def test_credential_backup_roundtrip_without_new_required_files(self):
        self.save({"cluster": KEY})
        directory = self.store.directory(self.identifier)
        (directory / "config/admin.secret").write_bytes(b"x" * 32)
        dump = directory / "dump"
        dump.write_bytes(b"PGDMPfixture")
        backups = BackupStore(directory)
        record = backups.publish("b" * 32, *self.store.snapshot(self.identifier), dump=dump, checksum="a" * 64, database_bytes=0)
        result = verify_archive(backups.path(record["id"]) / "snapshot.tar", expected_checksum="a" * 64, max_bytes=record["bytes"])
        self.assertEqual(result["snapshot"]["private"]["component_key_sets"]["2"]["cluster"], KEY)
        self.assertNotIn(KEY, json.dumps(record))

    def test_runtime_token_is_private_ephemeral_and_cleaned_on_exception(self):
        self.save({"cluster": KEY})
        filename = self.store.directory(self.identifier) / "config/runtime.json"
        write_json(filename, {"schema": 1, "environment": runtime_environment(*self.store.snapshot(self.identifier))})
        with patch.dict(os.environ, {"AI_CLUSTER_CONTROL_TOKEN_FILE": "/dev/old-secret"}, clear=True):
            token = load_environment(filename)
            self.assertNotIn(KEY, json.dumps(dict(os.environ)))
            self.assertNotIn("AI_CLUSTER_CONTROL_TOKEN_FILE", os.environ)
            with self.assertRaisesRegex(RuntimeError, "test exit"):
                with cluster_credentials(token):
                    path = Path(os.environ["AI_CLUSTER_CONTROL_TOKEN_FILE"])
                    self.assertEqual(path.read_text(), KEY)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                    self.assertNotIn(KEY, json.dumps(dict(os.environ)))
                    raise RuntimeError("test exit")
            self.assertFalse(path.exists())
            self.assertNotIn("AI_CLUSTER_CONTROL_TOKEN_FILE", os.environ)
        with patch.dict(os.environ, {"AI_CLUSTER_ENABLED": "false"}, clear=True), patch("tempfile.TemporaryDirectory") as create:
            with cluster_credentials(KEY):
                create.assert_not_called()
        with patch.dict(os.environ, {"AI_CLUSTER_ENABLED": "true"}, clear=True), self.assertRaises(ComponentError):
            with cluster_credentials(""):
                self.fail("must not run without key")


class ClusterProbeTests(unittest.TestCase):
    def test_local_authenticated_capability_call_and_readiness(self):
        responses = [manifest(), {**manifest(), "error": {"message": KEY}}]
        responses[0]["capabilities"][0]["available"] = True
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                calls.append((self.path, self.headers.get("Authorization")))
                body = json.dumps(responses.pop(0)).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            options = {**DEFAULTS["cluster"], "base_url": f"http://127.0.0.1:{server.server_port}/gateway"}
            with patch.dict(os.environ, {"HTTP_PROXY": "http://invalid.invalid:1"}):
                result = probe_cluster(options, KEY)
            self.assertTrue(result["upstream_ready"])
            self.assertEqual(result["available"], 1)
            self.assertEqual(calls, [("/gateway/v1/capabilities", "Bearer " + KEY)])
            degraded = probe_cluster(options, KEY)
            self.assertTrue(degraded["responded"])
            self.assertFalse(degraded["upstream_ready"])
            self.assertNotIn(KEY, json.dumps(degraded))
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_health_page_and_invalid_manifests_are_not_connection_success(self):
        for result in ({"status": "ok"}, {**manifest(), "capabilities": "invalid"},
            {**manifest(), "capabilities": [{"available": True}]}, {**manifest(), "catalog_version": True}):
            with patch("src.local_runtime.setup_clients.request_json", return_value=result), self.assertRaises(SetupError):
                probe_cluster({**DEFAULTS["cluster"], "base_url": "https://fixture.invalid"}, KEY)

    def test_entrypoint_probe_uses_stdin_and_does_not_load_runtime(self):
        payload = {"options": {**DEFAULTS["cluster"], "base_url": "https://fixture.invalid"}, "key": KEY}
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", output), \
            patch("src.local_runtime.entrypoint.load_environment") as load, \
            patch("src.local_runtime.setup_clients.probe_cluster", return_value={"responded": True}) as probe:
            self.assertEqual(main(["probe-cluster"]), 0)
            probe.assert_called_once_with(payload["options"], KEY)
            load.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {"data": {"responded": True}})
        self.assertNotIn(KEY, output.getvalue())


if __name__ == "__main__":
    unittest.main()

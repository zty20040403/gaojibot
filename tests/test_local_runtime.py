from __future__ import annotations

from copy import deepcopy
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.local_runtime.files import read_json, write_json
from src.local_runtime.instances import ConfigurationConflict, InstanceError, InstanceStore, validate_settings
from src.local_runtime.entrypoint import load_environment
from src.local_runtime.runtime_check import ROOT_FILES, verify_sources
from src.local_runtime.runtime import compose_document, runtime_environment


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = InstanceStore(Path(self.temporary.name))
        self.config = self.store.create("test")
        self.identifier = self.config["id"]

    def settings(self):
        settings = deepcopy(self.config["settings"])
        settings.update(bot_qq="12345678", admin_qq="23456789", default_model="mine")
        settings["models"] = [{"id": "mine", "model": "test-model", "base_url": "https://example.invalid/v1"}]
        return settings

    def test_instance_secrets_and_ports_are_independent(self):
        other = self.store.create("another")
        self.assertFalse(set(self.config["ports"].values()) & set(other["ports"].values()))
        self.assertNotEqual(self.store.secrets(self.identifier)["database_password"], self.store.secrets(other["id"])["database_password"])
        self.assertFalse(self.config["settings"]["allow_all_groups"])

    def test_optimistic_revision_and_redacted_public_status(self):
        updated = self.store.update(self.identifier, self.settings(), expected_revision=1, model_keys={"mine": "test-only-key"})
        self.assertEqual(updated["revision"], 2)
        status = self.store.public_status(self.identifier)
        self.assertTrue(status["settings"]["models"][0]["has_key"])
        self.assertNotIn("test-only-key", json.dumps(status))
        with self.assertRaises(ConfigurationConflict):
            self.store.update(self.identifier, self.settings(), expected_revision=1)

    def test_interrupted_config_save_retains_matching_preceding_credentials(self):
        original_write = write_json
        def fail_config(path, value):
            if path.name == "instance.json":
                raise OSError("simulated crash")
            original_write(path, value)
        with patch("src.local_runtime.instances.write_json", side_effect=fail_config):
            with self.assertRaises(OSError):
                self.store.update(self.identifier, self.settings(), expected_revision=1, model_keys={"mine": "new-key"})
        config, private = self.store.snapshot(self.identifier)
        self.assertEqual(config["revision"], 1)
        self.assertEqual(private["model_keys"], {})
        self.store.update(self.identifier, self.settings(), expected_revision=1, model_keys={"mine": "retry-key"})
        self.assertEqual(self.store.snapshot(self.identifier)[1]["model_keys"], {"mine": "retry-key"})

    def test_rejects_traversal_unknown_fields_and_fallback_cycles(self):
        with self.assertRaises(InstanceError):
            self.store.directory("../../other")
        settings = self.settings()
        settings["models"][0]["api_key"] = "forbidden"
        with self.assertRaises(InstanceError):
            validate_settings(settings)
        settings = self.settings()
        settings["models"].append({"id": "other", "model": "other", "base_url": "http://localhost:8000/v1", "fallbacks": ["mine"]})
        settings["models"][0]["fallbacks"] = ["other"]
        with self.assertRaisesRegex(InstanceError, "cycle"):
            validate_settings(settings)

    def test_endpoint_changes_never_inherit_an_existing_key(self):
        for field, value in (("base_url", "https://other.invalid/v1"), ("protocol", "anthropic-messages")):
            with self.subTest(field=field):
                config = self.store.create("key isolation")
                updated = self.store.update(config["id"], self.settings(), expected_revision=1, model_keys={"mine": "original-key"})
                settings = deepcopy(updated["settings"])
                settings["models"][0][field] = value
                with self.assertRaisesRegex(InstanceError, "重新填写密钥"):
                    self.store.update(config["id"], settings, expected_revision=2)
                unchanged, private = self.store.snapshot(config["id"])
                self.assertEqual(unchanged["revision"], 2)
                self.assertEqual(private["model_keys"], {"mine": "original-key"})
                self.store.update(config["id"], settings, expected_revision=2, model_keys={"mine": "replacement-key"})
                self.assertEqual(self.store.snapshot(config["id"])[1]["model_keys"], {"mine": "replacement-key"})

    def test_endpoint_change_can_explicitly_clear_key_for_a_keyless_service(self):
        config = self.store.update(self.identifier, self.settings(), expected_revision=1, model_keys={"mine": "original-key"})
        config["settings"]["models"][0].update(base_url="http://localhost:8000/v1", api_key_required=False)
        self.store.update(self.identifier, config["settings"], expected_revision=2, model_keys={"mine": ""})
        self.assertFalse(self.store.public_status(self.identifier)["settings"]["models"][0]["has_key"])
        env = runtime_environment(*self.store.snapshot(self.identifier))
        self.assertNotIn("original-key", json.dumps(env))

    def test_environment_has_no_publisher_profiles_and_enforces_selected_groups(self):
        self.store.update(self.identifier, self.settings(), expected_revision=1, model_keys={"mine": "own-key"})
        config, private = self.store.snapshot(self.identifier)
        env = runtime_environment(config, private)
        self.assertEqual(env["AI_GROUPS_REQUIRE_ALLOWLIST"], "true")
        self.assertEqual(env["AI_ENABLED_GROUPS"], "")
        self.assertEqual(env["AI_VISION_PROFILE"], "mine")
        self.assertEqual(env["AI_LOCAL_MODEL_PROFILE"], "")
        for key in ("AI_RECALL_ROUTER_PROFILE", "AI_PROACTIVE_CLASSIFIER_PROFILE", "AI_HISTORIAN_PROFILE", "AI_DREAM_PROFILE"):
            self.assertEqual(env[key], "mine")
        self.assertEqual(env["GAOJI_MODEL_KEY_0"], "own-key")
        self.assertNotIn("own-key", env["AI_MODEL_PROFILES_JSON"])
        self.assertEqual(set(json.loads(env["AI_MODEL_PROFILES_JSON"])["profiles"]), {"mine"})
        for value in ("h610", "tank", "imdomestic", "gpt-5.6", "3526452465"):
            self.assertNotIn(value, json.dumps(env))

    def test_start_requires_real_qq_admin_and_model_configuration(self):
        with self.assertRaises(InstanceError):
            runtime_environment(*self.store.snapshot(self.identifier))
        self.store.update(self.identifier, self.settings(), expected_revision=1)
        with self.assertRaisesRegex(InstanceError, "API key"):
            runtime_environment(*self.store.snapshot(self.identifier))

    def test_compose_has_no_public_database_no_host_socket_and_bounded_logs(self):
        image = "example.invalid/runtime@sha256:" + "a" * 64
        document = compose_document(self.config, self.store.directory(self.identifier), runtime_image="gaoji-app-runtime:" + "a" * 64,
            images={"postgres": image, "napcat": image})
        services = document["services"]
        self.assertNotIn("ports", services["database"])
        for name in ("napcat", "bot"):
            self.assertTrue(all(port.startswith("127.0.0.1:") for port in services[name]["ports"]))
        self.assertEqual(services["bot"]["pull_policy"], "never")
        self.assertEqual(services["bot"]["profiles"], ["configured"])
        assets = next(item for item in services["bot"]["volumes"] if item["target"] == "/opt/gaoji-models")
        self.assertTrue(assets["read_only"])
        self.assertTrue(assets["source"].endswith("/assets"))
        capacity = next(item for item in services["bot"]["volumes"] if item["target"] == "/run/gaoji-capacity")
        self.assertTrue(capacity["read_only"])
        self.assertEqual(capacity["source"], str(self.store.directory(self.identifier) / "capacity"))
        self.assertFalse(capacity["bind"]["create_host_path"])
        environment = runtime_environment(*self.store.snapshot(self.identifier), configured=False)
        self.assertEqual(environment["AI_APP_CAPACITY_FILE"], "/run/gaoji-capacity/status.json")
        self.assertEqual(environment["AI_APP_INSTANCE_ID"], self.identifier)
        self.assertNotIn("docker.sock", json.dumps(document))
        self.assertNotIn("database_password", json.dumps(document))
        self.assertEqual(services["database"]["logging"]["options"]["max-file"], "3")

    def test_entrypoint_discards_inherited_provider_credentials(self):
        filename = Path(self.temporary.name) / "runtime.json"
        write_json(filename, {"schema": 1, "environment": {"AI_ADMIN_ENABLED": "true", "GAOJI_MODEL_KEY_0": "new-key"}})
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "developer-key", "AI_POSTGRES_DSN": "developer-db", "PATH": "/bin"}, clear=True):
            load_environment(filename)
            self.assertNotIn("DEEPSEEK_API_KEY", os.environ)
            self.assertNotIn("AI_POSTGRES_DSN", os.environ)
            self.assertEqual(os.environ["GAOJI_MODEL_KEY_0"], "new-key")
            self.assertEqual(os.environ["PATH"], "/bin")
        write_json(filename, {"schema": 1, "environment": {"LD_PRELOAD": "/tmp/invalid.so"}})
        with self.assertRaises(ValueError):
            load_environment(filename)

    def test_sandbox_uses_only_instance_rpc_not_host_docker_and_restore_has_no_rpc(self):
        self.store.update(self.identifier, self.settings(), expected_revision=1, model_keys={"mine": "own-key"})
        config, private = self.store.snapshot(self.identifier)
        config["settings"]["features"]["sandbox"] = True
        with self.assertRaises(InstanceError):
            runtime_environment(config, private)
        inactive = runtime_environment(config, private, configured=False)
        self.assertEqual(inactive["AI_SANDBOX_ENABLED"], "false")
        self.assertNotIn("AI_SANDBOX_RUNNER_TOKEN", inactive)
        connection = {"socket": "/run/gaoji-sandbox/runner.sock", "token": "t" * 43}
        enabled = runtime_environment(config, private, sandbox=connection)
        self.assertEqual(enabled["AI_SANDBOX_RUNNER_TOKEN"], connection["token"])
        self.assertEqual(enabled["AI_SANDBOX_ENABLED"], "true")
        self.assertEqual(enabled["AI_SANDBOX_MAX_FILE_MB"], "20")
        for invalid in ({**connection, "socket": "/var/run/docker.sock"}, {**connection, "token": "short"}):
            with self.assertRaises(InstanceError):
                runtime_environment(config, private, sandbox=invalid)
        image = "example.invalid/runtime@sha256:" + "a" * 64
        for enabled in (True, False):
            document = compose_document(config, self.store.directory(self.identifier),
                runtime_image="gaoji-app-runtime:" + "a" * 64,
                images={"postgres": image, "napcat": image}, sandbox_enabled=enabled)
            sockets = [item for item in document["services"]["bot"]["volumes"] if item["target"] == "/run/gaoji-sandbox"]
            self.assertEqual(len(sockets), int(enabled))
            if enabled:
                self.assertTrue(sockets[0]["read_only"])
                self.assertFalse(sockets[0]["bind"]["create_host_path"])
            for name in ("database", "napcat"):
                self.assertFalse(any(item["target"] == "/run/gaoji-sandbox" for item in document["services"][name]["volumes"]))
            self.assertNotIn("docker.sock", json.dumps(document))

    def test_distribution_group_allowlist_preserves_legacy_default(self):
        # Load this pure configuration module without importing the NoneBot plugin.
        filename = Path(__file__).parents[1] / "src/plugins/ai_chat/config.py"
        import sys
        spec = importlib.util.spec_from_file_location("gaoji_test_settings", filename)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {spec.name: module}):
            spec.loader.exec_module(module)
            with patch.dict(os.environ, {}, clear=True):
                self.assertTrue(module.Settings.from_env().is_group_enabled(12345678))
            with patch.dict(os.environ, {"AI_GROUPS_REQUIRE_ALLOWLIST": "true"}, clear=True):
                self.assertFalse(module.Settings.from_env().is_group_enabled(12345678))
            with patch.dict(os.environ, {"AI_GROUPS_REQUIRE_ALLOWLIST": "true", "AI_ENABLED_GROUPS": "12345678"}, clear=True):
                self.assertTrue(module.Settings.from_env().is_group_enabled(12345678))
                self.assertFalse(module.Settings.from_env().is_group_enabled(23456789))


class RuntimeImageSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.names = ROOT_FILES | {"src/__init__.py", "src/local_runtime/entrypoint.py", "migrations/env.py",
            "src/plugins/ai_chat/admin_ui_dist/app.js", "src/plugins/ai_chat/admin_ui_dist/app.css"}
        self.entries = []
        for name in sorted(self.names):
            filename = self.root / name
            filename.parent.mkdir(parents=True, exist_ok=True)
            contents = b"# fixture\n"
            filename.write_bytes(contents)
            self.entries.append({"path": name, "bytes": len(contents), "sha256": hashlib.sha256(contents).hexdigest()})
        self.manifest()

    def manifest(self):
        (self.root / "manifest.json").write_text(json.dumps({"schema": 1, "files": self.entries}))

    def test_all_packaged_runtime_assets_are_read_and_verified(self):
        self.assertEqual(verify_sources(self.root), len(self.names))

    def test_missing_changed_unreadable_and_outside_sources_rejected(self):
        filename = self.root / "src/__init__.py"
        filename.unlink()
        with self.assertRaises(FileNotFoundError):
            verify_sources(self.root)
        filename.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "verification failed"):
            verify_sources(self.root)
        filename.write_bytes(b"# fixture\n")
        original = Path.read_bytes
        def unreadable(path):
            if path == filename:
                raise PermissionError("simulated non-root read failure")
            return original(path)
        with patch.object(Path, "read_bytes", unreadable), self.assertRaises(PermissionError):
            verify_sources(self.root)
        self.entries.append({"path": "../private", "bytes": 0, "sha256": "0" * 64})
        self.manifest()
        with self.assertRaisesRegex(ValueError, "manifest path"):
            verify_sources(self.root)

    def test_missing_console_manifest_entry_rejected(self):
        self.entries = [entry for entry in self.entries if not entry["path"].endswith("app.js")]
        self.manifest()
        with self.assertRaisesRegex(ValueError, "console assets are missing"):
            verify_sources(self.root)


if __name__ == "__main__":
    unittest.main()

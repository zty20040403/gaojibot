from __future__ import annotations

from copy import deepcopy
import http.client
import json
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from src.local_runtime.files import write_json
from src.local_runtime.instances import InstanceStore
from src.local_runtime.manager import Controller, ManagerServer
from src.local_runtime.setup_clients import NapCatSetup, SetupError, probe_model
from src.local_runtime.video_assets import VideoAssetError
from src.local_runtime.sandbox_image import SandboxImageError


PROFILE = {"id": "primary", "model": "test-model", "base_url": "https://model.example/v1", "provider": "custom"}


class ManagerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = InstanceStore(self.root / "data")
        config = self.store.create("Test")
        settings = config["settings"]
        settings.update(bot_qq="12345678", admin_qq="87654321", default_model="primary", models=[PROFILE])
        self.config = self.store.update(config["id"], settings, expected_revision=config["revision"], model_keys={"primary": "test-private-key"})
        self.directory = self.store.directory(config["id"])
        job = "1" * 32
        (self.root / "jobs").mkdir()
        write_json(self.root / "jobs" / (job + ".json"), {"instance_id": config["id"], "checksum": "2" * 64,
            "runtime_image": "gaoji-app-runtime:" + "3" * 64, "docker": "/test/docker", "docker_host": "unix:///test/engine"})
        self.token = secrets.token_urlsafe(32)
        write_json(self.directory / "config/control.json", {"port": 0, "token": self.token})
        self.controller = Controller(job, self.root)
        engine = self.root / "fixture-engine"
        engine.mkdir()
        self.controller.capacity.engine_inventory = Mock(return_value=(engine, [], 0))
        self.server = ManagerServer(self.controller)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def close():
            self.server.shutdown(); self.server.server_close(); thread.join(timeout=3)
        self.addCleanup(close)

    def post(self, route, body, *, authenticated=True, extra=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        headers = {"Content-Type": "application/json", **(extra or {})}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        connection.request("POST", route, json.dumps(body), headers)
        response = connection.getresponse()
        value = json.loads(response.read())
        connection.close()
        return response.status, value

    def test_auth_scope_and_public_secret_redaction(self):
        self.assertEqual(self.post("/status", {}, authenticated=False)[0], 403)
        self.assertEqual(self.post("/status", {}, extra={"Origin": "http://evil.example"})[0], 403)
        code, public = self.post("/status", {})
        self.assertEqual(code, 200)
        text = json.dumps(public)
        self.assertNotIn("test-private-key", text)
        self.assertNotIn(self.token, text)
        self.assertTrue(public["data"]["instance"]["settings"]["models"][0]["has_key"])
        for path in ("/exec", "/delete", "/../status", "/status?instance=other"):
            self.assertEqual(self.post(path, {})[0], 404)
        self.assertEqual(self.post("/status", {"instance": "other"})[0], 400)

    def test_save_preserves_keys_and_rejects_stale_revision(self):
        settings = deepcopy(self.config["settings"]); settings["name"] = "Renamed"
        body = {"revision": self.config["revision"], "settings": settings, "model_keys": {}}
        self.assertEqual(self.post("/settings", body)[0], 200)
        self.assertEqual(self.post("/settings", body)[0], 409)
        _, private = self.store.snapshot(self.config["id"])
        self.assertEqual(private["model_keys"]["primary"], "test-private-key")
        settings["host_path"] = "/root"
        self.assertEqual(self.post("/settings", {**body, "revision": self.config["revision"] + 1})[0], 400)

    def test_services_include_read_only_connection_status_without_private_identity(self):
        write_json(self.directory / "state/desktop-health.json", {"qq": "12345678", "connected": True, "updated_at": 99})
        self.controller.compose = Mock(return_value=json.dumps([
            {"Service": "bot", "State": "running", "Health": ""},
            {"Service": "other-instance", "State": "running", "Health": "private"},
        ]))
        with patch("src.local_runtime.readiness.time.time", return_value=100):
            code, result = self.post("/services", {})
        self.assertEqual(code, 200)
        self.assertEqual(result["data"]["connection"], "connected")
        self.assertEqual(len(result["data"]["services"]), 1)
        self.assertNotIn("12345678", json.dumps(result))
        self.assertNotIn("private", json.dumps(result))
        self.controller.compose.assert_called_once_with(["ps", "--all", "--format", "json"], timeout=15)

    def test_capacity_settings_use_config_revision_and_storage_status_is_cached(self):
        settings = deepcopy(self.config["settings"])
        settings["components"]["capacity"] = {"max_data_gib": 80, "reserve_free_gib": 3}
        body = {"revision": self.config["revision"], "settings": settings, "model_keys": {}}
        code, saved = self.post("/settings", body)
        self.assertEqual(code, 200, saved)
        self.assertEqual(saved["data"]["capacity_configuration"], {"schema": 1})
        self.assertEqual(self.post("/settings", body)[0], 409)
        self.controller.capacity.scan()
        self.controller.capacity.engine_inventory.reset_mock()
        for _ in range(3):
            code, result = self.post("/storage/status", {})
            self.assertEqual(code, 200, result)
            self.assertEqual(result["data"]["capacity"]["policy"], settings["components"]["capacity"])
        self.controller.capacity.engine_inventory.assert_not_called()
        settings["components"]["capacity"]["max_data_gib"] = 0
        self.assertEqual(self.post("/settings", {**body, "revision": self.config["revision"] + 1})[0], 400)

    def test_cannot_send_stored_key_to_changed_endpoint(self):
        model = deepcopy(self.config["settings"]["models"][0]); model["base_url"] = "https://other.example/v1"
        with patch.object(self.controller, "runtime_probe") as probe:
            code, result = self.post("/model/test", {"model": model, "key": None})
        self.assertEqual(code, 400)
        self.assertIn("重新填写密钥", result["error"])
        probe.assert_not_called()

    def test_operation_is_persisted_before_running_and_not_duplicated(self):
        body = {"id": "a" * 32, "kind": "activate", "revision": self.config["revision"],
            "administrator": {"username": "admin", "password": "private-password-for-setup"}}
        with patch.object(self.controller, "resume"):
            code, result = self.post("/operation", body)
            self.assertEqual(code, 200)
            self.assertNotIn("private-password", json.dumps(result))
            self.assertEqual(self.post("/operation", body)[1], result)
            self.assertEqual(self.post("/operation", {**body, "id": "b" * 32})[0], 400)
            settings = deepcopy(self.config["settings"])
            self.assertEqual(self.post("/settings", {"revision": self.config["revision"], "settings": settings, "model_keys": {}})[0], 400)
        restored = Controller("1" * 32, self.root)
        self.assertEqual(restored.operation["id"], body["id"])
        self.assertEqual(restored.operation["status"], "queued")

    def test_embedding_configuration_and_saved_key_are_private_and_scoped(self):
        settings = deepcopy(self.config["settings"])
        settings["components"]["embedding"]["base_url"] = "https://embedding.invalid/v1"
        body = {"revision": self.config["revision"], "settings": settings, "model_keys": {}, "component_keys": {"embedding": "private-vector-key"}}
        code, result = self.post("/settings", body)
        self.assertEqual(code, 200)
        self.assertNotIn("private-vector-key", json.dumps(result))
        self.assertEqual(result["data"]["instance"]["component_keys_present"], ["embedding"])
        options = settings["components"]["embedding"]
        with patch.object(self.controller, "runtime_probe", return_value={"responded": True}) as probe:
            self.assertEqual(self.post("/embedding/test", {"options": options, "key": None})[0], 200)
            probe.assert_called_once_with("probe-embedding", {"options": options, "key": "private-vector-key"})
        with patch.object(self.controller, "runtime_probe") as probe:
            code, result = self.post("/embedding/test", {"options": {**options, "base_url": "https://changed.invalid/v1"}, "key": None})
            self.assertEqual(code, 400)
            self.assertIn("重新填写密钥", result["error"])
            probe.assert_not_called()
        self.assertEqual(self.post("/embedding/test", {"options": options, "key": None}, authenticated=False)[0], 403)

    def test_activation_reports_missing_dependencies_without_starting_components(self):
        settings = deepcopy(self.config["settings"])
        settings["features"]["semantic"] = True
        config = self.store.update(self.config["id"], settings, expected_revision=self.config["revision"])
        with patch.object(self.controller, "resume") as resume:
            code, result = self.post("/operation", {"id": "a" * 32, "kind": "activate", "revision": config["revision"]})
            self.assertEqual(code, 400)
            self.assertIn("Embedding 服务地址", result["error"])
            resume.assert_not_called()
        self.assertFalse(self.controller.operation_path.exists())

    def test_cluster_api_preserves_endpoint_binding_and_uses_fixed_container_probe(self):
        settings = deepcopy(self.config["settings"])
        settings["components"]["cluster"]["base_url"] = "https://cluster.invalid"
        key = "synthetic-cluster-key-not-a-real-token"
        code, result = self.post("/settings", {"revision": self.config["revision"], "settings": settings,
            "model_keys": {}, "component_keys": {"cluster": key}})
        self.assertEqual(code, 200)
        self.assertEqual(result["data"]["cluster_configuration"], {"schema": 1})
        self.assertNotIn(key, json.dumps(result))
        options = settings["components"]["cluster"]
        with patch.object(self.controller, "compose", return_value='{"data":{"responded":true}}') as command:
            self.assertEqual(self.post("/cluster/test", {"options": options, "key": None})[0], 200)
            args, kwargs = command.call_args
            self.assertEqual(args[0], ["run", "--rm", "--no-deps", "-T", "bot", "probe-cluster"])
            self.assertEqual(kwargs["stdin"], {"options": options, "key": key})
            self.assertNotIn(key, json.dumps(args))
        with patch.object(self.controller, "compose") as command:
            self.assertEqual(self.post("/cluster/test", {"options": {**options, "base_url": "https://changed.invalid"}, "key": None})[0], 400)
            self.assertEqual(self.post("/cluster/test", {"options": options, "key": None}, authenticated=False)[0], 403)
            command.assert_not_called()

    def test_incompatible_cluster_does_not_switch_live_bot(self):
        settings = deepcopy(self.config["settings"])
        settings["features"]["cluster"] = True
        settings["components"]["cluster"]["base_url"] = "https://cluster.invalid"
        config = self.store.update(self.config["id"], settings, expected_revision=self.config["revision"],
            service_keys={"cluster": "synthetic-cluster-key-not-a-real-token"})
        with patch.object(self.controller, "resume"):
            self.controller.submit({"id": "f" * 32, "kind": "activate", "revision": config["revision"]})
        def probe(command, _payload):
            if command == "probe-cluster":
                raise SetupError("集群接口格式不兼容")
            return {"responded": True}
        with patch.object(self.controller, "compose") as compose, patch.object(self.controller, "render") as render, \
            patch.object(self.controller, "runtime_probe", side_effect=probe), patch.object(self.controller.napcat, "configure") as connect, \
            patch.object(self.controller.napcat, "status", return_value={"online": True, "qq": "12345678"}):
            self.controller.execute()
        self.assertEqual(self.controller.operation["status"], "failed")
        self.assertEqual(self.controller.operation["stage"], "cluster")
        connect.assert_not_called()
        render.assert_not_called()
        self.assertFalse(any("--force-recreate" in call.args[0] for call in compose.call_args_list))

    def test_matrix_api_checks_account_binding_and_fixed_probe(self):
        settings = deepcopy(self.config["settings"])
        options = {"enabled": True, "base_url": "https://matrix.invalid", "user_id": "@bot:example.test",
            "routes": [{"id": "b" * 32, "qq_group": "12345678", "room_id": "!room:example.test"}]}
        settings["components"]["matrix"] = options
        key = "synthetic-matrix-token-not-real"
        code, result = self.post("/settings", {"revision": self.config["revision"], "settings": settings,
            "model_keys": {}, "component_keys": {"matrix": key}})
        self.assertEqual(code, 200)
        self.assertEqual(result["data"]["matrix_configuration"], {"schema": 1})
        self.assertNotIn(key, json.dumps(result))
        with patch.object(self.controller, "compose", return_value='{"data":{"responded":true,"rooms":1}}') as command:
            self.assertEqual(self.post("/matrix/test", {"options": options, "key": None})[0], 200)
            self.assertEqual(command.call_args.args[0], ["run", "--rm", "--no-deps", "-T", "bot", "probe-matrix"])
            self.assertEqual(command.call_args.kwargs["stdin"], {"options": options, "key": key})
        with patch.object(self.controller, "compose") as command:
            for change in ({"base_url": "https://different.invalid"}, {"user_id": "@other:example.test"}):
                self.assertEqual(self.post("/matrix/test", {"options": {**options, **change}, "key": None})[0], 400)
            self.assertEqual(self.post("/matrix/test", {"options": options, "key": None}, authenticated=False)[0], 403)
            command.assert_not_called()

    def test_matrix_rejection_does_not_reconfigure_running_bot(self):
        settings = deepcopy(self.config["settings"])
        settings["components"]["matrix"] = {"enabled": True, "base_url": "https://matrix.invalid", "user_id": "@bot:example.test",
            "routes": [{"id": "b" * 32, "qq_group": "12345678", "room_id": "!room:example.test"}]}
        settings["group_ids"] = ["12345678"]
        config = self.store.update(self.config["id"], settings, expected_revision=self.config["revision"],
            service_keys={"matrix": "synthetic-matrix-token-not-real"})
        with patch.object(self.controller, "resume"):
            self.controller.submit({"id": "f" * 32, "kind": "activate", "revision": config["revision"]})
        def probe(command, _payload):
            if command == "probe-matrix":
                raise SetupError("Matrix 房间已加密")
            return {"responded": True}
        with patch.object(self.controller, "compose") as compose, patch.object(self.controller, "render") as render, \
            patch.object(self.controller, "runtime_probe", side_effect=probe), patch.object(self.controller.napcat, "configure") as connect, \
            patch.object(self.controller.napcat, "status", return_value={"online": True, "qq": "12345678"}):
            self.controller.execute()
        self.assertEqual(self.controller.operation["status"], "failed")
        self.assertEqual(self.controller.operation["stage"], "matrix")
        connect.assert_not_called()
        render.assert_not_called()
        self.assertFalse(any("--force-recreate" in call.args[0] for call in compose.call_args_list))

    def test_imessage_api_uses_fixed_probe_and_rejection_keeps_running_bot(self):
        settings = deepcopy(self.config["settings"])
        options = {"enabled": True, "base_url": "https://mac.invalid", "bot_handle": "bot@example.test",
            "routes": [{"id": "b" * 32, "qq_group": "12345678", "chat_guid": "iMessage;+;chat-test"}]}
        settings["components"]["imessage"] = options
        settings["group_ids"] = ["12345678"]
        key = "synthetic-bluebubbles-password"
        code, result = self.post("/settings", {"revision": self.config["revision"], "settings": settings,
            "model_keys": {}, "component_keys": {"imessage": key}})
        self.assertEqual(code, 200)
        self.assertEqual(result["data"]["imessage_configuration"], {"schema": 1})
        self.assertNotIn(key, json.dumps(result))
        with patch.object(self.controller, "compose", return_value='{"data":{"responded":true,"chats":1}}') as command:
            self.assertEqual(self.post("/imessage/test", {"options": options, "key": None})[0], 200)
            self.assertEqual(command.call_args.args[0], ["run", "--rm", "--no-deps", "-T", "bot", "probe-imessage"])
            self.assertEqual(command.call_args.kwargs["stdin"], {"options": options, "key": key})
        with patch.object(self.controller, "compose") as command:
            self.assertEqual(self.post("/imessage/test", {"options": options, "key": None}, authenticated=False)[0], 403)
            command.assert_not_called()
        config = self.store.read(self.config["id"])
        with patch.object(self.controller, "resume"):
            self.controller.submit({"id": "f" * 32, "kind": "activate", "revision": config["revision"]})
        def probe(command, _payload):
            if command == "probe-imessage":
                raise SetupError("BlueBubbles 未就绪")
            return {"responded": True}
        with patch.object(self.controller, "compose") as compose, patch.object(self.controller, "render") as render, \
                patch.object(self.controller, "runtime_probe", side_effect=probe), patch.object(self.controller.napcat, "configure") as connect, \
                patch.object(self.controller.napcat, "status", return_value={"online": True, "qq": "12345678"}):
            self.controller.execute()
        self.assertEqual(self.controller.operation["status"], "failed")
        self.assertEqual(self.controller.operation["stage"], "imessage")
        connect.assert_not_called()
        render.assert_not_called()
        self.assertFalse(any("--force-recreate" in call.args[0] for call in compose.call_args_list))

    def test_probes_run_inside_bot_network_and_pass_secrets_only_on_stdin(self):
        payload = {"model": PROFILE, "key": "stdin-only-secret", "listing": False}
        with patch.object(self.controller, "compose", return_value='{"data":{"responded":true}}') as command:
            self.assertEqual(self.controller.runtime_probe("probe-model", payload), {"responded": True})
            args, kwargs = command.call_args
            self.assertEqual(args[0], ["run", "--rm", "--no-deps", "-T", "bot", "probe-model"])
            self.assertNotIn("stdin-only-secret", json.dumps(args))
            self.assertEqual(kwargs["stdin"], payload)
        with patch.object(self.controller, "compose", return_value='{"error":"密钥不正确或已失效"}'), self.assertRaisesRegex(SetupError, "密钥"):
            self.controller.runtime_probe("probe-model", payload)

    def test_restore_requires_confirmation_and_cannot_change_backup_on_retry(self):
        body = {"id": "a" * 32, "kind": "restore", "revision": self.config["revision"], "backup_id": "b" * 32}
        with patch.object(self.controller, "resume"):
            self.assertEqual(self.post("/operation", body)[0], 400)
            code, result = self.post("/operation", {**body, "confirm": True})
            self.assertEqual(code, 200)
            self.assertEqual(result["data"]["backup_id"], "b" * 32)
            self.assertEqual(self.post("/operation", {**body, "confirm": True, "backup_id": "c" * 32})[0], 400)
        self.assertEqual(self.post("/backups", {})[1], {"data": {"backups": [], "transfer": {"schema": 1, "max_bytes": 32 * 1024**3}}})

    def test_interrupted_maintenance_blocks_edits_but_recovery_accepts_stale_revision(self):
        write_json(self.directory / "config/maintenance.json", {"schema": 1, "id": "d" * 32})
        body = {"id": "a" * 32, "kind": "activate", "revision": self.config["revision"]}
        with patch.object(self.controller, "resume"):
            self.assertEqual(self.post("/operation", body)[0], 400)
            self.assertEqual(self.post("/operation", {**body, "kind": "recover", "revision": 1})[0], 200)
        self.assertTrue(self.post("/status", {})[1]["data"]["maintenance_pending"])

    def test_video_dependency_failure_stops_activation_before_qq_reconfiguration(self):
        settings = deepcopy(self.config["settings"])
        settings["models"][0]["vision"] = True
        settings["features"].update(media=True, video=True)
        config = self.store.update(self.config["id"], settings, expected_revision=self.config["revision"])
        with patch.object(self.controller, "resume"):
            self.controller.submit({"id": "f" * 32, "kind": "activate", "revision": config["revision"]})
        with patch.object(self.controller, "compose") as compose, patch.object(self.controller, "render"), \
            patch.object(self.controller, "runtime_probe"), patch.object(self.controller.napcat, "configure") as connect, \
            patch.object(self.controller.napcat, "status", return_value={"online": True, "qq": "12345678"}), \
            patch("src.local_runtime.manager.VideoAssets.ensure", side_effect=VideoAssetError("转写模型校验失败")):
            self.controller.execute()
        self.assertEqual(self.controller.operation["status"], "failed")
        self.assertEqual(self.controller.operation["stage"], "video")
        self.assertIn("校验失败", self.controller.operation["message"])
        connect.assert_not_called()
        self.assertFalse(any("--force-recreate" in call.args[0] for call in compose.call_args_list))

    def test_failed_bot_to_runner_check_pauses_runner_and_does_not_switch_qq(self):
        settings = deepcopy(self.config["settings"])
        settings["features"]["sandbox"] = True
        config = self.store.update(self.config["id"], settings, expected_revision=self.config["revision"])
        with patch.object(self.controller, "resume"), patch.object(self.controller.sandbox, "prepare", return_value={
                "socket": "/run/gaoji-sandbox/runner.sock", "token": "t" * 43}):
            self.controller.submit({"id": "f" * 32, "kind": "activate", "revision": config["revision"]})
        with patch.object(self.controller, "compose", return_value='{"channel_ready":false}') as compose, \
                patch.object(self.controller, "render"), patch.object(self.controller, "runtime_probe"), \
                patch.object(self.controller.sandbox, "start") as start, patch.object(self.controller.sandbox, "pause") as pause, \
                patch.object(self.controller.napcat, "configure") as connect, \
                patch.object(self.controller.napcat, "status", return_value={"online": True, "qq": "12345678"}):
            self.controller.execute()
        self.assertEqual(self.controller.operation["status"], "failed")
        self.assertEqual(self.controller.operation["stage"], "sandbox")
        self.assertIn("尚未接通", self.controller.operation["message"])
        start.assert_called_once()
        pause.assert_called_once()
        connect.assert_not_called()
        self.assertFalse(any("--force-recreate" in call.args[0] for call in compose.call_args_list))

    def test_sandbox_build_failure_blocks_activation_and_keeps_qq_configuration(self):
        settings = deepcopy(self.config["settings"])
        settings["features"]["sandbox"] = True
        config = self.store.update(self.config["id"], settings, expected_revision=self.config["revision"])
        builder = Mock(recipe_id="a" * 64)
        builder.ensure.side_effect = SandboxImageError("构建下载失败")
        with patch.object(self.controller, "sandbox_build", return_value=builder), patch.object(self.controller, "resume"):
            self.controller.submit({"id": "f" * 32, "kind": "activate", "revision": config["revision"]})
            with patch.object(self.controller, "compose"), patch.object(self.controller, "render"), \
                    patch.object(self.controller, "runtime_probe"), patch.object(self.controller.sandbox, "start") as start, \
                    patch.object(self.controller.napcat, "configure") as connect, \
                    patch.object(self.controller.napcat, "status", return_value={"online": True, "qq": "12345678"}):
                self.controller.execute()
        self.assertEqual(self.controller.operation["status"], "failed")
        self.assertEqual(self.controller.operation["stage"], "sandbox_image")
        self.assertIn("下载失败", self.controller.operation["message"])
        start.assert_not_called()
        connect.assert_not_called()
        self.assertFalse(self.controller.sandbox_image_path.exists())

    def test_completed_image_is_persisted_before_runner_start_and_survives_manager_restart(self):
        settings = deepcopy(self.config["settings"])
        settings["features"]["sandbox"] = True
        config = self.store.update(self.config["id"], settings, expected_revision=self.config["revision"])
        record = {"schema": 1, "recipe": "a" * 64, "image": "gaoji-app-sandbox:" + "a" * 64,
                  "image_id": "sha256:" + "b" * 64, "bytes": 1024, "integrity": "nix-store-v1"}
        builder = Mock(recipe_id="a" * 64)
        builder.ensure.return_value = record
        def interrupted():
            self.assertEqual(json.loads(self.controller.sandbox_image_path.read_text()), record)
            raise SetupError("service unavailable")
        with patch.object(self.controller, "sandbox_build", return_value=builder), patch.object(self.controller, "resume"):
            self.controller.submit({"id": "f" * 32, "kind": "activate", "revision": config["revision"]})
            with patch.object(self.controller, "compose"), patch.object(self.controller, "render"), \
                    patch.object(self.controller, "runtime_probe"), patch.object(self.controller.sandbox, "start", side_effect=interrupted), \
                    patch.object(self.controller.napcat, "status", return_value={"online": True, "qq": "12345678"}):
                self.controller.execute()
        restored = Controller("1" * 32, self.root)
        self.assertTrue(restored.sandbox.available)
        self.assertEqual(restored.deployment["sandbox_image_id"], record["image_id"])
        self.assertNotIn("test-private-key", json.dumps(self.controller.public_operation()))

    def test_old_unverified_image_record_keeps_controller_reachable_but_sandbox_disabled(self):
        record = {"schema": 1, "recipe": "a" * 64, "image": "gaoji-app-sandbox:" + "a" * 64,
                  "image_id": "sha256:" + "b" * 64, "bytes": 1024}
        write_json(self.controller.sandbox_image_path, record)
        restored = Controller("1" * 32, self.root)
        self.assertFalse(restored.sandbox.available)
        with patch.object(restored, "sandbox_build", return_value=Mock(recipe_id="a" * 64)):
            state = restored.status()["sandbox_runtime"]
        self.assertTrue(state["supported"])
        self.assertEqual(state["state"], "not_installed")

    def test_video_activation_rechecks_model_before_starting_bot(self):
        settings = deepcopy(self.config["settings"])
        settings["models"][0]["vision"] = True
        settings["features"].update(media=True, video=True)
        config = self.store.update(self.config["id"], settings, expected_revision=self.config["revision"])
        with patch.object(self.controller, "resume"):
            self.controller.submit({"id": "e" * 32, "kind": "activate", "revision": config["revision"]})
        controller = Controller("1" * 32, self.root)
        controller.capacity.engine_inventory = self.controller.capacity.engine_inventory
        calls = []
        def compose(arguments, **_kwargs):
            calls.append(arguments)
            if "--force-recreate" in arguments:
                write_json(self.directory / "state/desktop-health.json", {"qq": "12345678", "connected": True, "updated_at": 9999999999})
            return ""
        with patch.object(controller, "compose", side_effect=compose), patch.object(controller, "render"), \
            patch.object(controller, "runtime_probe"), patch.object(controller.napcat, "configure"), \
            patch.object(controller.napcat, "status", return_value={"online": True, "qq": "12345678"}), \
            patch("src.local_runtime.manager.VideoAssets.ensure") as install:
            controller.execute()
        install.assert_called_once()
        check = next(i for i, command in enumerate(calls) if "check-video" in command)
        start = next(i for i, command in enumerate(calls) if "--force-recreate" in command)
        self.assertLess(check, start)
        self.assertEqual(controller.operation["id"], "e" * 32)
        self.assertEqual(controller.operation["status"], "completed")


class SetupClientTests(unittest.TestCase):
    def test_model_listing_and_reply_are_real_distinct_probes(self):
        with patch("src.local_runtime.setup_clients.request_json", return_value={"data": [{"id": "a"}, {"id": "a"}, {"id": "b"}]}) as request:
            self.assertEqual(probe_model(PROFILE, "private", listing=True), {"models": ["a", "b"]})
            self.assertIsNone(request.call_args.args[1])
        with patch("src.local_runtime.setup_clients.request_json", return_value={"choices": [{"message": {"content": "OK"}}]}):
            self.assertTrue(probe_model(PROFILE, "private")["responded"])
        with patch("src.local_runtime.setup_clients.request_json", return_value={"choices": [{"message": {"reasoning_content": "still thinking"}}]}):
            with self.assertRaisesRegex(SetupError, "没有返回文字"):
                probe_model(PROFILE, "private")

    def test_napcat_login_is_cached_and_qr_not_returned_after_login(self):
        def request(url, payload, headers=None, **kwargs):
            if url.endswith("/auth/login"):
                return {"code": 0, "data": {"Credential": "session"}}
            if url.endswith("/CheckLoginStatus"):
                return {"code": 0, "data": {"isLogin": True, "qrcodeurl": "private-qr"}}
            return {"code": 0, "data": {"user_id": 12345678, "nickname": "Test"}}
        client = NapCatSetup(6099, "private-token")
        with patch("src.local_runtime.setup_clients.request_json", side_effect=request) as mock:
            first = client.status(); second = client.status()
        self.assertEqual(first, second)
        self.assertEqual(first["qrcode"], "")
        self.assertEqual(first["qq"], "12345678")
        self.assertEqual(sum(call.args[0].endswith("/auth/login") for call in mock.call_args_list), 1)


if __name__ == "__main__":
    unittest.main()

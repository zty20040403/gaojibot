from __future__ import annotations

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.local_runtime.backups import BackupStore, verify_archive
from src.local_runtime.components import ComponentError, DEFAULTS, validate_components
from src.local_runtime.files import write_json
from src.local_runtime.entrypoint import main as runtime_main
from src.local_runtime.instances import InstanceError, InstanceStore, validate_settings
from src.local_runtime.runtime import runtime_environment
from src.local_runtime.setup_clients import SetupError, probe_embedding


class ComponentsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = InstanceStore(Path(temporary.name))
        self.config = self.store.create("components")
        self.identifier = self.config["id"]
        self.settings = self.config["settings"]
        self.settings.update(bot_qq="12345678", admin_qq="87654321", default_model="primary",
            models=[{"id": "primary", "model": "test", "base_url": "https://fixture.invalid/v1", "vision": True}])
        self.settings["components"] = deepcopy(DEFAULTS)

    def save(self, **kwargs):
        return self.store.update(self.identifier, self.settings, expected_revision=self.store.read(self.identifier)["revision"], **kwargs)

    def embedding(self):
        self.settings["components"]["embedding"].update(base_url="https://embedding.invalid/v1", api_key_required=True)
        self.settings["features"]["semantic"] = True

    def test_legacy_settings_defaults_and_invalid_fields(self):
        legacy = deepcopy(self.settings)
        legacy.pop("components")
        self.assertEqual(validate_settings(legacy)["components"], DEFAULTS)
        invalid = [
            {"unknown": {}}, {"voice": {**DEFAULTS["voice"], "command": "sh"}},
            {"embedding": {**DEFAULTS["embedding"], "api_key": "secret"}},
            {"embedding": {**DEFAULTS["embedding"], "base_url": "https://secret@example.test/v1"}},
            {"embedding": {**DEFAULTS["embedding"], "dimensions": True}},
            {"embedding": {**DEFAULTS["embedding"], "dimensions": 1536}},
            {"voice": {**DEFAULTS["voice"], "pitch": "999999Hz"}},
            {"alerts": {"base_url": "https://alerts.invalid", "group_id": "123;echo"}},
        ]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(InstanceError):
                validate_components(options)

    def test_private_component_keys_are_revisioned_redacted_and_endpoint_bound(self):
        self.embedding()
        self.save(model_keys={"primary": "model-secret"}, service_keys={"embedding": "embedding-secret"})
        status = self.store.public_status(self.identifier)
        self.assertEqual(status["component_keys_present"], ["embedding"])
        self.assertNotIn("embedding-secret", json.dumps(status))
        self.assertNotIn("embedding-secret", (self.store.directory(self.identifier) / "instance.json").read_text())
        self.settings["components"]["embedding"]["base_url"] = "https://different.invalid/v1"
        with self.assertRaisesRegex(InstanceError, "重新填写密钥"):
            self.save()
        self.assertEqual(self.store.read(self.identifier)["revision"], 2)
        self.save(service_keys={"embedding": ""})
        self.assertEqual(self.store.snapshot(self.identifier)[1]["component_keys"], {"embedding": ""})

    def test_interrupted_save_keeps_previous_component_key_pair(self):
        self.embedding()
        self.save(model_keys={"primary": "model-secret"}, service_keys={"embedding": "old-secret"})
        original = write_json
        def fail_config(path, value):
            if path.name == "instance.json":
                raise OSError("simulated crash")
            original(path, value)
        with patch("src.local_runtime.instances.write_json", side_effect=fail_config), self.assertRaises(OSError):
            self.save(service_keys={"embedding": "new-secret"})
        config, private = self.store.snapshot(self.identifier)
        self.assertEqual(config["revision"], 2)
        self.assertEqual(private["component_keys"], {"embedding": "old-secret"})
        self.save(service_keys={"embedding": "retry-secret"})
        self.assertEqual(self.store.snapshot(self.identifier)[1]["component_keys"], {"embedding": "retry-secret"})

    def test_actual_runtime_configuration_receives_selected_options(self):
        self.embedding()
        self.settings["features"].update(media=True, voice=True, browser=True, historian=True)
        self.settings["components"]["voice"].update(name="zh-CN-XiaoyiNeural", rate="+20%")
        self.settings["components"]["search"]["max_results"] = 8
        self.save(model_keys={"primary": "model-secret"}, service_keys={"embedding": "embedding-secret"})
        env = runtime_environment(*self.store.snapshot(self.identifier))
        self.assertEqual(env["AI_EMBEDDING_API_KEY"], "embedding-secret")
        self.assertEqual(env["AI_EMBEDDING_MODEL"], "bge-m3")
        self.assertEqual(env["AI_EMBEDDING_DIMENSIONS"], "1024")
        self.assertEqual(env["AI_VOICE_NAME"], "zh-CN-XiaoyiNeural")
        self.assertEqual(env["AI_VOICE_RATE"], "+20%")
        self.assertEqual(env["AI_SEARCH_MAX_RESULTS"], "8")
        self.assertEqual(env["AI_BROWSER_ENABLED"], "true")
        self.assertEqual(env["AI_HISTORIAN_PROFILE"], "primary")
        self.assertEqual(env["AI_VISION_AUTO_DESCRIBE"], "false")
        self.assertEqual(env["AI_PROACTIVE_ENABLED"], "false")

    def test_enabled_features_need_dependencies_before_start(self):
        self.save(model_keys={"primary": "model-secret"})
        for feature, expected in (("semantic", "Embedding"), ("notifications", "Alertmanager"), ("sandbox", "沙盒"), ("video", "视频"), ("cluster", "集群")):
            config, private = self.store.snapshot(self.identifier)
            config["settings"]["features"][feature] = True
            with self.subTest(feature=feature), self.assertRaisesRegex(ComponentError, expected):
                runtime_environment(config, private)
            runtime_environment(config, private, configured=False)
        config, private = self.store.snapshot(self.identifier)
        config["settings"]["features"]["media"] = True
        config["settings"]["models"][0]["vision"] = False
        with self.assertRaisesRegex(ComponentError, "识图"):
            runtime_environment(config, private)

    def test_video_roles_options_and_budgets_reach_runtime(self):
        self.settings["features"].update(media=True, video=True)
        self.settings["components"]["video"].update(threads=3, frame_count=6, max_download_mb=128)
        self.save(model_keys={"primary": "model-secret"})
        env = runtime_environment(*self.store.snapshot(self.identifier))
        self.assertEqual(env["AI_VIDEO_DEEP_ENABLED"], "true")
        self.assertEqual(env["AI_VIDEO_WHISPER_THREADS"], "3")
        self.assertEqual(env["AI_VIDEO_FRAME_COUNT"], "6")
        self.assertEqual(env["AI_VIDEO_MAX_DOWNLOAD_MB"], "128")
        self.assertEqual(env["AI_VIDEO_MAX_DURATION_MINUTES"], "60")
        self.assertEqual(env["AI_VIDEO_WHISPER_MODEL_PATH"], "/opt/gaoji-models/ggml-base.bin")
        for changes in ({"threads": 99}, {"max_duration_minutes": 61}, {"model_url": "https://untrusted.invalid/model"}):
            with self.subTest(changes=changes), self.assertRaises(InstanceError):
                validate_components({"video": {**DEFAULTS["video"], **changes}})

    def test_alert_target_must_be_enabled_group(self):
        self.settings["features"]["notifications"] = True
        self.settings["components"]["alerts"] = {"base_url": "https://alerts.invalid", "group_id": "12345678"}
        self.save(model_keys={"primary": "model-secret"})
        with self.assertRaisesRegex(ComponentError, "已启用"):
            runtime_environment(*self.store.snapshot(self.identifier))
        self.settings["group_ids"] = ["12345678"]
        self.save()
        self.assertEqual(runtime_environment(*self.store.snapshot(self.identifier))["AI_ALERT_NOTIFY_GROUP_ID"], "12345678")

    def test_component_secrets_survive_private_backup(self):
        self.embedding()
        self.save(model_keys={"primary": "model-secret"}, service_keys={"embedding": "embedding-secret"})
        directory = self.store.directory(self.identifier)
        (directory / "config/admin.secret").write_bytes(b"x" * 32)
        dump = directory / "dump"
        dump.write_bytes(b"PGDMPfixture")
        backups = BackupStore(directory)
        record = backups.publish("b" * 32, *self.store.snapshot(self.identifier), dump=dump, checksum="a" * 64, database_bytes=0)
        result = verify_archive(backups.path(record["id"]) / "snapshot.tar", expected_checksum="a" * 64, max_bytes=record["bytes"])
        self.assertEqual(result["snapshot"]["private"]["component_key_sets"]["2"], {"embedding": "embedding-secret"})
        self.assertNotIn("embedding-secret", json.dumps(record))


class EmbeddingProbeTests(unittest.TestCase):
    def test_container_probe_entrypoint_never_loads_database_or_inherited_configuration(self):
        payload = {"options": {**DEFAULTS["embedding"], "base_url": "https://fixture.invalid/v1"}, "key": "only-stdin-secret"}
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", output), \
                patch("src.local_runtime.entrypoint.load_environment") as load, \
                patch("src.local_runtime.setup_clients.probe_embedding", return_value={"responded": True, "dimensions": 1024}) as probe:
            self.assertEqual(runtime_main(["probe-embedding"]), 0)
            probe.assert_called_once_with(payload["options"], "only-stdin-secret")
            load.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {"data": {"responded": True, "dimensions": 1024}})
        self.assertNotIn("only-stdin-secret", output.getvalue())
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", output), \
                patch("src.local_runtime.setup_clients.probe_embedding", side_effect=SetupError("服务限流")):
            self.assertEqual(runtime_main(["probe-embedding"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"error": "服务限流"})

    def test_real_local_http_probe_and_authorization(self):
        captured = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_POST(self):
                captured.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                body = json.dumps({"data": [{"embedding": [0.2] * (1024 if len(captured) == 1 else 8)}]}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            options = {**DEFAULTS["embedding"], "base_url": f"http://127.0.0.1:{server.server_port}/v1"}
            result = probe_embedding(options, "fixture-key")
            self.assertEqual(result["dimensions"], 1024)
            self.assertEqual(captured, [("/v1/embeddings", "Bearer fixture-key", {"model": "bge-m3", "input": ["connection check"], "dimensions": 1024})])
            with self.assertRaisesRegex(SetupError, "不一致"):
                probe_embedding(options, "")
            self.assertIsNone(captured[-1][1])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_invalid_vectors_and_missing_key_rejected(self):
        options = {**DEFAULTS["embedding"], "base_url": "https://fixture.invalid/v1"}
        for vector in ([], [True] * 8, [float("nan")] * 8, "encoded-string"):
            with patch("src.local_runtime.setup_clients.request_json", return_value={"data": [{"embedding": vector}]}), self.assertRaises(SetupError):
                probe_embedding(options, "")
        with self.assertRaisesRegex(ComponentError, "密钥"):
            probe_embedding({**options, "api_key_required": True}, "")


if __name__ == "__main__":
    unittest.main()

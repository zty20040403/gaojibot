from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.local_runtime.instances import ConfigurationConflict, InstanceError, InstanceStore
from src.local_runtime.runtime import runtime_environment
from src.local_runtime.setup_clients import probe_model
from src.llm_options import anthropic_messages_url, chat_options

_spec = importlib.util.spec_from_file_location("gaoji_desktop_catalog_test", Path(__file__).parents[1] / "src/plugins/ai_chat/model_catalog.py")
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)


class DesktopCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = InstanceStore(Path(temporary.name))
        self.config = self.store.create("catalog")
        self.id = self.config["id"]
        self.settings = self.config["settings"]
        self.settings.update(bot_qq="12345678", admin_qq="87654321", default_model="everyday", vision_model="vision",
            models=[
                {"id": "everyday", "label": "日常聊天", "provider": "qwen", "base_url": "http://model.invalid:8000/v1", "model": "qwen-test", "api_key_required": False, "thinking": "enabled", "fallbacks": ["code", "vision"]},
                {"id": "code", "label": "写代码", "provider": "openai", "base_url": "https://provider.invalid/v1", "model": "code-test", "reasoning_effort": "xhigh", "max_input_tokens": 131072, "max_output_tokens": 8192, "fallbacks": ["vision"]},
                {"id": "vision", "label": "看图片", "provider": "anthropic", "protocol": "anthropic-messages", "base_url": "https://vision.invalid", "model": "vision-test", "vision": True, "thinking": "enabled"},
            ])

    def test_real_model_catalog_receives_profiles_keys_roles_and_order(self):
        self.store.update(self.id, self.settings, expected_revision=1, model_keys={"code": "code-private-key", "vision": "vision-private-key"})
        env = runtime_environment(*self.store.snapshot(self.id))
        catalog = _module.ModelCatalog.from_json(env["AI_MODEL_PROFILES_JSON"], default_profile=env["AI_MODEL_DEFAULT_PROFILE"], environ=env)
        self.assertEqual(catalog.default.name, "everyday")
        self.assertTrue(catalog.default.configured)
        self.assertEqual(catalog.default.api_key, "")
        self.assertEqual(catalog.default.thinking, "enabled")
        self.assertEqual(catalog.default.fallback_profiles, ("code", "vision"))
        self.assertEqual(catalog.resolve("code").api_key, "code-private-key")
        self.assertEqual(catalog.resolve("code").reasoning_effort, "xhigh")
        self.assertEqual(catalog.resolve("code").max_input_tokens, 131072)
        self.assertEqual(catalog.resolve("code").max_output_tokens, 8192)
        self.assertEqual(catalog.resolve("vision").api_key, "vision-private-key")
        self.assertTrue(catalog.resolve(env["AI_VISION_PROFILE"]).capabilities.vision)
        self.assertEqual({profile.name for profile in catalog.profiles}, {"everyday", "code", "vision"})
        self.assertNotIn("private-key", env["AI_MODEL_PROFILES_JSON"])

    def test_batch_import_copies_credentials_only_within_original_endpoint(self):
        config = self.store.update(self.id, self.settings, expected_revision=1, model_keys={"code": "private-value"})
        settings = deepcopy(config["settings"])
        settings["models"].append({**settings["models"][1], "id": "second", "model": "another-model", "fallbacks": []})
        self.store.update(self.id, settings, expected_revision=2, model_key_sources={"second": "code"})
        self.assertEqual(self.store.snapshot(self.id)[1]["model_keys"]["second"], "private-value")
        self.assertNotIn("private-value", json.dumps(self.store.public_status(self.id)))
        for change in ({"base_url": "https://other.invalid/v1"}, {"protocol": "anthropic-messages"}):
            changed = deepcopy(settings)
            changed["models"][-1].update(change)
            with self.subTest(change=change), self.assertRaises(InstanceError):
                self.store.update(self.id, changed, expected_revision=3, model_key_sources={"second": "code"})
        with self.assertRaises(InstanceError):
            self.store.update(self.id, settings, expected_revision=3, model_key_sources={"second": "missing"})

    def test_deleting_a_profile_removes_active_key_without_affecting_other_models(self):
        config = self.store.update(self.id, self.settings, expected_revision=1, model_keys={"code": "code-private-key", "vision": "vision-private-key"})
        settings = deepcopy(config["settings"])
        settings["vision_model"] = ""
        settings["models"] = [model for model in settings["models"] if model["id"] != "vision"]
        for model in settings["models"]:
            model["fallbacks"] = [name for name in model["fallbacks"] if name != "vision"]
        self.store.update(self.id, settings, expected_revision=2)
        _, private = self.store.snapshot(self.id)
        self.assertEqual(private["model_keys"], {"code": "code-private-key"})
        self.assertNotIn("vision-private-key", json.dumps(runtime_environment(*self.store.snapshot(self.id))))
        with self.assertRaises(ConfigurationConflict):
            self.store.update(self.id, settings, expected_revision=2)

    def test_dangling_roles_cycles_and_unrecognized_thinking_are_rejected(self):
        for mutation in (
            lambda settings: settings.update(default_model="unknown"),
            lambda settings: settings["models"][2].update(fallbacks=["everyday"]),
            lambda settings: settings["models"][2].update(thinking="magic"),
            lambda settings: settings["models"].pop(),
        ):
            settings = deepcopy(self.settings)
            mutation(settings)
            with self.subTest(settings=settings), self.assertRaises(InstanceError):
                self.store.update(self.id, settings, expected_revision=1)
        self.assertEqual(self.store.read(self.id)["revision"], 1)


class ModelProbeOptionsTests(unittest.TestCase):
    def test_listing_preserves_all_models_and_deduplicates_without_chat_probe(self):
        ids = ["gpt-5.6-luna", "gpt-5.6-sol", "gpt-image-2", "gpt-5.6-terra"]
        with patch("src.local_runtime.setup_clients.request_json", return_value={"data": [{"id": name} for name in ids + ids]}) as request:
            result = probe_model({"id": "p", "base_url": "http://fixture.invalid/v1", "model": "list"}, "fixture-key", listing=True)
        self.assertEqual(result["models"], sorted(ids))
        self.assertEqual(request.call_args.args[0], "http://fixture.invalid/v1/models")
    def test_probe_uses_same_wire_options_as_chat(self):
        for provider, thinking, effort in (("qwen", "enabled", ""), ("deepseek", "disabled", ""), ("openai", "auto", "xhigh"), ("cliproxy", "auto", "high")):
            profile = {"id": "test", "provider": provider, "protocol": "openai-chat", "model": "fixture-model", "base_url": "https://fixture.invalid/v1",
                "thinking": thinking, "reasoning_effort": effort, "max_output_tokens": 8192}
            with self.subTest(provider=provider), patch("src.local_runtime.setup_clients.request_json", return_value={"choices": [{"message": {"content": "OK"}}]}) as request:
                self.assertTrue(probe_model(profile, "private-key")["responded"])
                args, kwargs = request.call_args
                self.assertEqual(args[0], "https://fixture.invalid/v1/chat/completions")
                options = chat_options(provider, "openai-chat", thinking, effort)
                expected = {**options.pop("extra_body", {}), **options}
                for key, value in expected.items():
                    self.assertEqual(args[1][key], value)
                self.assertEqual(args[1]["max_tokens"], 8192)
                self.assertNotIn("extra_body", args[1])
                self.assertNotIn("private-key", json.dumps(args[1]))
                self.assertEqual(args[2], {"Authorization": "Bearer private-key"})
                self.assertEqual(kwargs["timeout"], 45)

    def test_anthropic_root_v1_and_full_path_use_identical_endpoint(self):
        for base in ("https://fixture.invalid", "https://fixture.invalid/v1/", "https://fixture.invalid/v1/messages"):
            model = {"id": "claude", "provider": "anthropic", "protocol": "anthropic-messages", "model": "fixture-model", "base_url": base,
                "thinking": "enabled", "max_output_tokens": 2048}
            with self.subTest(base=base), patch("src.local_runtime.setup_clients.request_json", return_value={"content": [{"type": "text", "text": "OK"}]}) as request:
                probe_model(model, "private-key")
                args, _kwargs = request.call_args
                self.assertEqual(args[0], anthropic_messages_url(base))
                self.assertEqual(args[0], "https://fixture.invalid/v1/messages")
                self.assertEqual(args[1]["thinking"], {"type": "adaptive"})
                self.assertEqual(args[1]["max_tokens"], 2048)
                self.assertEqual(args[2]["x-api-key"], "private-key")


if __name__ == "__main__":
    unittest.main()

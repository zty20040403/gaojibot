from __future__ import annotations

import json
import unittest
from dataclasses import replace



from src.plugins.ai_chat.config import settings
from src.plugins.ai_chat.model_catalog import (
    ModelCatalog,
    ModelCatalogError,
)


class ModelCatalogTests(unittest.TestCase):
    def test_forced_tool_choice_capability_is_configurable(self) -> None:
        catalog = ModelCatalog.from_json(
            json.dumps({
                "qwen-local": {
                    "model": "qwen-test",
                    "api_key_required": False,
                    "capabilities": {"forced_tool_choice": False},
                }
            }),
            default_profile="qwen-local",
            environ={},
        )
        self.assertFalse(catalog.default.capabilities.forced_tool_choice)

    def test_circuit_breaker_is_opt_out_per_profile(self) -> None:
        catalog = ModelCatalog.from_json(
            json.dumps({
                "qwen-local": {
                    "model": "qwen-test", "api_key_required": False,
                    "circuit_breaker_enabled": False,
                },
                "deepseek": {"model": "deepseek-test", "api_key_required": False},
            }),
            default_profile="qwen-local",
            environ={},
        )
        self.assertFalse(catalog.default.circuit_breaker_enabled)
        self.assertTrue(catalog.resolve("deepseek").circuit_breaker_enabled)
        self.assertFalse(catalog.default.with_model("qwen-other").circuit_breaker_enabled)
        with self.assertRaises(ModelCatalogError):
            ModelCatalog.from_json(
                json.dumps({"qwen-local": {
                    "model": "qwen-test", "circuit_breaker_enabled": "maybe",
                }}),
                default_profile="qwen-local",
                environ={},
            )

    def test_profiles_resolve_independent_secrets_protocols_and_aliases(self) -> None:
        raw = json.dumps(
            {
                "default": "fast",
                "profiles": {
                    "fast": {
                        "provider": "deepseek",
                        "protocol": "openai-chat",
                        "base_url": "https://api.deepseek.com",
                        "api_key_env": "DEEPSEEK_TEST_KEY",
                        "model": "deepseek-chat",
                        "aliases": ["ds"],
                        "reasoning_effort": "medium",
                    },
                    "claude": {
                        "provider": "anthropic",
                        "protocol": "anthropic-messages",
                        "api_key_env": "ANTHROPIC_TEST_KEY",
                        "model": "claude-sonnet-test",
                        "max_output_tokens": 2048,
                    },
                },
            }
        )

        catalog = ModelCatalog.from_json(
            raw,
            default_profile="ignored",
            environ={
                "DEEPSEEK_TEST_KEY": "secret-one",
                "ANTHROPIC_TEST_KEY": "secret-two",
            },
        )

        self.assertEqual(catalog.default.name, "fast")
        self.assertEqual(catalog.resolve("ds").model, "deepseek-chat")
        self.assertEqual(catalog.resolve("ds").reasoning_effort, "medium")
        claude = catalog.resolve("claude")
        self.assertEqual(claude.protocol, "anthropic-messages")
        self.assertEqual(claude.api_key, "secret-two")
        self.assertFalse(claude.capabilities.streaming)
        self.assertFalse(claude.capabilities.json_mode)
        self.assertNotIn("secret-two", repr(claude))

    def test_custom_catalog_unknown_preference_falls_back_to_default(self) -> None:
        catalog = ModelCatalog.from_json(
            json.dumps(
                {
                    "main": {
                        "model": "model-a",
                        "api_key_required": False,
                    }
                }
            ),
            default_profile="main",
            environ={},
        )

        self.assertEqual(catalog.resolve_preference("removed-profile").name, "main")
        self.assertEqual(catalog.resolve_preference("removed-profile").model, "model-a")

    def test_legacy_settings_keep_previous_raw_model_preferences_working(self) -> None:
        legacy_settings = replace(
            settings,
            model_profiles_json="",
            model_default_profile="deepseek",
            deepseek_api_key="test-key",
            deepseek_model="deepseek-chat",
        )

        catalog = ModelCatalog.from_settings(legacy_settings, environ={})
        selected = catalog.resolve_preference("deepseek-old-model")

        self.assertTrue(catalog.legacy_fallback)
        self.assertEqual(selected.name, "deepseek")
        self.assertEqual(selected.model, "deepseek-old-model")
        self.assertEqual(catalog.resolve("flash").model, "deepseek-v4-flash")
        self.assertEqual(catalog.resolve("pro").model, "deepseek-v4-pro")

    def test_invalid_secret_source_and_alias_collision_fail_fast(self) -> None:
        with self.assertRaisesRegex(ModelCatalogError, "both api_key"):
            ModelCatalog.from_json(
                json.dumps(
                    {
                        "main": {
                            "model": "a",
                            "api_key": "secret",
                            "api_key_env": "MODEL_KEY",
                        }
                    }
                ),
                default_profile="main",
                environ={"MODEL_KEY": "other"},
            )

        with self.assertRaisesRegex(ModelCatalogError, "used by both"):
            ModelCatalog.from_json(
                json.dumps(
                    {
                        "a": {
                            "model": "a",
                            "api_key_required": False,
                            "aliases": ["shared"],
                        },
                        "b": {
                            "model": "b",
                            "api_key_required": False,
                            "aliases": ["shared"],
                        },
                    }
                ),
                default_profile="a",
                environ={},
            )

    def test_invalid_protocol_is_rejected(self) -> None:
        with self.assertRaisesRegex(ModelCatalogError, "unsupported protocol"):
            ModelCatalog.from_json(
                json.dumps(
                    {
                        "main": {
                            "protocol": "made-up-api",
                            "model": "anything",
                            "api_key_required": False,
                        }
                    }
                ),
                default_profile="main",
                environ={},
            )

    def test_fallback_profiles_resolve_aliases_and_reject_unknown_names(self) -> None:
        catalog = ModelCatalog.from_json(
            json.dumps(
                {
                    "primary": {
                        "model": "model-a",
                        "api_key_required": False,
                        "fallback_profiles": ["backup-alias"],
                    },
                    "backup": {
                        "model": "model-b",
                        "api_key_required": False,
                        "aliases": ["backup-alias"],
                    },
                }
            ),
            default_profile="primary",
            environ={},
        )

        self.assertEqual(
            catalog.resolve("primary").fallback_profiles,
            ("backup",),
        )
        with self.assertRaisesRegex(ModelCatalogError, "unknown fallback"):
            ModelCatalog.from_json(
                json.dumps(
                    {
                        "primary": {
                            "model": "model-a",
                            "api_key_required": False,
                            "fallback_profiles": ["missing"],
                        }
                    }
                ),
                default_profile="primary",
                environ={},
            )


if __name__ == "__main__":
    unittest.main()

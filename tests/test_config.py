from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import nonebot

nonebot.init()

from src.plugins.ai_chat.config import Settings


class SettingsGroupFilterTests(unittest.TestCase):
    def test_intent_classifier_defaults_and_bounded_timeout(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertTrue(settings.tool_intent_enabled)
        self.assertEqual(settings.tool_intent_timeout_seconds, 3.0)
        for value, expected in (("0", 0.1), ("-1", 0.1), ("60", 10), ("2.5", 2.5),
                                ("nan", 3), ("inf", 3), ("bad", 3)):
            with self.subTest(value=value), patch.dict(os.environ, {
                "AI_TOOL_INTENT_ENABLED": "false", "AI_TOOL_INTENT_TIMEOUT_SECONDS": value,
            }, clear=True):
                settings = Settings.from_env()
                self.assertFalse(settings.tool_intent_enabled)
                self.assertEqual(settings.tool_intent_timeout_seconds, expected)

    def test_sandbox_allowlist_denies_by_default_and_allow_all_is_explicit(self) -> None:
        cases = (
            ({}, False, False),
            ({"AI_SANDBOX_ENABLED": "true"}, False, False),
            ({"AI_SANDBOX_ENABLED": "true", "AI_SANDBOX_ALLOWED_USERS": "101"}, True, False),
            ({"AI_SANDBOX_ENABLED": "true", "AI_SANDBOX_ALLOW_ALL": "true"}, True, True),
            ({"AI_SANDBOX_ENABLED": "false", "AI_SANDBOX_ALLOW_ALL": "true"}, False, False),
        )
        for environment, first, second in cases:
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True):
                settings = Settings.from_env()
                self.assertEqual(settings.is_sandbox_user_allowed(101), first)
                self.assertEqual(settings.is_sandbox_user_allowed(202), second)

    def test_sandbox_limits_are_bounded_and_invalid_configuration_fails(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual((settings.sandbox_cpus, settings.sandbox_pids_limit, settings.sandbox_memory_mib),
                         (2, 256, 8192))
        for name, maximum in (("AI_SANDBOX_CPUS", 16), ("AI_SANDBOX_PIDS_LIMIT", 4096),
                              ("AI_SANDBOX_MEMORY_MIB", 8192)):
            for value in ("0", "-1", "unlimited", str(maximum + 1)):
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}, clear=True):
                    with self.assertRaisesRegex(ValueError, name):
                        Settings.from_env()

    def test_proactive_defaults_use_interest_gate_and_frequent_voice(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.proactive_interest_threshold, 98)
        self.assertEqual(settings.proactive_gate_percent, 10)
        self.assertEqual(settings.proactive_max_checks_per_hour, 6)
        self.assertEqual(settings.proactive_classifier_profile, "deepseek")
        self.assertEqual(settings.proactive_voice_percent, 60)

    def test_zero_sandbox_file_limit_means_unlimited(self) -> None:
        with patch.dict(
            os.environ,
            {"AI_SANDBOX_MAX_FILE_MB": "0"},
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.sandbox_max_file_bytes, 0)

    def test_media_defaults_use_luna_and_are_disabled_locally(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()

        self.assertFalse(settings.media_enabled)
        self.assertEqual(settings.vision_profile, "gpt-5.6-luna")
        self.assertFalse(settings.vision_auto_describe)
        self.assertEqual(settings.media_max_vision_bytes, 20 * 1024 * 1024)

    def test_simple_chat_profile_is_optional(self) -> None:
        with patch.dict(
            os.environ,
            {"AI_SIMPLE_CHAT_PROFILE": "qwen-local"},
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.model_simple_chat_profile, "qwen-local")

    def test_disabled_groups_override_allowlist(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AI_ENABLED_GROUPS": "100,200",
                "AI_DISABLED_GROUPS": "200,300",
            },
        ):
            settings = Settings.from_env()

        self.assertTrue(settings.is_group_enabled(100))
        self.assertFalse(settings.is_group_enabled(200))
        self.assertFalse(settings.is_group_enabled(300))
        self.assertFalse(settings.is_group_enabled(400))

    def test_disabled_groups_work_without_allowlist(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AI_ENABLED_GROUPS": "",
                "AI_DISABLED_GROUPS": "201644592",
            },
        ):
            settings = Settings.from_env()

        self.assertFalse(settings.is_group_enabled(201644592))
        self.assertTrue(settings.is_group_enabled(930690526))

    def test_group_model_profiles_parse_group_ids_and_profile_names(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AI_GROUP_MODEL_PROFILES_JSON": (
                    '{"201644592":"gpt-5.6-sol","930690526":"deepseek"}'
                )
            },
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(
            settings.group_model_profiles,
            {201644592: "gpt-5.6-sol", 930690526: "deepseek"},
        )

    def test_group_model_profiles_reject_invalid_json(self) -> None:
        with patch.dict(
            os.environ,
            {"AI_GROUP_MODEL_PROFILES_JSON": "not-json"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "must be valid JSON"):
                Settings.from_env()

    def test_admin_user_ids_parse_qq_accounts(self) -> None:
        with patch.dict(
            os.environ,
            {"AI_ADMIN_USER_IDS": "3526452465, 2291939848,invalid"},
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.admin_user_ids, {3526452465, 2291939848})

    def test_activity_alert_notification_settings(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AI_ALERT_NOTIFY_ENABLED": "true",
                "AI_ALERT_NOTIFY_GROUP_ID": "611798505",
                "AI_ALERT_NOTIFY_CHECK_SECONDS": "5",
            },
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertTrue(settings.alert_notify_enabled)
        self.assertEqual(settings.alert_notify_group_id, 611798505)
        self.assertEqual(settings.alert_notify_check_seconds, 10)

    def test_postgres_node_names_parse_in_dsn_order(self) -> None:
        with patch.dict(
            os.environ,
            {"AI_POSTGRES_NODE_NAMES": "h610, tank"},
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.postgres_node_names, ("h610", "tank"))


if __name__ == "__main__":
    unittest.main()

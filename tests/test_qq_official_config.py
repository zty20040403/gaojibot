from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official import config


class OfficialConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in (("app", "TEST_CONFIG_APP"), ("secret", "TEST_CONFIG_SECRET"), ("pepper", "12" * 32)):
            path = root / name
            path.write_text(value)
            path.chmod(0o600)
        self.env = {
            "AI_QQ_OFFICIAL_ENABLED": "true",
            "AI_QQ_OFFICIAL_APP_ID_FILE": str(root / "app"),
            "AI_QQ_OFFICIAL_APP_SECRET_FILE": str(root / "secret"),
            "AI_QQ_OFFICIAL_BIND_PEPPER_FILE": str(root / "pepper"),
            "AI_QQ_OFFICIAL_API_BASE": "http://127.0.0.1:12345",
            "AI_QQ_OFFICIAL_OWNER_OPENIDS": '["TEST_所有者", "TEST_所有者"]',
        }

    def test_disabled_configuration_does_not_read_or_validate_dormant_credentials(self):
        with patch.object(config.Credentials, "from_files", side_effect=AssertionError("read")), patch.object(
            config, "_read_secret", side_effect=AssertionError("read"),
        ):
            dormant = config.OfficialConfig.from_env({**self.env, "AI_QQ_OFFICIAL_ENABLED": "false",
                "AI_QQ_OFFICIAL_PRIVATE_POLICY": "invalid", "AI_QQ_OFFICIAL_OWNER_OPENIDS": "[invalid"})
            self.assertFalse(dormant.enabled)
            self.assertIsNone(dormant.load())

    def test_loaded_identity_is_bound_to_credentials_and_test_environment(self):
        loaded = config.OfficialConfig.from_env(self.env).load(allow_test_server=True)
        self.assertEqual(loaded.account.environment, "test")
        self.assertEqual(loaded.account.app_id, "TEST_CONFIG_APP")
        self.assertEqual(len(loaded.owner_actors), 1)
        self.assertEqual(loaded.owner_actors[0].account, loaded.account)
        self.assertEqual(loaded.owner_actors[0].identity_kind, "c2c_user")
        self.assertEqual(loaded.binding_pepper, bytes.fromhex("12" * 32))
        self.assertNotIn("TEST_", repr(loaded))
        self.assertNotIn(self.temp.name, repr(config.OfficialConfig.from_env(self.env)))

    def test_production_and_sandbox_cannot_silently_share_an_environment_key(self):
        value = config.OfficialConfig.from_env({**self.env, "AI_QQ_OFFICIAL_API_BASE": config.DEFAULT_API_BASE})
        self.assertEqual(value.load().account.environment, "production")
        sandbox = replace(value, sandbox=True, api_base="")
        self.assertEqual(sandbox.load().account.environment, "sandbox")
        self.assertNotEqual(value.load().account, sandbox.load().account)
        for wrong in (replace(value, sandbox=True), replace(value, api_base=config.SANDBOX_API_BASE)):
            with self.assertRaises(config.OfficialConfigurationError):
                wrong.load()

    def test_loopback_and_expected_account_must_be_explicit(self):
        value = config.OfficialConfig.from_env(self.env)
        with self.assertRaises(config.OfficialConfigurationError):
            value.load()
        wrong_account = config.AccountKey("qq-official", "TEST_OTHER_APP", "test")
        with self.assertRaises(config.OfficialConfigurationError):
            replace(value, expected_account_key=wrong_account.key).load(allow_test_server=True)
        with self.assertRaises(config.OfficialConfigurationError):
            replace(value, api_base="https://untrusted.invalid").load(allow_test_server=True)

    def test_pepper_is_protected_and_errors_do_not_include_values(self):
        value = config.OfficialConfig.from_env(self.env)
        pepper = Path(value.binding_pepper_file)
        pepper.write_text("TEST_INVALID_SENSITIVE_PEPPER")
        with self.assertRaises(config.OfficialConfigurationError) as failure:
            value.load(allow_test_server=True)
        self.assertNotIn("TEST_INVALID", str(failure.exception))
        self.assertNotIn(self.temp.name, str(failure.exception))
        pepper.write_text("12" * 32)
        pepper.chmod(0o644)
        with self.assertRaises(config.OfficialConfigurationError):
            value.load(allow_test_server=True)

    def test_admission_configuration_cannot_enable_unbound_groups(self):
        for override in (
            {"AI_QQ_OFFICIAL_GROUP_ADMISSION": "public"},
            {"AI_QQ_OFFICIAL_PRIVATE_POLICY": "first_contact"},
            {"AI_QQ_OFFICIAL_ENABLED": "perhaps"},
            {"AI_QQ_OFFICIAL_OWNER_OPENIDS": '["TEST_\\u0000"]'},
            {"AI_QQ_OFFICIAL_INBOX_PAYLOAD_TTL_HOURS": "0"},
        ):
            with self.subTest(override=next(iter(override))), self.assertRaises(config.OfficialConfigurationError):
                config.OfficialConfig.from_env({**self.env, **override})


if __name__ == "__main__":
    unittest.main()

"""/bgmodel reads for everyone, changes only for the host owner, and edits the console's settings."""
import contextlib
import json
import unittest
from types import SimpleNamespace

from src.plugins.ai_chat.adapters.qq_official.background_commands import command
from src.plugins.ai_chat.application.commands import CommandServices, dispatch_command, parse_command
from src.plugins.ai_chat.model_catalog import ModelCatalog
from src.plugins.ai_chat.pi_engine import BackgroundModelSettings
from tests.test_platform_commands import message


class Preferences:
    def __init__(self):
        self.values = {}

    def get_explicit(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def clear(self, key):
        return self.values.pop(key, None) is not None


class BackgroundCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner = True
        self.preferences = Preferences()
        settings = SimpleNamespace(background_models=('TEST_DEFAULT_A', 'TEST_DEFAULT_B'),
                                   background_model='TEST_DEFAULT_A', background_effort='high')
        self.settings = BackgroundModelSettings(self.preferences, settings)

        async def calls(function):
            return function()

        authority = SimpleNamespace(owner_for_actor=lambda *_: 'TEST_OWNER' if self.owner else None)
        runtime = SimpleNamespace(_calls=calls, store=SimpleNamespace(authority=authority))
        life = SimpleNamespace(effect_scope=lambda _inbound: contextlib.nullcontext('TEST_CURSOR'))
        catalog = ModelCatalog.from_json(json.dumps({"default": "gpt-6.2-sol", "profiles": {
            "gpt-6.2-sol": {"provider": "cliproxy", "model": "gpt-6.2-sol", "aliases": ["gpt62"],
                            "base_url": "http://cliproxy.test/v1", "api_key_env": "TEST_KEY_ENV"},
            "gemini-4-argon-high": {"provider": "agy", "model": "gemini-4-argon-high", "aliases": ["gemini4"],
                                    "base_url": "https://agy.test/agy/v1", "api_key_env": "TEST_KEY_ENV"},
        }}), default_profile="", environ={"TEST_KEY_ENV": "TEST_KEY"})
        self.services = CommandServices(background=command(
            runtime, life, SimpleNamespace(model_catalog=catalog), SimpleNamespace(model_settings=self.settings)))

    async def run_command(self, text):
        return await dispatch_command(parse_command(text), message(), services=self.services)

    async def test_anyone_reads_the_chain_and_effort(self):
        self.owner = False
        result = await self.run_command('/bgmodel')
        self.assertEqual(result.status, 'ok')
        self.assertIn('TEST_DEFAULT_A → TEST_DEFAULT_B', result.text)
        self.assertIn('推理强度：high', result.text)

    async def test_only_the_owner_changes_it(self):
        self.owner = False
        result = await self.run_command('/bgmodel gpt62')
        self.assertEqual(result.status, 'denied')
        self.assertEqual(self.preferences.values, {})

    async def test_owner_sets_a_chain_with_aliases_resolved(self):
        result = await self.run_command('/bgmodel gpt62, gemini4，rewind/gpt-6-astra')
        self.assertEqual(result.status, 'ok')
        self.assertEqual(self.settings.chain(), ['gpt-6.2-sol', 'gemini-4-argon-high', 'rewind/gpt-6-astra'])
        self.assertEqual(self.settings.get(), ('gpt-6.2-sol', 'high'))

    async def test_owner_sets_effort_and_keeps_the_chain(self):
        result = await self.run_command('/bgmodel effort xhigh')
        self.assertEqual(result.status, 'ok')
        self.assertEqual(self.settings.get(), ('TEST_DEFAULT_A', 'xhigh'))
        self.assertEqual(self.settings.chain(), ['TEST_DEFAULT_A', 'TEST_DEFAULT_B'])
        bad = await self.run_command('/bgmodel effort turbo')
        self.assertEqual(bad.status, 'syntax')
        self.assertEqual(self.settings.get()[1], 'xhigh')

    async def test_reset_returns_to_host_configuration(self):
        await self.run_command('/bgmodel gpt62')
        await self.run_command('/bgmodel effort low')
        result = await self.run_command('/bgmodel default')
        self.assertEqual(result.status, 'ok')
        self.assertEqual(self.preferences.values, {})
        self.assertEqual(self.settings.get(), ('TEST_DEFAULT_A', 'high'))

    async def test_too_long_chain_is_refused(self):
        result = await self.run_command('/bgmodel ' + ','.join(f'm{i}' for i in range(9)))
        self.assertEqual(result.status, 'syntax')
        self.assertEqual(self.preferences.values, {})

    async def test_chinese_names_are_not_commands(self):
        for text in ('/后台模型', '/后台'):
            self.assertIsNone(parse_command(text))

    async def test_help_lists_it_only_when_configured(self):
        shown = await dispatch_command(parse_command('/help'), message(), services=self.services)
        self.assertIn('/bgmodel', shown.text)
        hidden = await dispatch_command(parse_command('/help'), message(), services=CommandServices())
        self.assertNotIn('/bgmodel', hidden.text)
        missing = await dispatch_command(parse_command('/bgmodel'), message(), services=CommandServices())
        self.assertEqual(missing.status, 'unavailable')


if __name__ == '__main__':
    unittest.main()

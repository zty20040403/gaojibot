"""Real official factory chooses the configured engine without changing Codex defaults."""
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.plugins.ai_chat.adapters.qq_official import background_agents
from src.plugins.ai_chat.codex_engine import CodexEngine
from src.plugins.ai_chat.config import Settings
from src.plugins.ai_chat.pi_engine import PiEngine
from src.plugins.ai_chat.platform_types import AccountKey


class PiFactoryTests(unittest.IsolatedAsyncioTestCase):
    def test_settings_validate_opt_in_and_context(self):
        defaults = Settings.from_env({})
        self.assertEqual((defaults.background_engine, defaults.background_context_window), ('codex', 200000))
        selected = Settings.from_env({'AI_BACKGROUND_ENGINE': 'pi', 'AI_BACKGROUND_CONTEXT_WINDOW': '128000'})
        self.assertEqual((selected.background_engine, selected.background_context_window), ('pi', 128000))
        for changes in ({'AI_BACKGROUND_ENGINE': 'bogus'}, {'AI_BACKGROUND_CONTEXT_WINDOW': 'nan'},
                        {'AI_BACKGROUND_CONTEXT_WINDOW': '1'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Settings.from_env(changes)

    async def test_official_factory_selects_pi_only_for_nixos_containers(self):
        account = AccountKey('qq-official', 'TEST_APP', 'test')
        store = SimpleNamespace(clock=lambda: 0, transaction=Mock(), admitted=Mock())
        runtime = SimpleNamespace(store=store, jobs=SimpleNamespace(_transaction=store.transaction), _calls=Mock())
        token = SimpleNamespace(account_key=account.key)
        settings = SimpleNamespace(background_engine='pi', background_context_window=128000,
            sandbox_backend='container', background_gateway_port=18640)
        app = SimpleNamespace(settings=settings, model_preferences=Mock(), logger=logging.getLogger(__name__),
            model_catalog=Mock(), sandbox_manager=SimpleNamespace(backend='container'))
        with patch('src.plugins.ai_chat.adapters.qq_official.vm_lifecycle.OfficialVMLifecycle'):
            service = background_agents.OfficialBackgroundAgents(runtime, token, app)
        self.addAsyncCleanup(service.gateway.client.aclose)
        self.assertIsInstance(service.runner.engine, PiEngine)
        self.assertEqual(service.runner.engine.context_window, 128000)
        self.assertIn('uv pip install', background_agents.background_prompt(settings))
        self.assertIn('/etc/gaoji/skills/', background_agents.background_prompt(settings))
        self.assertIn('没有 apt 和 nix shell', background_agents.background_prompt(settings))
        self.assertIn('NixOS 容器', background_agents.background_prompt(settings))
        self.assertIn('只有 /work 会一直保留，其他位置的改动重启即丢失', background_agents.background_prompt(settings))
        self.assertNotIn('可以装软件。', background_agents.background_prompt(settings))

        options = dict(gateway=object(), model_settings=object(), start_vm=lambda _: None,
                       guest_exec=lambda *_: None, clock=lambda: 0)
        settings.background_engine = 'codex'
        self.assertIsInstance(background_agents.make_engine(settings, **options), CodexEngine)
        self.assertNotIn('uv pip install', background_agents.background_prompt(settings))
        settings.background_engine, settings.sandbox_backend = 'pi', 'vm'
        self.assertNotIn('uv pip install', background_agents.background_prompt(settings))
        with self.assertRaisesRegex(RuntimeError, 'NixOS'):
            background_agents.make_engine(settings, **options)
        settings.sandbox_backend = 'oci'
        with self.assertRaisesRegex(RuntimeError, 'NixOS'):
            background_agents.make_engine(settings, **options)

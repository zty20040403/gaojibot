"""Official background factory uses PiEngine in NixOS containers."""
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.plugins.ai_chat.adapters.qq_official import background_agents
from src.plugins.ai_chat.config import Settings
from src.plugins.ai_chat.pi_engine import PiEngine
from src.plugins.ai_chat.platform_types import AccountKey


class PiFactoryTests(unittest.IsolatedAsyncioTestCase):
    def test_settings_validate_context_window(self):
        defaults = Settings.from_env({})
        self.assertEqual((defaults.sandbox_backend, defaults.background_context_window), ('container', 200000))
        selected = Settings.from_env({'AI_BACKGROUND_CONTEXT_WINDOW': '128000'})
        self.assertEqual(selected.background_context_window, 128000)
        for changes in ({'AI_BACKGROUND_CONTEXT_WINDOW': 'nan'}, {'AI_BACKGROUND_CONTEXT_WINDOW': '1'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Settings.from_env(changes)

    async def test_official_factory_selects_pi_for_nixos_containers(self):
        account = AccountKey('qq-official', 'TEST_APP', 'test')
        store = SimpleNamespace(clock=lambda: 0, transaction=Mock(), admitted=Mock())
        runtime = SimpleNamespace(store=store, jobs=SimpleNamespace(_transaction=store.transaction), _calls=Mock())
        token = SimpleNamespace(account_key=account.key)
        settings = SimpleNamespace(background_context_window=128000,
            sandbox_backend='container', background_gateway_port=18640)
        app = SimpleNamespace(settings=settings, model_preferences=Mock(), logger=logging.getLogger(__name__),
            model_catalog=Mock(), sandbox_manager=SimpleNamespace(backend='container'))
        with patch('src.plugins.ai_chat.adapters.qq_official.vm_lifecycle.OfficialVMLifecycle'):
            service = background_agents.OfficialBackgroundAgents(runtime, token, app)
        self.addAsyncCleanup(service.gateway.client.aclose)
        self.assertIsInstance(service.runner.engine, PiEngine)
        self.assertEqual(service.runner.engine.context_window, 128000)
        prompt = background_agents.background_prompt(settings)
        self.assertIn('uv pip install', prompt)
        self.assertIn('/etc/gaoji/skills/', prompt)
        self.assertIn('没有 apt 和 nix shell', prompt)
        self.assertIn('NixOS 容器', prompt)
        self.assertIn('只有 /work 会一直保留，其他位置的改动重启即丢失', prompt)
        self.assertNotIn('可以装软件。', prompt)

        options = dict(gateway=object(), model_settings=object(), start_vm=lambda _: None,
                       guest_exec=lambda *_: None, clock=lambda: 0)
        for bad_backend in ('vm', 'oci'):
            settings.sandbox_backend = bad_backend
            with self.assertRaisesRegex(RuntimeError, 'NixOS'):
                background_agents.make_engine(settings, **options)

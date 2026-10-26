from __future__ import annotations

import importlib
import unittest
from unittest.mock import Mock, patch

import nonebot

nonebot.init()


class AgentImportCompatibilityTests(unittest.TestCase):
    def test_old_agent_imports_share_the_new_types_and_functions(self):
        old = importlib.import_module("src.plugins.ai_chat.deepseek")
        new = importlib.import_module("src.plugins.ai_chat.agent_loop")
        package = importlib.import_module("src.plugins.ai_chat.agent")
        self.assertIs(old.DeepSeekTrace, new.AgentTrace)
        self.assertIs(old.LLMTrace, new.AgentTrace)
        self.assertIs(package.AgentTrace, new.AgentTrace)
        self.assertEqual(new.AgentTrace.__name__, "AgentTrace")
        self.assertEqual(new.AgentTrace.__module__, "src.plugins.ai_chat.agent_loop")
        for name in (
            "AgentLoopEvent",
            "FinalStreamState",
            "DeepSeekConfigError",
            "configure_llm_runtime",
            "ask_deepseek",
            "ask_deepseek_json",
            "ask_deepseek_with_tools",
            "list_deepseek_models",
            "ask_llm",
            "ask_llm_json",
            "ask_llm_with_tools",
            "list_llm_models",
            "_build_messages",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(old, name), getattr(new, name))
        self.assertIn("DeepSeekTrace", old.__all__)
        self.assertIn("AgentTrace", dir(old))
        with self.assertRaises(AttributeError):
            getattr(old, "unknown_export")

    def test_legacy_configuration_updates_the_single_canonical_runtime(self):
        old = importlib.import_module("src.plugins.ai_chat.deepseek")
        new = importlib.import_module("src.plugins.ai_chat.agent_loop")
        catalog, gateway = Mock(), Mock()
        with (
            patch.object(new, "_model_catalog", None),
            patch.object(new, "_llm_gateway", None),
        ):
            old.configure_llm_runtime(catalog, gateway)
            self.assertIs(new._model_catalog, catalog)
            self.assertIs(new._llm_gateway, gateway)
            self.assertIs(old._model_catalog, catalog)
            self.assertIs(old._llm_gateway, gateway)

    def test_onebot_compatibility_does_not_replace_application_orchestration(self):
        old = importlib.import_module("src.plugins.ai_chat.chat_orchestrator")
        new = importlib.import_module("src.plugins.ai_chat.onebot_chat")
        application = importlib.import_module(
            "src.plugins.ai_chat.application.chat_orchestrator"
        )
        self.assertIs(old.OneBotChatService, new.OneBotChatService)
        self.assertEqual(
            new.OneBotChatService.__module__, "src.plugins.ai_chat.onebot_chat"
        )
        self.assertIs(new.ChatOrchestrator, application.ChatOrchestrator)
        self.assertEqual(
            application.ChatOrchestrator.__module__,
            "src.plugins.ai_chat.application.chat_orchestrator",
        )
        self.assertIn("OneBotChatService", old.__all__)

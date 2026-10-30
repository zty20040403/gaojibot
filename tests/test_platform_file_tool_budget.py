"""File preparation must leave enough shared-loop rounds for delivery."""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.plugins.ai_chat.agent_loop import ask_deepseek_with_tools
from src.plugins.ai_chat.application.platform_chat import PlatformChatService
from src.plugins.ai_chat.model_catalog import ModelCatalog
from tests.handler_fixture import HandlerTestCaseMixin
from tests.test_shared_agent_turn import completion, official


class PlatformFileToolBudgetTests(HandlerTestCaseMixin, unittest.IsolatedAsyncioTestCase):
    async def test_file_turn_reaches_delivery_beyond_simple_chat_rounds(self):
        self.app_context.settings = replace(self.app_context.settings,
            tool_intent_enabled=False, tool_simple_max_rounds=5, tool_max_rounds=12)
        self.app_context.model_catalog = ModelCatalog.from_json(json.dumps({
            'default': 'fixture', 'profiles': {'fixture': {
                'model': 'TEST_MODEL', 'api_key_required': False,
            }},
        }), default_profile='fixture', environ={})
        names = ['use_skill', 'sandbox_list', 'sandbox_create', 'sandbox_write_file',
                 'sandbox_exec', 'sandbox_read_file', 'send_file_from_sandbox']
        tools = [{'type': 'function', 'function': {'name': name, 'parameters': {
            'type': 'object', 'properties': {}, 'additionalProperties': False,
        }}} for name in names]
        order = []

        async def execute(_turn, name, _arguments):
            order.append(name)
            self.assertEqual(_arguments, {})
            if name == 'send_file_from_sandbox' and order.count(name) == 1:
                return '{"retryable":true,"message_accepted":false}'
            return '{"ok":true}'

        async def close():
            order.append('close')

        session = SimpleNamespace(context=lambda context: context, close=AsyncMock(side_effect=close))
        registry = SimpleNamespace(definitions=lambda _: tools, execute=execute)
        service = PlatformChatService(self.app_context, scope_enabled=lambda _: True,
            generation_factory=AsyncMock(return_value=session),
            registry_factory=lambda _: registry, llm=ask_deepseek_with_tools)
        responses = [completion(tool=name) for name in names + ['send_file_from_sandbox']] + [completion('File registered.')]
        for index, response in enumerate(responses[:-1]):
            response.choices[0].message.tool_calls[0].id = f'test-call-{index}'
            response.choices[0].message.tool_calls[0].function.arguments = '{"reason":"test"}'
        inbound, platform = official(kind='private', target='TEST_FILE_BUDGET')
        with patch('src.plugins.ai_chat.agent_loop._create_completion',
                   new=AsyncMock(side_effect=responses)):
            answer = await service.ask(inbound, platform, 'Create and send a PDF')
        self.assertEqual(answer, 'File registered.')
        self.assertEqual(order, names + ['send_file_from_sandbox', 'close'])
        session.close.assert_awaited_once()
